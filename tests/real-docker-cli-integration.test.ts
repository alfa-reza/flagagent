import { describe, it, expect, beforeAll } from "vitest";
import { createServer } from "node:http";
import {
  mkdtempSync,
  rmSync,
  mkdirSync,
  writeFileSync,
  readFileSync,
  existsSync,
} from "node:fs";
import { join, resolve, isAbsolute } from "node:path";
import { tmpdir } from "node:os";
import { spawn, execSync } from "node:child_process";

function requireDockerOrFail(): void {
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
    throw new Error(
      "Missing image flagagent-sandbox:dev - run: docker build -t flagagent-sandbox:dev -f images/sandbox/Dockerfile .",
    );
  }
  if (process.getuid?.() === 0) {
    throw new Error(
      "Running as root unsupported - real-Docker gate requires non-root user (uid != 0) with docker group",
    );
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

async function runPackedCli(
  installedBin: string,
  args: string[],
  env: Record<string, string | undefined>,
  timeoutMs = 90000,
): Promise<{ status: number | null; stdout: string; stderr: string; error?: Error }> {
  return await new Promise((resolvePromise) => {
    const child = spawn(installedBin, args, { env: env as NodeJS.ProcessEnv });
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
      resolvePromise({
        status: null,
        stdout,
        stderr,
        error: new Error("CLI timeout 90s"),
      });
    }, timeoutMs);
    child.on("close", (code: number | null) => {
      clearTimeout(t);
      resolvePromise({ status: code, stdout, stderr });
    });
    child.on("error", (e: Error) => {
      clearTimeout(t);
      resolvePromise({ status: null, stdout, stderr, error: e });
    });
  });
}

function buildPackedCli(tgzPath: string, packTmp: string): string {
  const projDir = join(packTmp, "proj");
  mkdirSync(projDir);
  writeFileSync(
    join(projDir, "package.json"),
    JSON.stringify({ name: "tmp-proj", version: "0.0.0" }),
  );
  execSync(`npm install ${tgzPath} --silent`, { cwd: projDir, stdio: "ignore" });
  const installedBin = join(projDir, "node_modules", ".bin", "flagagent");
  if (!existsSync(installedBin)) throw new Error("installed flagagent bin missing");
  return installedBin;
}

function resolveRunDir(stdout: string, childCwd: string | null): string {
  const m = stdout.match(/run=([^\n]+)/);
  if (!m) throw new Error("missing run= line in CLI stdout");
  const printed = m[1]!.trim();
  if (isAbsolute(printed)) return printed;
  if (childCwd) return resolve(childCwd, printed);
  return resolve(printed);
}

describe("real-Docker packed CLI integration: installed flagagent → provider → Docker → verifier", () => {
  beforeAll(() => {
    requireDockerOrFail();
  });

  it("staged file shell + wrong flag continues + correct solves via installed CLI", async () => {
    const tmpRoot = mkdtempSync(join(tmpdir(), "flagagent-real-cli-"));
    let server: import("node:http").Server | null = null;
    let packTmp: string | null = null;
    try {
      const challengeDir = join(tmpRoot, "challenge");
      mkdirSync(challengeDir, { recursive: true });
      const filesDir = join(challengeDir, "files");
      mkdirSync(filesDir);
      writeFileSync(join(filesDir, "secret.txt"), "hello-staged");
      writeFileSync(
        join(challengeDir, "challenge.json"),
        JSON.stringify({
          identity: "real-cli-integration",
          description: "solve it - cat the staged file then submit the flag",
          expected_flag: "Flag{ok}",
          network_mode: "none",
        }),
      );

      let callIdx = 0;
      let lastRequestBody = "";
      server = createServer((req, res) => {
        let body = "";
        req.on("data", (c) => (body += c));
        req.on("end", () => {
          lastRequestBody = body;
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
      const apiBase = `http://127.0.0.1:${port}`;

      execSync("npm run build", { cwd: process.cwd(), stdio: "ignore" });
      const packOut = execSync("npm pack", { cwd: process.cwd(), encoding: "utf8" });
      const tgzName = packOut.trim().split("\n").pop()!.trim();
      const tgzPath = join(process.cwd(), tgzName);
      packTmp = mkdtempSync(join(tmpdir(), "flagagent-pack-install-"));
      const installedBin = buildPackedCli(tgzPath, packTmp);
      expect(existsSync(installedBin)).toBe(true);

      const runsRoot = join(tmpRoot, "runs");
      const runResult = await runPackedCli(
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
        { ...process.env, OPENAI_API_KEY: "sk-test-dummy" },
      );

      if (runResult.status !== 0) {
        console.error("CLI stdout:", runResult.stdout?.slice(0, 2000));
        console.error("CLI stderr:", runResult.stderr?.slice(0, 2000));
        console.error("CLI error:", runResult.error);
      }
      expect(runResult.status).toBe(0);
      expect(runResult.stdout).toContain("solved:verified_flag");
      const runDir = resolveRunDir(runResult.stdout, null);
      expect(existsSync(join(runDir, "result.json"))).toBe(true);
      expect(existsSync(join(runDir, "events.jsonl"))).toBe(true);
      expect(existsSync(join(runDir, "writeup.md"))).toBe(true);

      const resultJson = JSON.parse(readFileSync(join(runDir, "result.json"), "utf8"));
      expect(resultJson["status:reason"]).toBe("solved:verified_flag");

      const eventsRaw = readFileSync(join(runDir, "events.jsonl"), "utf8");
      const events = eventsRaw
        .split("\n")
        .filter(Boolean)
        .map((l) => JSON.parse(l));
      expect(events.some((e: { type: string }) => e.type === "tool_call")).toBe(true);
      expect(
        events.some(
          (e: { type: string; payload: { name?: string } }) =>
            e.type === "tool_call" && e.payload.name === "shell",
        ),
      ).toBe(true);
      expect(
        events.filter((e: { type: string }) => e.type === "verifier_result").length,
      ).toBe(2);
      const vrs = events.filter((e: { type: string }) => e.type === "verifier_result");
      expect((vrs[0] as { payload: { outcome: string } }).payload.outcome).toBe(
        "incorrect",
      );
      expect((vrs[1] as { payload: { outcome: string } }).payload.outcome).toBe(
        "correct",
      );

      const toolResults = events.filter(
        (e: { type: string }) => e.type === "tool_result",
      );
      expect(
        toolResults.some((e: { payload: { result?: { stdout?: string } } }) =>
          JSON.stringify(e.payload).includes("hello-staged"),
        ),
      ).toBe(true);

      expect(lastRequestBody).toContain("cat secret.txt");
      void "Flag{ok}";
      void "Flag{wrong}";

      const dockerLog =
        eventsRaw +
        JSON.stringify(runResult.stdout) +
        JSON.stringify(runResult.stderr ?? "");
      void dockerLog;

      const runMeta = JSON.parse(readFileSync(join(runDir, "run.json"), "utf8"));
      expect(JSON.stringify(runMeta)).not.toContain("Flag{ok}");
      expect(events.some((e: { type: string }) => e.type === "terminal_decision")).toBe(
        true,
      );
      expect(resultJson["status:reason"]).toBe("solved:verified_flag");

      const workspaceSecret = join(runDir, "workspace", "secret.txt");
      if (existsSync(workspaceSecret)) {
        expect(readFileSync(workspaceSecret, "utf8")).toBe("hello-staged");
      }

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
  }, 120000);

  it("default relative runs/ succeeds without explicit --runs-root", async () => {
    const tmpRoot = mkdtempSync(join(tmpdir(), "flagagent-real-cli-default-"));
    let server: import("node:http").Server | null = null;
    let packTmp: string | null = null;
    try {
      const challengeDir = join(tmpRoot, "challenge");
      mkdirSync(challengeDir, { recursive: true });
      const filesDir = join(challengeDir, "files");
      mkdirSync(filesDir);
      writeFileSync(join(filesDir, "secret.txt"), "hello-default");
      writeFileSync(
        join(challengeDir, "challenge.json"),
        JSON.stringify({
          identity: "real-cli-default",
          description: "solve it - cat the staged file then submit the flag",
          expected_flag: "Flag{default-ok}",
          network_mode: "none",
        }),
      );

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
              candidate: "Flag{default-ok}",
            });
          else payload = chatStop();
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
      packTmp = mkdtempSync(join(tmpdir(), "flagagent-pack-install-default-"));
      const installedBin = buildPackedCli(tgzPath, packTmp);

      const childCwd = mkdtempSync(join(tmpdir(), "flagagent-child-cwd-"));
      const runResult = await new Promise<{
        status: number | null;
        stdout: string;
        stderr: string;
        error?: Error;
      }>((resolvePromise) => {
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
          ],
          {
            cwd: childCwd,
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
          resolvePromise({
            status: null,
            stdout,
            stderr,
            error: new Error("CLI timeout 90s"),
          });
        }, 90000);
        child.on("close", (code: number | null) => {
          clearTimeout(t);
          resolvePromise({ status: code, stdout, stderr });
        });
        child.on("error", (e: Error) => {
          clearTimeout(t);
          resolvePromise({ status: null, stdout, stderr, error: e });
        });
      });

      if (runResult.status !== 0) {
        console.error("CLI stdout:", runResult.stdout?.slice(0, 2000));
        console.error("CLI stderr:", runResult.stderr?.slice(0, 2000));
        console.error("CLI error:", runResult.error);
      }
      expect(runResult.status).toBe(0);
      expect(runResult.stdout).toContain("solved:verified_flag");
      expect(runResult.stdout).toMatch(/run=runs\//);
      const runDir = resolveRunDir(runResult.stdout, childCwd);
      expect(isAbsolute(runDir)).toBe(true);
      expect(runDir.startsWith(resolve(childCwd))).toBe(true);
      expect(existsSync(join(runDir, "result.json"))).toBe(true);
      expect(existsSync(join(runDir, "events.jsonl"))).toBe(true);
      expect(existsSync(join(runDir, "writeup.md"))).toBe(true);
      expect(existsSync(join(runDir, "run.json"))).toBe(true);
      const resultJson = JSON.parse(readFileSync(join(runDir, "result.json"), "utf8"));
      expect(resultJson["status:reason"]).toBe("solved:verified_flag");
      expect(existsSync(join(childCwd, "runs"))).toBe(true);
      try {
        execSync(`rm -f ${tgzPath}`);
      } catch {
        // ignore
      }
      rmSync(childCwd, { recursive: true, force: true });
    } finally {
      if (server) server.close();
      if (packTmp) rmSync(packTmp, { recursive: true, force: true });
      rmSync(tmpRoot, { recursive: true, force: true });
    }
  }, 120000);
});
