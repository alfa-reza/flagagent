import { describe, it, expect } from "vitest";
import { createServer } from "node:http";
import { mkdtempSync, rmSync, mkdirSync, writeFileSync, existsSync } from "node:fs";
import { execSync } from "node:child_process";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { AgentLoop } from "../src/flagagent/loop.js";
import { Limits } from "../src/flagagent/limits.js";
import { ExactStringVerifier } from "../src/flagagent/tools.js";
import { DockerExecutor } from "../src/flagagent/docker.js";
import { ChatCompletionsModel } from "../src/flagagent/providers/chat.js";
import { readEvents } from "../src/flagagent/artifacts.js";

const NOW = new Date("2026-08-14T16:15:30.000Z");

function chatToolResponse(callId: string, name: string, args: unknown): string {
  return JSON.stringify({
    id: "chatcmpl-1",
    object: "chat.completion",
    created: 0,
    model: "m",
    choices: [
      {
        index: 0,
        message: {
          role: "assistant",
          content: "",
          tool_calls: [
            {
              id: callId,
              type: "function",
              function: { name, arguments: JSON.stringify(args) },
            },
          ],
        },
        finish_reason: "tool_calls",
      },
    ],
    usage: { prompt_tokens: 2, completion_tokens: 2 },
  });
}

function chatStop(): string {
  return JSON.stringify({
    id: "chatcmpl-1",
    object: "chat.completion",
    created: 0,
    model: "m",
    choices: [
      {
        index: 0,
        message: { role: "assistant", content: "done" },
        finish_reason: "stop",
      },
    ],
    usage: { prompt_tokens: 1, completion_tokens: 1 },
  });
}

describe("provider → AgentLoop → Docker → verifier (direct construction, not CLI)", () => {
  it("staged file shell + incorrect flag continues + correct solves via real Docker", async () => {
    try {
      const out = execSync("docker info --format '{{.ServerVersion}}'", {
        encoding: "utf8",
      });
      void out;
    } catch {
      console.warn("Docker unavailable - skipping direct-construction smoke");
      return;
    }

    const tmp = mkdtempSync(join(tmpdir(), "flagagent-assembled-"));
    let server: import("node:http").Server | null = null;
    try {
      const src = join(tmp, "src");
      mkdirSync(src);
      writeFileSync(join(src, "secret.txt"), "hello-staged");

      // Local OpenAI-compatible fixture: sequence of responses
      // Turn1: shell cat secret.txt
      // Turn2: submit_flag wrong
      // Turn3: submit_flag correct
      let callIdx = 0;
      server = createServer((req, res) => {
        let body = "";
        req.on("data", (c) => (body += c));
        req.on("end", () => {
          callIdx++;
          let payload: string;
          if (callIdx === 1)
            payload = chatToolResponse("c1", "shell", { command: "cat secret.txt" });
          else if (callIdx === 2)
            payload = chatToolResponse("c2", "submit_flag", {
              candidate: "Flag{wrong}",
            });
          else if (callIdx === 3)
            payload = chatToolResponse("c3", "submit_flag", { candidate: "Flag{ok}" });
          else payload = chatStop();
          res.writeHead(200, { "Content-Type": "application/json" });
          res.end(payload);
        });
      });
      await new Promise<void>((r) => server!.listen(0, "127.0.0.1", r as never));
      const port = (server.address() as { port: number }).port;
      const base = `http://127.0.0.1:${port}`;

      const executor = new DockerExecutor({ networkMode: "none" });
      const model = new ChatCompletionsModel({
        model: "m",
        apiKey: "sk-test",
        baseURL: base,
      });

      const loop = new AgentLoop({
        model: model as never,
        executor: executor as never,
        verifier: new ExactStringVerifier("Flag{ok}"),
        challenge: { identity: "assembled", description: "solve it", sourceDir: src },
        limits: new Limits({
          maxModelTurns: 10,
          wallTimeoutSeconds: 30,
          commandTimeoutSeconds: 10,
        }),
        runsRoot: join(tmp, "runs"),
        monotonic: () => Number(process.hrtime.bigint()) / 1_000_000_000,
        utcNow: () => NOW,
        runId: "FA-20260814T161530Z-a13f4c2d",
      });

      const result = await loop.run();
      expect(result["status:reason"]).toBe("solved:verified_flag");
      const events = readEvents(
        (loop as unknown as { artifacts: { eventsPath: string } }).artifacts.eventsPath,
      );
      // shell observation
      const toolCalls = events.filter((e) => e.type === "tool_call");
      expect(
        toolCalls.some((e) => (e.payload as Record<string, unknown>).name === "shell"),
      ).toBe(true);
      const toolResults = events.filter((e) => e.type === "tool_result");
      expect(
        toolResults.some((e) => JSON.stringify(e.payload).includes("hello-staged")),
      ).toBe(true);
      // incorrect flag continues: at least two verifier_result, first incorrect
      const vrs = events.filter((e) => e.type === "verifier_result");
      expect(vrs.length).toBe(2);
      expect((vrs[0]!.payload as Record<string, unknown>).outcome).toBe("incorrect");
      expect((vrs[1]!.payload as Record<string, unknown>).outcome).toBe("correct");
      // writeup exists via loop artifacts? verify result.json exists
      const runDir = (loop as unknown as { artifacts: { directory: string } }).artifacts
        .directory;
      expect(existsSync(join(runDir, "result.json"))).toBe(true);
    } finally {
      if (server) server.close();
      rmSync(tmp, { recursive: true, force: true });
    }
  }, 60000);
});
