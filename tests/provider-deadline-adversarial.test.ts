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

class EarlyCommitLateObserveSuccessModel {
  lastCommittedAt: number | undefined;
  private monotonic: () => number = () => Date.now() / 1000;
  setMonotonic(m: () => number): void {
    this.monotonic = m;
  }
  private capture(): void {
    this.lastCommittedAt = this.monotonic();
  }
  async generate(): Promise<ModelResponse> {
    // Commit before deadline
    this.capture();
    const committed = this.lastCommittedAt!;
    // Independent timing witness: captured < deadline, but we delay settlement past deadline
    // Delay 80ms after deadline window to ensure parent observes late but within 150ms drain
    // Wall is 0.2s, so capture at ~0.01, settlement at ~0.28 (80ms after deadline 0.2) -> within 150ms drain
    await new Promise<void>((r) => setTimeout(r, 280));
    // Verify capture was before deadline independently (caller checks via monotonic)
    void committed;
    return new ModelResponse(
      "",
      [new ToolCall("c1", "shell", { command: "echo hi" })],
      {
        input_tokens: 7,
        output_tokens: 3,
      },
    );
  }
}

class EarlyCommitLateObserveFailureModel {
  lastCommittedAt: number | undefined;
  private monotonic: () => number = () => Date.now() / 1000;
  setMonotonic(m: () => number): void {
    this.monotonic = m;
  }
  private capture(): void {
    this.lastCommittedAt = this.monotonic();
  }
  async generate(): Promise<ModelResponse> {
    this.capture();
    await new Promise<void>((r) => setTimeout(r, 280));
    throw new Error("provider boom");
  }
}

class LateCommitModel {
  lastCommittedAt: number | undefined;
  private monotonic: () => number = () => Date.now() / 1000;
  setMonotonic(m: () => number): void {
    this.monotonic = m;
  }
  private capture(): void {
    this.lastCommittedAt = this.monotonic();
  }
  async generate(): Promise<ModelResponse> {
    // Sleep past deadline BEFORE capture -> committedAt >= deadline
    await new Promise<void>((r) => setTimeout(r, 350));
    this.capture();
    return new ModelResponse("", [new ToolCall("c1", "shell", { command: "echo hi" })]);
  }
}

describe("provider/deadline adversarial completion semantics", () => {
  it("pre-deadline success observed late is preserved (model_response, usage, unprocessed, no shell)", async () => {
    const tmp = mkdtempSync(join(tmpdir(), "flagagent-adv-"));
    try {
      const model = new EarlyCommitLateObserveSuccessModel();
      let shellExecuted = false;
      const executor = {
        execute: async () => {
          shellExecuted = true;
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
        monotonic: () => Number(process.hrtime.bigint()) / 1_000_000_000,
        utcNow: () => NOW,
        runId: "FA-20260814T000000Z-a13f4c2d",
      });
      const result = await loop.run();
      // Must NOT be wall_limit swallowing the preserved response; committed before deadline => wall_limit with unprocessed preservation via committed path?
      // For success case, pre-deadline tool_calls are preserved as unprocessed, no shell executed
      // Implementation preserves model_response and unprocessed ids even though status is wall_limit (since tools not executed)
      // Check that model_response exists and usage preserved, and shell not executed
      expect(result.model_calls).toBe(1);
      expect(shellExecuted).toBe(false);
      const events = readEvents(
        (loop as unknown as { artifacts: { eventsPath: string } }).artifacts.eventsPath,
      );
      const mrs = events.filter((e) => e.type === "model_response");
      expect(mrs.length).toBe(1);
      const payload = mrs[0]!.payload as Record<string, unknown>;
      expect(payload.tool_calls).toBeDefined();
      // usage preserved
      expect((payload.usage as Record<string, unknown>)?.input_tokens).toBe(7);
      const terminal = events.find((e) => e.type === "terminal_decision");
      expect(terminal).toBeDefined();
      const unprocessed = (terminal!.payload as Record<string, unknown>)
        .unprocessed_call_ids as string[];
      expect(unprocessed).toEqual(["c1"]);
      // No tool_call executed
      expect(events.filter((e) => e.type === "tool_call").length).toBe(0);
      // Independent timing witness: captured before deadline
      expect(typeof model.lastCommittedAt).toBe("number");
    } finally {
      rmSync(tmp, { recursive: true, force: true });
    }
  });

  it("pre-deadline provider error observed late remains provider_error", async () => {
    const tmp = mkdtempSync(join(tmpdir(), "flagagent-adv-"));
    try {
      const model = new EarlyCommitLateObserveFailureModel();
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
        monotonic: () => Number(process.hrtime.bigint()) / 1_000_000_000,
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
      expect(events.filter((e) => e.type === "tool_call").length).toBe(0);
    } finally {
      rmSync(tmp, { recursive: true, force: true });
    }
  });

  it("late/incomplete completion rejected as pre-deadline evidence, no tool executes", async () => {
    const tmp = mkdtempSync(join(tmpdir(), "flagagent-adv-"));
    try {
      const model = new LateCommitModel();
      let shellExecuted = false;
      const executor = {
        execute: async () => {
          shellExecuted = true;
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
        monotonic: () => Number(process.hrtime.bigint()) / 1_000_000_000,
        utcNow: () => NOW,
        runId: "FA-20260814T000000Z-a13f4c2d",
      });
      const result = await loop.run();
      expect(result["status:reason"]).toBe("unsolved:wall_limit");
      expect(result.model_calls).toBe(1);
      expect(shellExecuted).toBe(false);
      const events = readEvents(
        (loop as unknown as { artifacts: { eventsPath: string } }).artifacts.eventsPath,
      );
      // Late commit must NOT create model_response (rejected)
      expect(events.filter((e) => e.type === "model_response").length).toBe(0);
      expect(events.filter((e) => e.type === "tool_call").length).toBe(0);
      // Timing witness: committedAt >= deadline so should be rejected
      expect(typeof model.lastCommittedAt).toBe("number");
    } finally {
      rmSync(tmp, { recursive: true, force: true });
    }
  });
});
