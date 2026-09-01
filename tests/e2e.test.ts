import { describe, it, expect } from "vitest";
import { mkdtempSync, rmSync, mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { AgentLoop } from "../src/flagagent/loop.js";
import { Limits } from "../src/flagagent/limits.js";
import { ExactStringVerifier, ShellResult } from "../src/flagagent/tools.js";
import { ScriptedModel, ModelResponse, ToolCall } from "../src/flagagent/model.js";
import { readEvents } from "../src/flagagent/artifacts.js";

const NOW = new Date("2026-08-14T16:15:30.000Z");

describe("e2e smoke: CLI/challenge → loop → shell → submit_flag → verifier → artifacts", () => {
  it("deterministic path with scripted model and fake executor", async () => {
    const tmp = mkdtempSync(join(tmpdir(), "flagagent-e2e-"));
    try {
      const src = join(tmp, "src");
      mkdirSync(src);
      writeFileSync(join(src, "data.txt"), "hello");
      const executor = {
        prepare: async (ws: string) => {
          writeFileSync(join(ws, "prepared"), "ok");
        },
        execute: async (cmd: string) => new ShellResult(`ran ${cmd}`, "", 0, false),
        cleanup: async () => {},
      };
      const loop = new AgentLoop({
        model: new ScriptedModel([
          new ModelResponse("", [
            new ToolCall("c1", "shell", { command: "cat data.txt" }),
          ]),
          new ModelResponse("", [
            new ToolCall("c2", "submit_flag", { candidate: "Flag{ok}" }),
          ]),
        ]) as never,
        executor: executor as never,
        verifier: new ExactStringVerifier("Flag{ok}"),
        challenge: { identity: "e2e", description: "solve it", sourceDir: src },
        limits: new Limits({
          maxModelTurns: 5,
          wallTimeoutSeconds: 5,
          commandTimeoutSeconds: 10,
        }),
        runsRoot: join(tmp, "runs"),
        monotonic: () => Date.now() / 1000,
        utcNow: () => NOW,
        runId: "FA-20260814T161530Z-a13f4c2d",
      });
      const result = await loop.run();
      expect(result["status:reason"]).toBe("solved:verified_flag");
      const events = readEvents(
        (loop as unknown as { artifacts: { eventsPath: string } }).artifacts.eventsPath,
      );
      expect(events.some((e) => e.type === "tool_call")).toBe(true);
      expect(events.some((e) => e.type === "verifier_result")).toBe(true);
    } finally {
      rmSync(tmp, { recursive: true, force: true });
    }
  });
});
