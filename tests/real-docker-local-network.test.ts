import { describe, it, expect, beforeAll } from "vitest";
import { createServer } from "node:http";
import { mkdtempSync, rmSync, mkdirSync, writeFileSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { spawn, execSync } from "node:child_process";

function requireDockerLocalOrFail(): void {
  try {
    execSync("docker info --format '{{.ServerVersion}}'", { stdio: "ignore" });
  } catch {
    throw new Error("Docker unavailable - real-Docker gate requires Docker Engine");
  }
  try {
    execSync("docker image inspect flagagent-sandbox:dev --format '{{.Id}}'", {
      stdio: "ignore",
    });
  } catch {
    throw new Error("Missing image flagagent-sandbox:dev - build it first");
  }
  try {
    execSync("docker image inspect flagagent-target:dev --format '{{.Id}}'", {
      stdio: "ignore",
    });
  } catch {
    throw new Error("Missing image flagagent-target:dev - build it first");
  }
  if (process.getuid?.() === 0) {
    throw new Error("Running as root unsupported");
  }
}

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

describe("real-Docker local network: installed flagagent → local target → verifier", () => {
  beforeAll(() => {
    requireDockerLocalOrFail();
  });

  it("agent reaches target:9999 via internal network, derives flag, solves", async () => {
    const tmpRoot = mkdtempSync(join(tmpdir(), "flagagent-real-local-"));
    let server: import("node:http").Server | null = null;
    let packTmp: string | null = null;
    let runDir: string | null = null;
    try {
      const challengeDir = join(tmpRoot, "challenge");
      mkdirSync(challengeDir, { recursive: true });
      writeFileSync(
        join(challengeDir, "challenge.json"),
        JSON.stringify({
          identity: "real-local-integration",
          description:
            "Connect to target:9999, read marker flagagent-target-ok, strip -ok, replace hyphens with underscores, wrap as Flag{...}, submit via submit_flag",
          expected_flag: "Flag{flagagent_target}",
          network_mode: "local",
          target_context:
            "The audited local target is reachable at target:9999. It returns one deterministic marker per connection.",
        }),
      );

      let callIdx = 0;
      server = createServer((req, res) => {
        let body = "";
        req.on("data", (c) => (body += c));
        req.on("end", () => {
          callIdx++;
          let payload: string;
          if (callIdx === 1) {
            payload = chatToolResponse("c1", "shell", {
              command:
                "python3 -c \"import socket; s=socket.create_connection(('target',9999),2); print(s.recv(64).decode().strip())\"",
            });
          } else if (callIdx === 2) {
            payload = chatToolResponse("c2", "submit_flag", {
              candidate: "Flag{flagagent_target}",
            });
          } else payload = chatStop();
          res.writeHead(200, { "Content-Type": "application/json" });
          res.end(payload);
        });
      });
      await new Promise<void>((r) => server!.listen(0, "127.0.0.1", r as never));
      const port = (server.address() as { port: number }).port;
      const apiBase = `http://127.0.0.1:${port}`;

      execSync("npm run build", { cwd: process.cwd(), stdio: "ignore" });
      const packOut = execSync("npm pack", { cwd: process.cwd(), encoding: "utf8" });
      const tgzName = packOut.trim().split("\n").pop()!.trim();
      const tgzPath = join(process.cwd(), tgzName);
      packTmp = mkdtempSync(join(tmpdir(), "flagagent-pack-local-"));
      const projDir = join(packTmp, "proj");
      mkdirSync(projDir);
      writeFileSync(
        join(projDir, "package.json"),
        JSON.stringify({ name: "tmp-proj", version: "0.0.0" }),
      );
      execSync(`npm install ${tgzPath} --silent`, { cwd: projDir, stdio: "ignore" });
      const installedBin = join(projDir, "node_modules", ".bin", "flagagent");

      const runsRoot = join(tmpRoot, "runs");
      const runResult = await new Promise<{
        status: number | null;
        stdout: string;
        stderr: string;
        error?: Error;
      }>((resolve) => {
        const child = spawn(
          installedBin,
          [
            "run",
            "--challenge",
            challengeDir,
            "--protocol",
            "openai-chat",
            "--model",
            "m",
            "--api-base",
            apiBase,
            "--runs-root",
            runsRoot,
          ],
          {
            env: { ...process.env, OPENAI_API_KEY: "sk-test-dummy" },
          },
        );
        let stdout = "";
        let stderr = "";
        child.stdout?.on("data", (d: Buffer) => (stdout += d.toString()));
        child.stderr?.on("data", (d: Buffer) => (stderr += d.toString()));
        const t = setTimeout(() => {
          try {
            child.kill("SIGKILL");
          } catch (_e) {
            void _e;
          }
          resolve({
            status: null,
            stdout,
            stderr,
            error: new Error("CLI timeout 120s"),
          });
        }, 120000);
        child.on("close", (code) => {
          clearTimeout(t);
          resolve({ status: code, stdout, stderr });
        });
        child.on("error", (e) => {
          clearTimeout(t);
          resolve({ status: null, stdout, stderr, error: e });
        });
      });

      if (runResult.status !== 0) {
        console.error("STDOUT", runResult.stdout.slice(0, 3000));
        console.error("STDERR", runResult.stderr.slice(0, 3000));
        console.error("ERROR", runResult.error);
      }
      expect(runResult.status).toBe(0);
      expect(runResult.stdout).toContain("solved:verified_flag");
      const m = runResult.stdout.match(/run=([^\n]+)/);
      expect(m).not.toBeNull();
      runDir = m![1]!.trim();
      const resultJson = JSON.parse(readFileSync(join(runDir, "result.json"), "utf8"));
      expect(resultJson["status:reason"]).toBe("solved:verified_flag");
      const events = readFileSync(join(runDir, "events.jsonl"), "utf8")
        .split("\n")
        .filter(Boolean)
        .map((l) => JSON.parse(l));
      const toolResults = events.filter(
        (e: { type: string }) => e.type === "tool_result",
      );
      expect(
        toolResults.some((e: { payload: { result?: { stdout?: string } } }) =>
          JSON.stringify(e.payload).includes("flagagent-target-ok"),
        ),
      ).toBe(true);

      const lifecycle = events.find(
        (e: { type: string }) => e.type === "sandbox_lifecycle",
      );
      expect(lifecycle).toBeDefined();
      const lifePayload = (lifecycle as { payload: Record<string, unknown> }).payload;
      expect(lifePayload.agent_container_id).toBeDefined();
      expect(lifePayload.network_id).toBeDefined();
      expect(lifePayload.target_container_id).toBeDefined();

      try {
        execSync(`rm -f ${tgzPath}`);
      } catch {
        // ignore
      }
    } finally {
      if (server) server.close();
      if (packTmp) rmSync(packTmp, { recursive: true, force: true });
      rmSync(tmpRoot, { recursive: true, force: true });
    }
    if (runDir) {
      const runId = runDir.split("/").pop()!;
      const containers = execSync(
        `docker ps -a --filter "label=flagagent.run_id=${runId}" --format "{{.ID}}"`,
        { encoding: "utf8" },
      ).trim();
      const networks = execSync(
        `docker network ls --filter "label=flagagent.run_id=${runId}" --format "{{.ID}}"`,
        { encoding: "utf8" },
      ).trim();
      expect(containers).toBe("");
      expect(networks).toBe("");
    }
  }, 150000);
});
