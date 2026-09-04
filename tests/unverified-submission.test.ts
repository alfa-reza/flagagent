import { describe, it, expect } from "vitest";
import { mkdtempSync, rmSync, mkdirSync, writeFileSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { AgentLoop } from "../src/flagagent/loop.js";
import { Limits } from "../src/flagagent/limits.js";
import { ModelResponse, ToolCall, ScriptedModel } from "../src/flagagent/model.js";
import { ExactStringVerifier, ShellResult } from "../src/flagagent/tools.js";
import { readEvents } from "../src/flagagent/artifacts.js";
import { loadChallenge } from "../src/flagagent/cli.js";

const NOW = new Date("2026-08-14T16:15:30.000Z");

function limits(): Limits {
  return new Limits({
    maxModelTurns: 10,
    wallTimeoutSeconds: 100,
    commandTimeoutSeconds: 10,
  });
}

function makeChallengeDir(payload: Record<string, unknown>): string {
  const tmp = mkdtempSync(join(tmpdir(), "flagagent-desc-"));
  const dir = join(tmp, "ch");
  mkdirSync(dir, { recursive: true });
  writeFileSync(join(dir, "challenge.json"), JSON.stringify(payload));
  return dir;
}

async function runLoop(
  tmp: string,
  responses: (ModelResponse | Error)[],
  verifier: unknown,
  executor?: unknown,
  opts?: { monotonic?: () => number; omitVerifier?: boolean },
) {
  const base: Record<string, unknown> = {
    model: new ScriptedModel(responses as InstanceType<typeof ScriptedModel>["script"]),
    executor: (executor as never) ?? {
      execute: async () => new ShellResult("", "", 0, false),
    },
    challenge: { identity: "fixture", description: "solve it" },
    limits: limits(),
    runsRoot: tmp,
    monotonic: opts?.monotonic ?? (() => 0),
    utcNow: () => NOW,
    runId: "FA-20260814T161530Z-a13f4c2d",
  };
  if (!opts?.omitVerifier) base.verifier = verifier as never;
  const loop = new AgentLoop(base as never);
  const result = await loop.run();
  return { loop, result };
}

describe("descriptor optional expected_flag", () => {
  it("omitted is valid, present preserved without normalization", () => {
    const dir1 = makeChallengeDir({
      identity: "u",
      description: "d",
      network_mode: "none",
    });
    try {
      const ch = loadChallenge(dir1);
      expect(ch.expectedFlag).toBeUndefined();
    } finally {
      rmSync(join(dir1, ".."), { recursive: true, force: true });
    }
    const dir2 = makeChallengeDir({
      identity: "k",
      description: "d",
      expected_flag: "Flag{known}",
      network_mode: "none",
    });
    try {
      expect(loadChallenge(dir2).expectedFlag).toBe("Flag{known}");
    } finally {
      rmSync(join(dir2, ".."), { recursive: true, force: true });
    }
    const dir3 = makeChallengeDir({
      identity: "k",
      description: "d",
      expected_flag: "  Flag{spaced}  ",
      network_mode: "none",
    });
    try {
      expect(loadChallenge(dir3).expectedFlag).toBe("  Flag{spaced}  ");
    } finally {
      rmSync(join(dir3, ".."), { recursive: true, force: true });
    }
  });

  it("invalid present values rejected", () => {
    for (const bad of ["", "   ", null, 123]) {
      const dir = makeChallengeDir({
        identity: "x",
        description: "d",
        expected_flag: bad,
        network_mode: "none",
      });
      try {
        expect(() => loadChallenge(dir)).toThrow();
      } finally {
        rmSync(join(dir, ".."), { recursive: true, force: true });
      }
    }
  });
});

describe("verified behavior preserved", () => {
  it("wrong continues, correct terminates solved without candidate_flag", async () => {
    const tmp = mkdtempSync(join(tmpdir(), "flagagent-"));
    try {
      const { result } = await runLoop(
        tmp,
        [
          new ModelResponse("", [
            new ToolCall("w", "submit_flag", { candidate: "wrong" }),
            new ToolCall("s", "shell", { command: "after-wrong" }),
            new ToolCall("c", "submit_flag", { candidate: "Flag{ok}" }),
            new ToolCall("x", "shell", { command: "skipped" }),
          ]),
        ],
        new ExactStringVerifier("Flag{ok}"),
        { execute: async () => new ShellResult("ok", "", 0, false) },
      );
      expect(result["status:reason"]).toBe("solved:verified_flag");
      expect(result.status).toBe("solved");
      expect("candidate_flag" in result).toBe(false);
    } finally {
      rmSync(tmp, { recursive: true, force: true });
    }
  });
});

describe("unverified submission", () => {
  it("terminates submitted with normalized candidate and no verifier_result", async () => {
    const tmp = mkdtempSync(join(tmpdir(), "flagagent-"));
    try {
      const { loop, result } = await runLoop(
        tmp,
        [
          new ModelResponse("", [
            new ToolCall("c1", "submit_flag", { candidate: "  Flag{candidate}\n" }),
          ]),
        ],
        null,
      );
      expect(result["status:reason"]).toBe("submitted:unverified_flag");
      expect(result.status).toBe("submitted");
      expect(result.reason).toBe("unverified_flag");
      expect(result.candidate_flag).toBe("Flag{candidate}");
      expect(result.schema_version).toBe(1);
      expect("verifier_result" in result).toBe(false);
      const events = readEvents(
        (loop as unknown as { artifacts: { eventsPath: string } }).artifacts.eventsPath,
      );
      expect(events.filter((e) => e.type === "verifier_result").length).toBe(0);
      const subs = events.filter((e) => e.type === "flag_submission");
      expect(subs.length).toBe(1);
      expect((subs[0]!.payload as Record<string, unknown>).candidate).toBe(
        "Flag{candidate}",
      );
      const trs = events.filter((e) => e.type === "tool_result");
      expect(trs.length).toBe(1);
      expect((trs[0]!.payload as Record<string, unknown>).executed).toBe(true);
      expect((trs[0]!.payload as Record<string, unknown>).call_id).toBe("c1");
      const term = events.find((e) => e.type === "terminal_decision");
      expect((term!.payload as Record<string, unknown>).unprocessed_call_ids).toEqual(
        [],
      );
      expect(result.flag_submissions).toBe(1);
      expect(result.tool_calls).toBe(1);
    } finally {
      rmSync(tmp, { recursive: true, force: true });
    }
  });

  it("first valid unverified short-circuits same response", async () => {
    const tmp = mkdtempSync(join(tmpdir(), "flagagent-"));
    try {
      const executed: string[] = [];
      const { loop, result } = await runLoop(
        tmp,
        [
          new ModelResponse("", [
            new ToolCall("c1", "submit_flag", { candidate: " Flag{first} " }),
            new ToolCall("c2", "submit_flag", { candidate: "Flag{second}" }),
            new ToolCall("c3", "shell", { command: "echo hi" }),
          ]),
        ],
        null,
        {
          execute: async (cmd: string) => {
            executed.push(cmd);
            return new ShellResult("ok", "", 0, false);
          },
        },
      );
      expect(result["status:reason"]).toBe("submitted:unverified_flag");
      expect(result.candidate_flag).toBe("Flag{first}");
      expect(executed).toEqual([]);
      const events = readEvents(
        (loop as unknown as { artifacts: { eventsPath: string } }).artifacts.eventsPath,
      );
      expect(events.filter((e) => e.type === "tool_call").length).toBe(1);
      expect(events.filter((e) => e.type === "tool_result").length).toBe(1);
      expect(events.filter((e) => e.type === "flag_submission").length).toBe(1);
      const term = events.find((e) => e.type === "terminal_decision");
      expect((term!.payload as Record<string, unknown>).unprocessed_call_ids).toEqual([
        "c2",
        "c3",
      ]);
    } finally {
      rmSync(tmp, { recursive: true, force: true });
    }
  });

  it("no candidate leakage into unsolved or error", async () => {
    const tmp1 = mkdtempSync(join(tmpdir(), "flagagent-"));
    try {
      const { result } = await runLoop(
        tmp1,
        [
          new ModelResponse("", [
            new ToolCall("w", "submit_flag", { candidate: "Flag{bad}" }),
          ]),
          new ModelResponse("done"),
        ],
        new ExactStringVerifier("Flag{ok}"),
      );
      expect(result["status:reason"]).toBe("unsolved:model_stop");
      expect("candidate_flag" in result).toBe(false);
    } finally {
      rmSync(tmp1, { recursive: true, force: true });
    }
    const tmp2 = mkdtempSync(join(tmpdir(), "flagagent-"));
    try {
      const { result } = await runLoop(tmp2, [new ModelResponse("done")], null);
      expect(result["status:reason"]).toBe("unsolved:model_stop");
      expect("candidate_flag" in result).toBe(false);
    } finally {
      rmSync(tmp2, { recursive: true, force: true });
    }
    const tmp3 = mkdtempSync(join(tmpdir(), "flagagent-"));
    try {
      const { result } = await runLoop(tmp3, [new Error("boom")], null);
      expect(result["status:reason"]).toBe("error:provider_error");
      expect("candidate_flag" in result).toBe(false);
    } finally {
      rmSync(tmp3, { recursive: true, force: true });
    }
  });

  it("wall deadline retains precedence over unverified submission", async () => {
    const tmp = mkdtempSync(join(tmpdir(), "flagagent-"));
    try {
      let now = 0;
      const monotonic = () => now;
      const model = {
        lastCommittedAt: 0,
        setRemaining() {},
        setMonotonic() {},
        async generate(): Promise<ModelResponse> {
          now = 100;
          (this as unknown as { lastCommittedAt: number }).lastCommittedAt = 100;
          return new ModelResponse("", [
            new ToolCall("c1", "submit_flag", { candidate: "Flag{x}" }),
          ]);
        },
      };
      const loop = new AgentLoop({
        model: model as never,
        executor: { execute: async () => new ShellResult("ok", "", 0, false) } as never,
        verifier: null,
        challenge: { identity: "fixture", description: "test" },
        limits: new Limits({
          maxModelTurns: 5,
          wallTimeoutSeconds: 5,
          commandTimeoutSeconds: 10,
        }),
        runsRoot: tmp,
        monotonic,
        utcNow: () => NOW,
        runId: "FA-20260814T161530Z-a13f4c2d",
      });
      const result = await loop.run();
      expect(result["status:reason"]).toBe("unsolved:wall_limit");
      expect("candidate_flag" in result).toBe(false);
      const events = readEvents(
        (loop as unknown as { artifacts: { eventsPath: string } }).artifacts.eventsPath,
      );
      expect(events.filter((e) => e.type === "flag_submission").length).toBe(0);
    } finally {
      rmSync(tmp, { recursive: true, force: true });
    }
  });

  it("expected-flag isolation preserved", async () => {
    const tmp = mkdtempSync(join(tmpdir(), "flagagent-"));
    const SENTINEL = "Flag{SENTINEL_9f3a_isolation}";
    try {
      const model = new ScriptedModel([
        new ModelResponse("", [
          new ToolCall("c1", "submit_flag", { candidate: "Flag{wrong}" }),
        ]),
        new ModelResponse("done"),
      ]);
      const loop = new AgentLoop({
        model: model as never,
        executor: { execute: async () => new ShellResult("ok", "", 0, false) } as never,
        verifier: new ExactStringVerifier(SENTINEL),
        challenge: { identity: "iso", description: "solve it" },
        limits: limits(),
        runsRoot: tmp,
        monotonic: () => 0,
        utcNow: () => NOW,
        runId: "FA-20260814T161530Z-a13f4c2d",
      });
      const result = await loop.run();
      expect(result["status:reason"]).toBe("unsolved:model_stop");
      expect(JSON.stringify(model.calls)).not.toContain(SENTINEL);
      const runJson = JSON.parse(
        readFileSync(join(tmp, "FA-20260814T161530Z-a13f4c2d", "run.json"), "utf8"),
      );
      expect(JSON.stringify(runJson)).not.toContain(SENTINEL);
      const events = readEvents(
        join(tmp, "FA-20260814T161530Z-a13f4c2d", "events.jsonl"),
      );
      expect(JSON.stringify(events)).not.toContain(SENTINEL);
    } finally {
      rmSync(tmp, { recursive: true, force: true });
    }
  });

  it("runtime reachable without verifier via loader and loop", async () => {
    const dir = makeChallengeDir({
      identity: "u",
      description: "d",
      network_mode: "none",
    });
    try {
      const ch = loadChallenge(dir);
      expect(ch.expectedFlag).toBeUndefined();
      const verifier =
        ch.expectedFlag !== undefined ? new ExactStringVerifier(ch.expectedFlag) : null;
      expect(verifier).toBeNull();
      const tmp = mkdtempSync(join(tmpdir(), "flagagent-"));
      try {
        const { result } = await runLoop(
          tmp,
          [
            new ModelResponse("", [
              new ToolCall("c1", "submit_flag", { candidate: "Flag{x}" }),
            ]),
          ],
          verifier,
        );
        expect(result["status:reason"]).toBe("submitted:unverified_flag");
      } finally {
        rmSync(tmp, { recursive: true, force: true });
      }
      const tmp2 = mkdtempSync(join(tmpdir(), "flagagent-"));
      try {
        const { result } = await runLoop(
          tmp2,
          [
            new ModelResponse("", [
              new ToolCall("c1", "submit_flag", { candidate: "Flag{y}" }),
            ]),
          ],
          undefined,
          undefined,
          { omitVerifier: true },
        );
        expect(result["status:reason"]).toBe("submitted:unverified_flag");
        expect(result.candidate_flag).toBe("Flag{y}");
      } finally {
        rmSync(tmp2, { recursive: true, force: true });
      }
    } finally {
      rmSync(join(dir, ".."), { recursive: true, force: true });
    }
  });
});
