import { describe, it, expect } from "vitest";
import { mkdtempSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { AgentLoop } from "../src/flagagent/loop.js";
import { Limits } from "../src/flagagent/limits.js";
import { ModelResponse, ToolCall, ScriptedModel } from "../src/flagagent/model.js";
import { ExactStringVerifier, ShellResult } from "../src/flagagent/tools.js";

const NOW = new Date("2026-08-14T16:15:30.000Z");

class Clock {
  value: number;
  constructor(v = 0) {
    this.value = v;
  }
  fn = () => this.value;
  advance(n: number) {
    this.value += n;
  }
}

function runLoop(
  tmp: string,
  responses: (ModelResponse | Error)[],
  executor?: unknown,
  verifier?: unknown,
  limits?: Limits,
  clock?: Clock,
) {
  const loop = new AgentLoop({
    model: new ScriptedModel(responses as InstanceType<typeof ScriptedModel>["script"]),
    executor: (executor as never) ?? {
      execute: async () => new ShellResult("", "", 0, false),
    },
    verifier: (verifier as never) ?? new ExactStringVerifier("Flag{ok}"),
    challenge: { identity: "fixture", description: "solve it" },
    limits:
      limits ??
      new Limits({
        maxModelTurns: 10,
        wallTimeoutSeconds: 100,
        commandTimeoutSeconds: 10,
      }),
    runsRoot: tmp,
    monotonic: clock ? clock.fn : () => 0,
    utcNow: () => NOW,
    runId: "FA-20260814T161530Z-a13f4c2d",
  });
  return loop.run().then((result) => ({ loop, result }));
}

describe("loop basic", () => {
  it("assistant precedes correlated sequential results", async () => {
    const tmp = mkdtempSync(join(tmpdir(), "flagagent-"));
    try {
      const calls: [string, number][] = [];
      const executor = {
        execute: async (cmd: string, t: number) => {
          calls.push([cmd, t]);
          return cmd === "one"
            ? new ShellResult("one", "", 0, false)
            : new ShellResult("two", "", 0, false);
        },
      };
      const { loop, result } = await runLoop(
        tmp,
        [
          new ModelResponse("", [
            new ToolCall("c1", "shell", { command: "one" }),
            new ToolCall("c2", "shell", { command: "two" }),
          ]),
          new ModelResponse("stop"),
        ],
        executor,
      );
      expect(result["status:reason"]).toBe("unsolved:model_stop");
      expect(calls).toEqual([
        ["one", 10],
        ["two", 10],
      ]);
      // @ts-expect-error accessing private for test
      const roles = (loop as unknown as { messages: { role: string }[] }).messages.map(
        (m) => m.role,
      );
      expect(roles).toEqual(["user", "assistant", "tool", "tool", "assistant"]);
    } finally {
      rmSync(tmp, { recursive: true, force: true });
    }
  });

  it("unknown and invalid recover without execution", async () => {
    const tmp = mkdtempSync(join(tmpdir(), "flagagent-"));
    try {
      const executor = { execute: async () => new ShellResult("", "", 0, false) };
      const { loop, result } = await runLoop(
        tmp,
        [
          new ModelResponse("", [
            new ToolCall("u", "invented", {}),
            new ToolCall("i", "shell", { command: " " }),
          ]),
          new ModelResponse("done"),
        ],
        executor,
      );
      expect(result.reason).toBe("model_stop");
      // @ts-expect-error accessing private for test
      const toolResults = (
        loop as unknown as {
          messages: { role: string; result: { error: { type: string } } }[];
        }
      ).messages.filter((m) => m.role === "tool");
      expect(toolResults.map((m) => m.result.error.type)).toEqual([
        "unknown_tool",
        "invalid_arguments",
      ]);
    } finally {
      rmSync(tmp, { recursive: true, force: true });
    }
  });

  it("duplicate call_id is provider_error", async () => {
    const tmp = mkdtempSync(join(tmpdir(), "flagagent-"));
    try {
      const { result } = await runLoop(tmp, [
        new ModelResponse("", [
          new ToolCall("dup", "shell", { command: "one" }),
          new ToolCall("dup", "shell", { command: "two" }),
        ]),
      ]);
      expect(result["status:reason"]).toBe("error:provider_error");
    } finally {
      rmSync(tmp, { recursive: true, force: true });
    }
  });

  it("wrong flag continues and correct short-circuits", async () => {
    const tmp = mkdtempSync(join(tmpdir(), "flagagent-"));
    try {
      const calls: string[] = [];
      const executor = {
        execute: async (cmd: string) => {
          calls.push(cmd);
          return new ShellResult("after wrong", "", 0, false);
        },
      };
      const { result } = await runLoop(
        tmp,
        [
          new ModelResponse("", [
            new ToolCall("w", "submit_flag", { candidate: "wrong" }),
            new ToolCall("s", "shell", { command: "after-wrong" }),
            new ToolCall("c", "submit_flag", { candidate: "  Flag{ok}\n" }),
            new ToolCall("x", "shell", { command: "skipped" }),
          ]),
        ],
        executor,
      );
      expect(result["status:reason"]).toBe("solved:verified_flag");
      expect(calls).toEqual(["after-wrong"]);
    } finally {
      rmSync(tmp, { recursive: true, force: true });
    }
  });

  it("truncated does not execute tools", async () => {
    const tmp = mkdtempSync(join(tmpdir(), "flagagent-"));
    try {
      const executor = {
        execute: async () => new ShellResult("should not run", "", 0, false),
      };
      const { result } = await runLoop(
        tmp,
        [new ModelResponse("partial", [], { input_tokens: 5, output_tokens: 7 }, true)],
        executor,
      );
      expect(result["status:reason"]).toBe("unsolved:model_output_limit");
    } finally {
      rmSync(tmp, { recursive: true, force: true });
    }
  });

  it("wall deadline wins over tool execution", async () => {
    const tmp = mkdtempSync(join(tmpdir(), "flagagent-"));
    try {
      const clock = new Clock(95);
      const executor = {
        execute: async () => {
          // advance clock past deadline during execution
          clock.value = 100;
          return new ShellResult("late", "", 0, false);
        },
      };
      const { result } = await runLoop(
        tmp,
        [new ModelResponse("", [new ToolCall("c", "shell", { command: "x" })])],
        executor,
        undefined,
        new Limits({
          maxModelTurns: 2,
          wallTimeoutSeconds: 5,
          commandTimeoutSeconds: 60,
        }),
        clock,
      );
      expect(result["status:reason"]).toBe("unsolved:wall_limit");
    } finally {
      rmSync(tmp, { recursive: true, force: true });
    }
  });

  it("model text is not success", async () => {
    const tmp = mkdtempSync(join(tmpdir(), "flagagent-"));
    try {
      const { result } = await runLoop(tmp, [new ModelResponse("Flag{ok}")]);
      expect(result["status:reason"]).toBe("unsolved:model_stop");
    } finally {
      rmSync(tmp, { recursive: true, force: true });
    }
  });

  it("non-Docker executor without deadline seam still works", async () => {
    const tmp = mkdtempSync(join(tmpdir(), "flagagent-"));
    try {
      const executor = {
        execute: async () => new ShellResult("ok", "", 0, false),
      };
      const { result } = await runLoop(
        tmp,
        [
          new ModelResponse("", [new ToolCall("s", "shell", { command: "x" })]),
          new ModelResponse("done"),
        ],
        executor,
      );
      expect(result["status:reason"]).toBe("unsolved:model_stop");
    } finally {
      rmSync(tmp, { recursive: true, force: true });
    }
  });
});
