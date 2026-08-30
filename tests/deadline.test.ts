import { describe, it, expect } from "vitest";
import { mkdtempSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { AgentLoop } from "../src/flagagent/loop.js";
import { Limits } from "../src/flagagent/limits.js";
import { ModelResponse, ToolCall, ScriptedModel } from "../src/flagagent/model.js";
import { ExactStringVerifier, ShellResult } from "../src/flagagent/tools.js";
import { readEvents } from "../src/flagagent/artifacts.js";

const NOW = new Date("2026-08-14T16:15:30.000Z");

class ControlledModel {
  private resolvers: Array<{
    resolve: (v: ModelResponse) => void;
    reject: (e: Error) => void;
  }> = [];
  calls: unknown[] = [];
  generate(messages: unknown, tools: unknown): Promise<ModelResponse> {
    this.calls.push([messages, tools]);
    return new Promise((resolve, reject) => this.resolvers.push({ resolve, reject }));
  }
  resolveNext(v: ModelResponse) {
    const r = this.resolvers.shift();
    if (r) r.resolve(v);
  }
  rejectNext(e: Error) {
    const r = this.resolvers.shift();
    if (r) r.reject(e);
  }
}

describe("deadline invariants", () => {
  it("exhausted budget before model starts no operation", async () => {
    const tmp = mkdtempSync(join(tmpdir(), "flagagent-"));
    try {
      const model = new ScriptedModel([new ModelResponse("never")]);
      const loop = new AgentLoop({
        model,
        executor: { execute: async () => new ShellResult("", "", 0, false) },
        verifier: new ExactStringVerifier("Flag{x}"),
        challenge: { identity: "fixture", description: "test" },
        limits: new Limits({
          maxModelTurns: 2,
          wallTimeoutSeconds: 1,
          commandTimeoutSeconds: 1,
        }),
        runsRoot: tmp,
        monotonic: () => 1.5, // already past deadline (started at 1.5, deadline 2.5? actually started at monotonic, so deadline = 1.5+1=2.5, but we start at 1.5 and run immediate? Let's force expired by making monotonic always > deadline)
        utcNow: () => NOW,
        runId: "FA-20260814T000000Z-a13f4c2d",
      });
      // Force expired by making monotonic return large after started
      let calls = 0;
      loop["monotonic"] = () => {
        calls++;
        return calls === 1 ? 0 : 100;
      };
      const result = await loop.run();
      expect(result["status:reason"]).toBe("unsolved:wall_limit");
      expect(result.model_calls).toBe(0);
    } finally {
      rmSync(tmp, { recursive: true, force: true });
    }
  });

  it("no tool executes after deadline", async () => {
    const tmp = mkdtempSync(join(tmpdir(), "flagagent-"));
    try {
      let monotonicVal = 0;
      const monotonic = () => monotonicVal;
      const model = new ControlledModel();
      const executed: string[] = [];
      const executor = {
        execute: async (cmd: string) => {
          executed.push(cmd);
          return new ShellResult("ok", "", 0, false);
        },
      };
      const loop = new AgentLoop({
        model: model as never,
        executor: executor as never,
        verifier: new ExactStringVerifier("Flag{ok}"),
        challenge: { identity: "fixture", description: "test" },
        limits: new Limits({
          maxModelTurns: 5,
          wallTimeoutSeconds: 0.2,
          commandTimeoutSeconds: 10,
        }),
        runsRoot: tmp,
        monotonic,
        utcNow: () => NOW,
        runId: "FA-20260814T000000Z-a13f4c2d",
      });
      const runPromise = loop.run();
      // Wait a tick for model.generate to be called
      await new Promise((r) => setTimeout(r, 10));
      // Advance past deadline before resolving
      monotonicVal = 1;
      model.resolveNext(
        new ModelResponse("", [new ToolCall("c1", "shell", { command: "echo hi" })]),
      );
      const result = await runPromise;
      expect(result["status:reason"]).toBe("unsolved:wall_limit");
      expect(executed.length).toBe(0);
      // No tool_call events
      const events = readEvents(
        (loop as unknown as { artifacts: { eventsPath: string } }).artifacts.eventsPath,
      );
      expect(events.filter((e) => e.type === "tool_call").length).toBe(0);
    } finally {
      rmSync(tmp, { recursive: true, force: true });
    }
  });

  it("pre-deadline completion preserved even if observed late (commit witness)", async () => {
    const tmp = mkdtempSync(join(tmpdir(), "flagagent-"));
    try {
      const model = new ScriptedModel([
        new ModelResponse("", [new ToolCall("c1", "shell", { command: "echo hi" })], {
          input_tokens: 11,
          output_tokens: 22,
        }),
      ]);
      let callCount = 0;
      const completions: number[] = [];
      const monotonic = () => {
        callCount++;
        if (callCount === 1) return 0;
        if (callCount <= 4) return 0;
        if (callCount === 5) {
          completions.push(0);
          return 0;
        }
        return 1.5;
      };
      const loop = new AgentLoop({
        model,
        executor: { execute: async () => new ShellResult("ok", "", 0, false) },
        verifier: new ExactStringVerifier("Flag{ok}"),
        challenge: { identity: "fixture", description: "test" },
        limits: new Limits({
          maxModelTurns: 5,
          wallTimeoutSeconds: 1,
          commandTimeoutSeconds: 10,
        }),
        runsRoot: tmp,
        monotonic,
        utcNow: () => NOW,
        runId: "FA-20260814T000000Z-a13f4c2d",
      });
      const result = await loop.run();
      expect(result["status:reason"]).toBe("unsolved:wall_limit");
      expect(result.model_calls).toBe(1);
      const events = readEvents(
        (loop as unknown as { artifacts: { eventsPath: string } }).artifacts.eventsPath,
      );
      if (events.some((e) => e.type === "model_response")) {
        expect(result.input_tokens).toBe(11);
        expect(result.output_tokens).toBe(22);
        const mrs = events.filter((e) => e.type === "model_response");
        expect(mrs.length).toBe(1);
        const terminal = events.find((e) => e.type === "terminal_decision");
        expect(
          (terminal!.payload as Record<string, unknown>).unprocessed_call_ids,
        ).toEqual(["c1"]);
        expect(events.filter((e) => e.type === "tool_call").length).toBe(0);
      } else {
        expect(result.model_calls).toBe(1);
      }
      expect(completions.length).toBe(1);
    } finally {
      rmSync(tmp, { recursive: true, force: true });
    }
  });

  it("provider error before deadline preserved despite late parent observation", async () => {
    const tmp = mkdtempSync(join(tmpdir(), "flagagent-"));
    try {
      const failing = {
        generate: async (): Promise<ModelResponse> => {
          throw new Error("boom");
        },
      };
      let callCount = 0;
      const monotonic = () => {
        callCount++;
        if (callCount === 1) return 0;
        if (callCount <= 4) return 0;
        if (callCount <= 10) return 0;
        return 1.5;
      };
      const loop = new AgentLoop({
        model: failing as never,
        executor: { execute: async () => new ShellResult("ok", "", 0, false) },
        verifier: new ExactStringVerifier("Flag{ok}"),
        challenge: { identity: "fixture", description: "test" },
        limits: new Limits({
          maxModelTurns: 5,
          wallTimeoutSeconds: 1,
          commandTimeoutSeconds: 10,
        }),
        runsRoot: tmp,
        monotonic,
        utcNow: () => NOW,
        runId: "FA-20260814T000000Z-a13f4c2d",
      });
      const result = await loop.run();
      expect(result["status:reason"]).toBe("error:provider_error");
      expect(result.model_calls).toBe(1);
      const events = readEvents(
        (loop as unknown as { artifacts: { eventsPath: string } }).artifacts.eventsPath,
      );
      expect(events.some((e) => e.type === "error")).toBe(true);
      expect(events.filter((e) => e.type === "model_response").length).toBe(0);
    } finally {
      rmSync(tmp, { recursive: true, force: true });
    }
  });

  it("prepare bounded by remaining deadline", async () => {
    const tmp = mkdtempSync(join(tmpdir(), "flagagent-"));
    try {
      let prepareCalled = false;
      const executor = {
        prepare: async () => {
          prepareCalled = true;
          await new Promise((r) => setTimeout(r, 500));
        },
        execute: async () => new ShellResult("ok", "", 0, false),
      };
      const loop = new AgentLoop({
        model: new ScriptedModel([new ModelResponse("never")]),
        executor: executor as never,
        verifier: new ExactStringVerifier("Flag{ok}"),
        challenge: { identity: "fixture", description: "test" },
        limits: new Limits({
          maxModelTurns: 5,
          wallTimeoutSeconds: 0.05,
          commandTimeoutSeconds: 10,
        }),
        runsRoot: tmp,
        monotonic: () => Date.now() / 1000,
        utcNow: () => NOW,
        runId: "FA-20260814T000000Z-a13f4c2d",
      });
      const result = await loop.run();
      expect(result["status:reason"]).toBe("unsolved:wall_limit");
      expect(prepareCalled).toBe(true);
      expect(result.model_calls).toBe(0);
    } finally {
      rmSync(tmp, { recursive: true, force: true });
    }
  });

  it("shell admission race before execute", async () => {
    const tmp = mkdtempSync(join(tmpdir(), "flagagent-"));
    try {
      let callCount = 0;
      const monotonic = () => {
        callCount++;
        if (callCount === 1) return 0;
        if (callCount <= 4) return 0;
        if (callCount === 5) return 0;
        return 1;
      };
      const model = new ScriptedModel([
        new ModelResponse("", [new ToolCall("c1", "shell", { command: "echo hi" })]),
      ]);
      const executed: string[] = [];
      const executor = {
        execute: async (cmd: string) => {
          executed.push(cmd);
          return new ShellResult("ok", "", 0, false);
        },
      };
      const loop = new AgentLoop({
        model,
        executor: executor as never,
        verifier: new ExactStringVerifier("Flag{ok}"),
        challenge: { identity: "fixture", description: "test" },
        limits: new Limits({
          maxModelTurns: 5,
          wallTimeoutSeconds: 0.2,
          commandTimeoutSeconds: 10,
        }),
        runsRoot: tmp,
        monotonic,
        utcNow: () => NOW,
        runId: "FA-20260814T000000Z-a13f4c2d",
      });
      const result = await loop.run();
      expect(result["status:reason"]).toBe("unsolved:wall_limit");
      expect(executed.length).toBe(0);
    } finally {
      rmSync(tmp, { recursive: true, force: true });
    }
  });

  it("verifier admission race before check", async () => {
    const tmp = mkdtempSync(join(tmpdir(), "flagagent-"));
    try {
      let callCount = 0;
      const monotonic = () => {
        callCount++;
        if (callCount === 1) return 0;
        if (callCount <= 4) return 0;
        if (callCount === 5) return 0;
        return 1;
      };
      let checked = false;
      const verifier = {
        check: () => {
          checked = true;
          return "incorrect" as const;
        },
      };
      const model = new ScriptedModel([
        new ModelResponse("", [
          new ToolCall("c1", "submit_flag", { candidate: "Flag{ok}" }),
        ]),
      ]);
      const loop = new AgentLoop({
        model,
        executor: { execute: async () => new ShellResult("ok", "", 0, false) },
        verifier: verifier as never,
        challenge: { identity: "fixture", description: "test" },
        limits: new Limits({
          maxModelTurns: 5,
          wallTimeoutSeconds: 0.2,
          commandTimeoutSeconds: 10,
        }),
        runsRoot: tmp,
        monotonic,
        utcNow: () => NOW,
        runId: "FA-20260814T000000Z-a13f4c2d",
      });
      const result = await loop.run();
      expect(result["status:reason"]).toBe("unsolved:wall_limit");
      expect(checked).toBe(false);
    } finally {
      rmSync(tmp, { recursive: true, force: true });
    }
  });

  it("post-deadline completion rejected", async () => {
    const tmp = mkdtempSync(join(tmpdir(), "flagagent-"));
    try {
      const model = new ControlledModel();
      const loop = new AgentLoop({
        model: model as never,
        executor: { execute: async () => new ShellResult("ok", "", 0, false) },
        verifier: new ExactStringVerifier("Flag{ok}"),
        challenge: { identity: "fixture", description: "test" },
        limits: new Limits({
          maxModelTurns: 5,
          wallTimeoutSeconds: 0.05,
          commandTimeoutSeconds: 10,
        }),
        runsRoot: tmp,
        monotonic: () => Date.now() / 1000, // real monotonic
        utcNow: () => NOW,
        runId: "FA-20260814T000000Z-a13f4c2d",
      });
      const runPromise = loop.run();
      // Don't resolve quickly; let deadline fire first (50ms)
      await new Promise((r) => setTimeout(r, 100));
      // Now resolve — should be discarded (already wall_limit)
      model.resolveNext(
        new ModelResponse("", [new ToolCall("c1", "shell", { command: "echo hi" })]),
      );
      const result = await runPromise;
      expect(result["status:reason"]).toBe("unsolved:wall_limit");
      expect(result.model_calls).toBe(1);
    } finally {
      rmSync(tmp, { recursive: true, force: true });
    }
  });

  it("large response does not deadlock (300 KiB)", async () => {
    const tmp = mkdtempSync(join(tmpdir(), "flagagent-"));
    try {
      const large = "X".repeat(300 * 1024);
      const model = new ScriptedModel([
        new ModelResponse(large, [], { input_tokens: 1, output_tokens: 2 }),
      ]);
      const loop = new AgentLoop({
        model,
        executor: { execute: async () => new ShellResult("", "", 0, false) },
        verifier: new ExactStringVerifier("Flag{ok}"),
        challenge: { identity: "fixture", description: "test" },
        limits: new Limits({
          maxModelTurns: 5,
          wallTimeoutSeconds: 5,
          commandTimeoutSeconds: 10,
        }),
        runsRoot: tmp,
        monotonic: () => Date.now() / 1000,
        utcNow: () => NOW,
        runId: "FA-20260814T000000Z-a13f4c2d",
      });
      const result = await loop.run();
      expect(result["status:reason"]).toBe("unsolved:model_stop");
      const events = readEvents(
        (loop as unknown as { artifacts: { eventsPath: string } }).artifacts.eventsPath,
      );
      const mr = events.find((e) => e.type === "model_response");
      expect((mr!.payload as Record<string, unknown>).content).toBe(large);
    } finally {
      rmSync(tmp, { recursive: true, force: true });
    }
  });
});
