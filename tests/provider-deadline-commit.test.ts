import { describe, it, expect } from "vitest";
import { mkdtempSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { AgentLoop } from "../src/flagagent/loop.js";
import { Limits } from "../src/flagagent/limits.js";
import { ModelResponse, ToolCall } from "../src/flagagent/model.js";
import { ExactStringVerifier, ShellResult } from "../src/flagagent/tools.js";
import { readEvents } from "../src/flagagent/artifacts.js";

const NOW = new Date("2026-08-14T16:15:30.000Z");

class ControlledModel {
  private resolvers: Array<{
    resolve: (v: ModelResponse) => void;
    reject: (e: Error) => void;
  }> = [];
  lastCommittedAt: number | undefined;
  private monotonic: () => number = () => Date.now() / 1000;
  setMonotonic(m: () => number): void {
    this.monotonic = m;
  }
  generate(): Promise<ModelResponse> {
    return new Promise((resolve, reject) => this.resolvers.push({ resolve, reject }));
  }
  resolveNext(v: ModelResponse): void {
    this.lastCommittedAt = this.monotonic();
    const r = this.resolvers.shift();
    r?.resolve(v);
  }
  rejectNext(e: Error): void {
    this.lastCommittedAt = this.monotonic();
    const r = this.resolvers.shift();
    r?.reject(e);
  }
}

describe("D013 deadline-gated commit (loop-level)", () => {
  it("pre-deadline success observed late is preserved with usage and unprocessed, no tool after deadline", async () => {
    const tmp = mkdtempSync(join(tmpdir(), "flagagent-"));
    try {
      let monotonicVal = 0;
      const monotonic = () => monotonicVal;
      const model = new ControlledModel();
      const loop = new AgentLoop({
        model: model as never,
        executor: { execute: async () => new ShellResult("ok", "", 0, false) } as never,
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
      await new Promise((r) => setTimeout(r, 10));
      monotonicVal = 0.05;
      model.resolveNext(
        new ModelResponse("", [new ToolCall("c1", "shell", { command: "echo hi" })], {
          input_tokens: 7,
          output_tokens: 3,
        }),
      );
      monotonicVal = 1;
      const result = await runPromise;
      expect(model.lastCommittedAt).toBeDefined();
      expect(model.lastCommittedAt! < 0.2).toBe(true);
      expect(result.model_calls).toBe(1);
      const events = readEvents(
        (loop as unknown as { artifacts: { eventsPath: string } }).artifacts.eventsPath,
      );
      const mrs = events.filter((e) => e.type === "model_response");
      expect(mrs.length).toBe(1);
      expect((mrs[0]!.payload as Record<string, unknown>).tool_calls).toBeDefined();
      expect(
        ((mrs[0]!.payload as Record<string, unknown>).usage as Record<string, unknown>)
          ?.input_tokens,
      ).toBe(7);
      const term = events.find((e) => e.type === "terminal_decision");
      expect((term!.payload as Record<string, unknown>).unprocessed_call_ids).toEqual([
        "c1",
      ]);
      expect(events.filter((e) => e.type === "tool_call").length).toBe(0);
    } finally {
      rmSync(tmp, { recursive: true, force: true });
    }
  });

  it("pre-deadline error observed late remains provider_error", async () => {
    const tmp = mkdtempSync(join(tmpdir(), "flagagent-"));
    try {
      let monotonicVal = 0;
      const monotonic = () => monotonicVal;
      const model = new ControlledModel();
      const loop = new AgentLoop({
        model: model as never,
        executor: { execute: async () => new ShellResult("ok", "", 0, false) } as never,
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
      await new Promise((r) => setTimeout(r, 10));
      monotonicVal = 0.05;
      model.rejectNext(new Error("boom"));
      monotonicVal = 1;
      const result = await runPromise;
      expect(result["status:reason"]).toBe("error:provider_error");
      const events = readEvents(
        (loop as unknown as { artifacts: { eventsPath: string } }).artifacts.eventsPath,
      );
      expect(events.some((e) => e.type === "error")).toBe(true);
      expect(events.filter((e) => e.type === "tool_call").length).toBe(0);
    } finally {
      rmSync(tmp, { recursive: true, force: true });
    }
  });

  it("late completion not accepted as pre-deadline evidence, no tool executes", async () => {
    const tmp = mkdtempSync(join(tmpdir(), "flagagent-"));
    try {
      let monotonicVal = 0;
      const monotonic = () => monotonicVal;
      const model = new ControlledModel();
      const loop = new AgentLoop({
        model: model as never,
        executor: { execute: async () => new ShellResult("ok", "", 0, false) } as never,
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
      await new Promise((r) => setTimeout(r, 10));
      monotonicVal = 1;
      model.resolveNext(
        new ModelResponse("", [new ToolCall("c1", "shell", { command: "echo hi" })]),
      );
      const result = await runPromise;
      expect(result["status:reason"]).toBe("unsolved:wall_limit");
      expect(result.model_calls).toBe(1);
      const events = readEvents(
        (loop as unknown as { artifacts: { eventsPath: string } }).artifacts.eventsPath,
      );
      expect(events.filter((e) => e.type === "model_response").length).toBe(0);
      expect(events.filter((e) => e.type === "tool_call").length).toBe(0);
    } finally {
      rmSync(tmp, { recursive: true, force: true });
    }
  });
});
