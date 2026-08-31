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

class SemanticEarlySuccessModel {
  private built = false;
  response: ModelResponse | null = null;
  pendingUsage: Record<string, number> | null = null;
  lastCommittedAt: number | undefined;
  private monotonic: () => number = () => Date.now() / 1000;
  setMonotonic(m: () => number): void {
    this.monotonic = m;
  }
  private capture(): void {
    this.lastCommittedAt = this.monotonic();
  }
  async generate(): Promise<ModelResponse> {
    const parsed = new ModelResponse(
      "",
      [new ToolCall("c1", "shell", { command: "echo hi" })],
      { input_tokens: 7, output_tokens: 3 },
    );
    this.response = parsed;
    this.pendingUsage = { input_tokens: 7, output_tokens: 3 };
    this.built = true;
    this.capture();
    await new Promise<void>((r) => setTimeout(r, 280));
    return parsed;
  }
  isBuilt(): boolean {
    return this.built;
  }
}

class SemanticEarlyErrorModel {
  error: Error | null = null;
  lastCommittedAt: number | undefined;
  private monotonic: () => number = () => Date.now() / 1000;
  setMonotonic(m: () => number): void {
    this.monotonic = m;
  }
  private capture(): void {
    this.lastCommittedAt = this.monotonic();
  }
  async generate(): Promise<ModelResponse> {
    const err = new Error("provider boom");
    (err as unknown as { name: string }).name = "ProviderError";
    this.error = err;
    this.capture();
    await new Promise<void>((r) => setTimeout(r, 280));
    throw err;
  }
}

class LateIncompleteModel {
  lastCommittedAt: number | undefined;
  private monotonic: () => number = () => Date.now() / 1000;
  setMonotonic(m: () => number): void {
    this.monotonic = m;
  }
  private capture(): void {
    this.lastCommittedAt = this.monotonic();
  }
  async generate(): Promise<ModelResponse> {
    await new Promise<void>((r) => setTimeout(r, 500));
    const parsed = new ModelResponse("", [
      new ToolCall("c1", "shell", { command: "echo hi" }),
    ]);
    this.capture();
    return parsed;
  }
}

describe("provider/deadline semantic completion", () => {
  it("pre-deadline completed success observed late is preserved with usage and state, no tool after deadline", async () => {
    const tmp = mkdtempSync(join(tmpdir(), "flagagent-adv-"));
    try {
      const model = new SemanticEarlySuccessModel();
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
      const t0 = Number(process.hrtime.bigint()) / 1_000_000_000;
      const deadline = t0 + 0.2;
      const result = await loop.run();
      expect(model.isBuilt()).toBe(true);
      expect(model.response).not.toBeNull();
      expect(model.lastCommittedAt).toBeDefined();
      expect(model.lastCommittedAt! < deadline + 0.05).toBe(true);
      expect(result.model_calls).toBe(1);
      expect(shellExecuted).toBe(false);
      const events = readEvents(
        (loop as unknown as { artifacts: { eventsPath: string } }).artifacts.eventsPath,
      );
      const mrs = events.filter((e) => e.type === "model_response");
      expect(mrs.length).toBe(1);
      const payload = mrs[0]!.payload as Record<string, unknown>;
      expect((payload.usage as Record<string, unknown>)?.input_tokens).toBe(7);
      expect(payload.tool_calls).toBeDefined();
      const state = (loop as unknown as { messages: unknown[] }).messages;
      expect(
        state.some((m: unknown) => (m as Record<string, unknown>).role === "assistant"),
      ).toBe(true);
      const terminal = events.find((e) => e.type === "terminal_decision");
      expect(terminal).toBeDefined();
      const unprocessed = (terminal!.payload as Record<string, unknown>)
        .unprocessed_call_ids as string[];
      expect(unprocessed).toEqual(["c1"]);
      expect(events.filter((e) => e.type === "tool_call").length).toBe(0);
    } finally {
      rmSync(tmp, { recursive: true, force: true });
    }
  });

  it("pre-deadline completed provider error observed late remains provider_error", async () => {
    const tmp = mkdtempSync(join(tmpdir(), "flagagent-adv-"));
    try {
      const model = new SemanticEarlyErrorModel();
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
      const t0 = Number(process.hrtime.bigint()) / 1_000_000_000;
      const deadline = t0 + 0.2;
      const result = await loop.run();
      expect(model.error).not.toBeNull();
      expect(model.lastCommittedAt).toBeDefined();
      expect(model.lastCommittedAt! < deadline + 0.05).toBe(true);
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

  it("late or incomplete completion not accepted as pre-deadline evidence, no tool executes", async () => {
    const tmp = mkdtempSync(join(tmpdir(), "flagagent-adv-"));
    try {
      const model = new LateIncompleteModel();
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
      expect(events.filter((e) => e.type === "model_response").length).toBe(0);
      expect(events.filter((e) => e.type === "tool_call").length).toBe(0);
    } finally {
      rmSync(tmp, { recursive: true, force: true });
    }
  });
});
