import { describe, it, expect } from "vitest";
import { mkdtempSync, rmSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { ToolCall, ModelResponse, ScriptedModel } from "../src/flagagent/model.js";
import {
  validateToolArguments,
  ShellResult,
  truncateUtf8,
  normalizeShellResult,
  ExactStringVerifier,
  MODEL_TOOL_OUTPUT_BYTES,
  LOGGED_TOOL_OUTPUT_BYTES,
} from "../src/flagagent/tools.js";
import { Limits } from "../src/flagagent/limits.js";
import {
  RunArtifacts,
  validateRunId,
  readEvents,
  EventStreamPoisoned,
} from "../src/flagagent/artifacts.js";
import { codeSpan, renderWriteup } from "../src/flagagent/writeup.js";
import {
  SOLVER_PROMPT,
  SOLVER_PROMPT_SHA256,
  SOLVER_PROMPT_VERSION,
} from "../src/flagagent/prompt.js";
import { FLAGAGENT_VERSION } from "../src/flagagent/version.js";
import { createHash } from "node:crypto";

function tmpRoot(): string {
  return mkdtempSync(join(tmpdir(), "flagagent-test-"));
}

describe("model snapshot and contracts", () => {
  it("snapshots mutable values", () => {
    const args: Record<string, unknown> = { command: "true" };
    const usage: Record<string, unknown> = { input_tokens: 1 };
    const response = new ModelResponse(
      "working",
      [new ToolCall("call-1", "shell", args)],
      usage,
    );
    args.command = "false";
    (usage as Record<string, unknown>).input_tokens = 99;
    expect(response.toolCalls[0]!.arguments).toEqual({ command: "true" });
    expect(response.usage).toEqual({ input_tokens: 1 });
  });
  it("rejects malformed tool call values", () => {
    expect(() => new ToolCall("", "shell", { command: "true" })).toThrow();
    expect(() => new ToolCall("call-1", "", { command: "true" })).toThrow();
    expect(() => new ToolCall("call-1", "shell", [])).toThrow();
  });
  it("scripted model snapshots and replays error", async () => {
    const err = new Error("provider failed");
    const model = new ScriptedModel([new ModelResponse("first"), err]);
    const messages: Record<string, unknown>[] = [{ role: "user", content: "task" }];
    const tools: Record<string, unknown>[] = [{ name: "shell" }];
    expect((await model.generate(messages as never[], tools as never[])).content).toBe("first");
    messages[0]!.content = "changed";
    tools[0]!.name = "changed";
    await expect(model.generate(messages as never[], tools as never[])).rejects.toThrow(
      "provider failed",
    );
    expect(model.calls[0]).toEqual([
      [{ role: "user", content: "task" }],
      [{ name: "shell" }],
    ]);
  });
});

describe("tool validation", () => {
  it("accepts exact required fields and preserves value", () => {
    expect(validateToolArguments("shell", { command: "printf ok" })).toEqual({
      command: "printf ok",
    });
    expect(validateToolArguments("submit_flag", { candidate: " Flag{x} " })).toEqual({
      candidate: " Flag{x} ",
    });
  });
  it("rejects extra/empty/non-string args", () => {
    expect(() => validateToolArguments("shell", {})).toThrow();
    expect(() => validateToolArguments("shell", { command: "  " })).toThrow();
    expect(() =>
      validateToolArguments("shell", { command: "true", cwd: "/" } as never),
    ).toThrow();
    expect(() =>
      validateToolArguments("submit_flag", { candidate: 1 } as never),
    ).toThrow();
  });
});

describe("ShellResult shapes", () => {
  it("accepts nonzero and timeout evidence", () => {
    expect(new ShellResult("", "", 1, false).exitCode).toBe(1);
    expect(new ShellResult("partial", "", null, true).timedOut).toBe(true);
  });
  it("rejects invalid timeout shapes", () => {
    expect(() => new ShellResult("", "", null as never, false)).toThrow();
  });
});

describe("ExactStringVerifier", () => {
  it("strips and is case-sensitive", () => {
    const v = new ExactStringVerifier("Flag{Example}");
    expect(v.check("  Flag{Example}\n")).toBe("correct");
    expect(v.check("flag{Example}")).toBe("incorrect");
  });
});

describe("truncateUtf8", () => {
  it("preserves head/tail and byte budget", () => {
    const [value, truncated] = truncateUtf8("abcdefghijklmnopqrstuvwxyz", 14);
    expect(truncated).toBe(true);
    expect(value.startsWith("a")).toBe(true);
    expect(value).toContain("truncated");
    expect(value.endsWith("z")).toBe(true);
    expect(Buffer.byteLength(value, "utf8")).toBeLessThanOrEqual(14);
  });
  it("handles multibyte and tiny budgets", () => {
    const [value, truncated] = truncateUtf8("\u03b1\u03b2\u03b3\u03b4\u03b5", 8);
    const [tiny, tinyT] = truncateUtf8("abcdef", 3);
    expect(truncated).toBe(true);
    expect(Buffer.byteLength(value, "utf8")).toBeLessThanOrEqual(8);
    expect(tinyT).toBe(true);
    expect(Buffer.byteLength(tiny, "utf8")).toBeLessThanOrEqual(3);
  });
  it("canonical byte boundaries exact", () => {
    const [at, atT] = truncateUtf8(
      "a".repeat(MODEL_TOOL_OUTPUT_BYTES),
      MODEL_TOOL_OUTPUT_BYTES,
    );
    expect(atT).toBe(false);
    expect(at).toBe("a".repeat(MODEL_TOOL_OUTPUT_BYTES));
    const [over, overT] = truncateUtf8(
      "a".repeat(MODEL_TOOL_OUTPUT_BYTES + 1),
      MODEL_TOOL_OUTPUT_BYTES,
    );
    expect(overT).toBe(true);
    expect(Buffer.byteLength(over, "utf8")).toBeLessThanOrEqual(
      MODEL_TOOL_OUTPUT_BYTES,
    );
    expect(over).toContain("truncated");
  });
  it("exact boundary not truncated", () => {
    const [v, t] = truncateUtf8("12345678", 8);
    expect(v).toBe("12345678");
    expect(t).toBe(false);
  });
});

describe("normalizeShellResult", () => {
  it("bounds streams independently", () => {
    const original = new ShellResult("A".repeat(40), "B".repeat(40), 0, false);
    const [model, logged] = normalizeShellResult(original, 16, 32);
    expect(Buffer.byteLength(model.stdout, "utf8")).toBeLessThanOrEqual(16);
    expect(Buffer.byteLength(logged.stdout, "utf8")).toBeLessThanOrEqual(32);
    expect(logged.stdout.endsWith("A")).toBe(true);
  });
  it("default limits bound multibyte independently", () => {
    const original = new ShellResult(
      "\u03b1".repeat(MODEL_TOOL_OUTPUT_BYTES + 1),
      "\u03b2".repeat(LOGGED_TOOL_OUTPUT_BYTES + 1),
      0,
      false,
    );
    const [model, logged] = normalizeShellResult(original);
    expect(Buffer.byteLength(model.stdout, "utf8")).toBeLessThanOrEqual(
      MODEL_TOOL_OUTPUT_BYTES,
    );
    expect(Buffer.byteLength(logged.stdout, "utf8")).toBeLessThanOrEqual(
      LOGGED_TOOL_OUTPUT_BYTES,
    );
    expect(model.stdout).toContain("truncated");
    expect(logged.stdout).toBe(original.stdout);
  });
});

describe("Limits", () => {
  it("rejects nonpositive and boolean", () => {
    expect(() => new Limits({ maxModelTurns: 0 } as never)).toThrow();
    expect(() => new Limits({ maxModelTurns: true as never } as never)).toThrow();
    expect(
      () => new Limits({ maxModelToolOutputBytes: 20, maxLoggedToolOutputBytes: 10 }),
    ).toThrow();
  });
  it("toDict includes source limits", () => {
    const v = new Limits().toObject();
    expect(v.max_source_file_bytes).toBe(10 * 1024 * 1024);
    expect(v.max_source_total_bytes).toBe(50 * 1024 * 1024);
  });
});

describe("artifacts", () => {
  const FIXED = new Date("2026-08-14T16:15:30.000Z");
  function meta() {
    return {
      schema_version: 1,
      run_id: "FA-20260814T161530Z-a13f4c2d",
      flagagent_version: "0.1.0",
      concept_version: "0.1.0",
      challenge: { identity: "fixture", description: "test" },
      started_at: "2026-08-14T16:15:30Z",
      limits: { max_model_turns: 1 },
    };
  }
  it("create without overwriting", () => {
    const root = tmpRoot();
    try {
      const a = RunArtifacts.create(root, meta(), {
        runId: "FA-20260814T161530Z-a13f4c2d",
        now: () => FIXED,
      });
      expect(readFileSync(a.runPath, "utf8")).toContain("fixture");
      expect(() =>
        RunArtifacts.create(root, meta(), { runId: a.runId, now: () => FIXED }),
      ).toThrow();
      a.close();
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });
  it("generated id matches contract", () => {
    const id = RunArtifacts.generateRunId(
      () => FIXED,
      () => "a13f4c2d",
    );
    expect(id).toBe("FA-20260814T161530Z-a13f4c2d");
  });
  it("validates run ids", () => {
    expect(() => validateRunId("FA/../escape")).toThrow();
    expect(() => validateRunId(".hidden")).toThrow();
    expect(validateRunId("FA-TEST-abcd1234")).toBe("FA-TEST-abcd1234");
  });
  it("events sequenced", () => {
    const root = tmpRoot();
    try {
      const a = RunArtifacts.create(root, meta(), {
        runId: meta().run_id,
        now: () => FIXED,
      });
      const f = a.appendEvent("model_response", { content: "hi" });
      const s = a.appendEvent("tool_call", { call_id: "call-1" });
      expect(f.seq).toBe(1);
      expect(s.seq).toBe(2);
      const ev = readEvents(a.eventsPath);
      expect(ev.map((e) => e.type)).toEqual(["model_response", "tool_call"]);
      a.close();
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });
  it("reader ignores only trailing incomplete line", () => {
    const root = tmpRoot();
    try {
      const p = join(root, "events.jsonl");
      writeFileSync(p, '{"seq":1}\n{"seq":2', "utf8");
      expect(readEvents(p)).toEqual([{ seq: 1 }]);
      writeFileSync(p, '{"seq":1}\nnot-json\n{"seq":3}\n', "utf8");
      expect(() => readEvents(p)).toThrow(/interior/);
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });
  it("poisons stream on failure", () => {
    const root = tmpRoot();
    try {
      const a = RunArtifacts.create(root, meta(), {
        runId: meta().run_id,
        now: () => FIXED,
      });
      expect(() => a.appendEvent("error", { bad: Infinity } as never)).toThrow();
      expect(() => a.appendEvent("error", { safe: true } as never)).toThrow(
        EventStreamPoisoned,
      );
      a.close();
      expect(() => a.close()).not.toThrow();
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });
  it("commit uses replace and refuses second", () => {
    const root = tmpRoot();
    try {
      const a = RunArtifacts.create(root, meta(), {
        runId: meta().run_id,
        now: () => FIXED,
      });
      a.commitResult({
        schema_version: 1,
        run_id: a.runId,
        status: "unsolved",
        reason: "model_stop",
      });
      expect(() =>
        a.commitResult({
          schema_version: 1,
          run_id: a.runId,
          status: "unsolved",
          reason: "model_stop",
        }),
      ).toThrow();
      a.close();
      // idempotent: second close does not throw
      expect(() => a.close()).not.toThrow();
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });
});

describe("writeup", () => {
  it("codeSpan delimiter selection", () => {
    expect(codeSpan("a`b")).toBe("``a`b``");
    expect(codeSpan("a``b")).toBe("```a``b```");
    expect(codeSpan("`leading")).toBe("`` `leading ``");
    expect(codeSpan("no ticks")).toBe("`no ticks`");
    expect(codeSpan("")).toBe("``");
  });
  it("renderWriteup derived", () => {
    const run: Record<string, unknown> = {
      run_id: "FA-x",
      challenge: { identity: "fixture" },
      model: { name: "m", protocol: "openai-chat" },
      prompt: { version: "0.1.0", sha256: "abc" },
    };
    const result: Record<string, unknown> = {
      status: "solved",
      reason: "verified_flag",
      duration_seconds: 1,
      model_calls: 1,
      tool_calls: 1,
      flag_submissions: 1,
    };
    const text = renderWriteup(run as never, [], result as never);
    expect(text).toContain("# FlagAgent Run");
    expect(text).toContain("fixture");
  });
});

describe("prompt", () => {
  it("sha matches content", () => {
    expect(SOLVER_PROMPT_SHA256).toBe(
      createHash("sha256").update(SOLVER_PROMPT, "utf8").digest("hex"),
    );
    expect(SOLVER_PROMPT_VERSION).toBe("0.1.0");
    expect(FLAGAGENT_VERSION).toBe("0.2.0");
  });
});
