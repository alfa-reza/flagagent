/* eslint-disable no-useless-escape */
import { describe, it, expect } from "vitest";
import { createServer } from "node:http";
import { mkdtempSync, rmSync, mkdirSync, writeFileSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { AgentLoop } from "../src/flagagent/loop.js";
import { Limits } from "../src/flagagent/limits.js";
import { ExactStringVerifier, ShellResult } from "../src/flagagent/tools.js";
import { DockerExecutor } from "../src/flagagent/docker.js";

const NOW = new Date("2026-08-14T16:15:30.000Z");
const SENTINEL = "Flag{SENTINEL_EXPECTED_FLAG_9f3a}";

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
    usage: { prompt_tokens: 1, completion_tokens: 1 },
  });
}

describe("control-side expected_flag isolation", () => {
  it("initial provider request, workspace and run.json do not contain sentinel", async () => {
    const tmp = mkdtempSync(join(tmpdir(), "flagagent-flagiso-"));
    let server: import("node:http").Server | null = null;
    try {
      const src = join(tmp, "src");
      mkdirSync(src);
      writeFileSync(join(src, "data.txt"), "hello");

      const requestBodies: string[] = [];
      let callIdx = 0;
      server = createServer((req, res) => {
        let body = "";
        req.on("data", (c) => (body += c));
        req.on("end", () => {
          requestBodies.push(body);
          callIdx++;
          let payload: string;
          if (callIdx === 1) {
            payload = chatToolResponse("c1", "shell", { command: "cat data.txt" });
          } else {
            payload = JSON.stringify({
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
          res.writeHead(200, { "Content-Type": "application/json" });
          res.end(payload);
        });
      });
      await new Promise<void>((r) => server!.listen(0, "127.0.0.1", r as never));
      const port = (server.address() as { port: number }).port;
      const apiBase = `http://127.0.0.1:${port}`;

      const { ChatCompletionsModel } =
        await import("../src/flagagent/providers/chat.js");
      const model = new ChatCompletionsModel({
        model: "m",
        apiKey: "sk-test",
        baseURL: apiBase,
      });

      const loop = new AgentLoop({
        model: model as never,
        executor: {
          prepare: async (ws: string, runId: string) => {
            const realExec = new DockerExecutor({ networkMode: "none" });
            const wsReal = ws;
            void realExec;
            void wsReal;
            void runId;
          },
          execute: async (cmd: string) => {
            expect(cmd).not.toContain(SENTINEL);
            return new ShellResult("hello", "", 0, false);
          },
        } as never,
        verifier: new ExactStringVerifier(SENTINEL),
        challenge: { identity: "iso-test", description: "solve it", sourceDir: src },
        limits: new Limits({
          maxModelTurns: 3,
          wallTimeoutSeconds: 5,
          commandTimeoutSeconds: 5,
        }),
        runsRoot: join(tmp, "runs"),
        monotonic: () => Date.now() / 1000,
        utcNow: () => NOW,
        runId: "FA-20260814T161530Z-a13f4c2d",
      });

      const result = await loop.run();
      expect(result["status:reason"]).toBe("unsolved:model_stop");
      expect(requestBodies.length).toBeGreaterThan(0);
      expect(requestBodies[0]!).not.toContain(SENTINEL);
      const runJson = JSON.parse(
        readFileSync(
          join(tmp, "runs", "FA-20260814T161530Z-a13f4c2d", "run.json"),
          "utf8",
        ),
      );
      expect(JSON.stringify(runJson)).not.toContain(SENTINEL);
      expect(JSON.stringify(runJson.challenge ?? {})).not.toContain(SENTINEL);
      const wsDir = join(tmp, "runs", "FA-20260814T161530Z-a13f4c2d", "workspace");
      const { readdirSync, readFileSync: rfs } = await import("node:fs");
      const allEntries: string[] = readdirSync(wsDir, {
        recursive: true,
      } as unknown as string) as unknown as string[];
      for (const entry of allEntries) {
        const full = join(wsDir, entry as string);
        try {
          const stat = (await import("node:fs")).statSync(full);
          if (stat.isFile()) {
            const content = rfs(full, "utf8");
            expect(content).not.toContain(SENTINEL);
          }
        } catch (_e) {
          void _e;
        }
      }
      const wsData = (() => {
        try {
          return readFileSync(
            join(tmp, "runs", "FA-20260814T161530Z-a13f4c2d", "workspace", "data.txt"),
            "utf8",
          );
        } catch {
          return "";
        }
      })();
      expect(wsData).not.toContain(SENTINEL);
      expect(wsData).toBe("hello");
    } finally {
      if (server) server.close();
      rmSync(tmp, { recursive: true, force: true });
    }
  });

  it("fake Docker argv does not receive sentinel via control config", async () => {
    const fakeBin = mkdtempSync(join(tmpdir(), "flagagent-fake-flag-"));
    const origPath = process.env.PATH ?? "";
    const tmp = mkdtempSync(join(tmpdir(), "flagagent-flag-docker-"));
    try {
      const logFile = join(tmp, "docker-flag.log");
      const script = `#!/usr/bin/env bash
set -euo pipefail
LOG="\${FAKE_DOCKER_LOG:-/tmp/fake-docker.log}"
echo "docker $*" >> "\$LOG"
case "\$1" in
  context) if [ "\$2" = "show" ]; then echo "default"; else echo "unix:///var/run/docker.sock"; fi; exit 0;;
  run) echo "id-\$(date +%s%N)"; exit 0;;
  inspect) if [[ "\$*" == *".State.Running"* ]]; then echo "true"; exit 0; fi; if [[ "\$*" == *".Image"* ]]; then echo "sha256:abc123"; exit 0; fi; echo "true"; exit 0;;
  exec) echo "hello"; exit 0;;
  kill|start|rm|ps) exit 0;;
  network) if [ "\$2" = "create" ]; then echo "net-1"; exit 0; fi; if [ "\$2" = "ls" ]; then exit 0; fi; if [ "\$2" = "rm" ]; then exit 0; fi;;
  *) exit 0;;
esac
`;
      const { writeFileSync: wfs, chmodSync } = await import("node:fs");
      wfs(join(fakeBin, "docker"), script, { mode: 0o755 });
      chmodSync(join(fakeBin, "docker"), 0o755);
      process.env.PATH = `${fakeBin}:${origPath}`;
      process.env.FAKE_DOCKER_LOG = logFile;
      const ex = new DockerExecutor({ networkMode: "none" });
      const ws = mkdtempSync(join(tmp, "ws-"));
      await ex.prepare(ws, "FA-20260814T000000Z-a13f4c2d");
      await ex.execute("echo hi", 5);
      const log = readFileSync(logFile, "utf8");
      expect(log).not.toContain(SENTINEL);
      expect(log).toContain("flagagent.managed=true");
      delete process.env.FAKE_DOCKER_LOG;
      process.env.PATH = origPath;
      await ex.cleanup("FA-20260814T000000Z-a13f4c2d");
    } finally {
      process.env.PATH = origPath;
      delete process.env.FAKE_DOCKER_LOG;
      rmSync(tmp, { recursive: true, force: true });
      rmSync(fakeBin, { recursive: true, force: true });
    }
  });
});
