import { spawn } from "node:child_process";
import { SandboxError, ShellResult } from "./tools.js";
import { validateRunId } from "./artifacts.js";

const SANDBOX_IMAGE = "flagagent-sandbox:dev";
const TARGET_IMAGE = "flagagent-target:dev";
const WORKSPACE_TARGET = "/workspace";
const OUTPUT_LIMIT_BYTES = 64 * 1024;
const OUTPUT_SUFFIX_LIMIT_BYTES = OUTPUT_LIMIT_BYTES * 2;
const EXEC_REAP_TIMEOUT_MS = 1000;

interface DockerResult {
  stdout: string;
  stderr: string;
  status: number | null;
  timedOut: boolean;
  truncated: boolean;
  error: Error | null;
}

function monotonicNow(): number {
  return Number(process.hrtime.bigint()) / 1_000_000_000;
}

function runtimeUser(): string {
  return `${process.getuid?.() ?? 1000}:${process.getgid?.() ?? 1000}`;
}

function asError(error: unknown): Error {
  return error instanceof Error ? error : new Error(String(error));
}

function resultDetail(result: DockerResult): string {
  return result.stderr.trim() || result.error?.message || "unknown error";
}

class BoundedOutput {
  private prefix = Buffer.alloc(0);
  private suffix = Buffer.alloc(0);
  private observed = 0;

  truncated = false;

  append(chunk: Buffer | string): void {
    const data = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    this.observed += data.length;

    if (this.prefix.length < OUTPUT_LIMIT_BYTES) {
      const prefixBytes = Math.min(
        OUTPUT_LIMIT_BYTES - this.prefix.length,
        data.length,
      );
      if (prefixBytes > 0) {
        this.prefix = Buffer.concat([this.prefix, data.subarray(0, prefixBytes)]);
      }
    }

    if (this.observed > OUTPUT_LIMIT_BYTES) this.truncated = true;

    this.suffix = Buffer.concat([this.suffix, data]);
    if (this.suffix.length > OUTPUT_SUFFIX_LIMIT_BYTES) {
      this.suffix = this.suffix.subarray(
        this.suffix.length - OUTPUT_SUFFIX_LIMIT_BYTES,
      );
    }
  }

  view(): string {
    const data = this.truncated
      ? Buffer.concat([this.prefix, this.suffix])
      : this.prefix;
    return data.toString("utf8");
  }
}

/**
 * Run one Docker CLI operation without blocking the event loop.
 *
 * stdout and stderr are always put into flowing mode and retain only a
 * bounded prefix plus a rolling suffix.  A timeout kills the CLI client and
 * waits briefly for its close event so any already-delivered output is still
 * included in the returned view.
 */
async function runDocker(args: string[], timeoutMs = 30000): Promise<DockerResult> {
  const child = spawn("docker", args, {
    stdio: ["ignore", "pipe", "pipe"],
  });

  const stdout = new BoundedOutput();
  const stderr = new BoundedOutput();
  let exitCode: number | null = null;
  let childError: Error | null = null;
  let completed = false;

  const snapshot = (status: number | null, timedOut: boolean): DockerResult => ({
    stdout: stdout.view(),
    stderr: stderr.view(),
    status,
    timedOut,
    truncated: stdout.truncated || stderr.truncated,
    error: childError,
  });

  const completion = new Promise<DockerResult>((resolve) => {
    const finish = (status: number | null): void => {
      if (completed) return;
      completed = true;
      resolve(snapshot(status, false));
    };

    child.stdout?.on("data", (chunk: Buffer | string) => stdout.append(chunk));
    child.stderr?.on("data", (chunk: Buffer | string) => stderr.append(chunk));
    child.stdout?.resume();
    child.stderr?.resume();

    child.once("exit", (code) => {
      exitCode = typeof code === "number" ? code : null;
    });
    child.once("error", (error) => {
      childError = asError(error);
      finish(null);
    });
    child.once("close", (code) => {
      finish(typeof code === "number" ? code : exitCode);
    });
  });

  let timeoutHandle: ReturnType<typeof setTimeout> | undefined;
  const timeout = new Promise<DockerResult>((resolve) => {
    timeoutHandle = setTimeout(
      () => {
        try {
          child.kill("SIGKILL");
        } catch {
          // The close/error handlers still own the normal completion path.
        }
        resolve(snapshot(null, true));
      },
      Math.max(0, timeoutMs),
    );
  });

  const first = await Promise.race([completion, timeout]);
  if (!first.timedOut) {
    if (timeoutHandle !== undefined) clearTimeout(timeoutHandle);
    return first;
  }

  let reapHandle: ReturnType<typeof setTimeout> | undefined;
  const reaped = await Promise.race([
    completion,
    new Promise<null>((resolve) => {
      reapHandle = setTimeout(() => resolve(null), EXEC_REAP_TIMEOUT_MS);
    }),
  ]);
  if (reapHandle !== undefined) clearTimeout(reapHandle);

  if (reaped !== null) {
    return {
      ...reaped,
      status: null,
      timedOut: true,
    };
  }
  // The client did not close within the bounded reap window.  The Docker
  // operation itself has still timed out; later close events cannot block the
  // caller or reset its deadline.
  return first;
}

export class DockerExecutor {
  private containerId: string | null = null;
  private containerName: string | null = null;
  private networkId: string | null = null;
  private networkName: string | null = null;
  private targetId: string | null = null;
  private targetName: string | null = null;
  private executionDeadline: number | null = null;
  private preparationDeadline: number | null = null;
  private preparationRemaining: number | null = null;
  private clock: () => number = monotonicNow;

  constructor(private opts: { image?: string; networkMode?: string } = {}) {
    this.opts.networkMode ??= "none";
    if (!["none", "local"].includes(this.opts.networkMode)) {
      throw new Error(`unsupported network_mode ${this.opts.networkMode}`);
    }
  }

  setRemaining(remaining: number): void {
    this.preparationRemaining = Math.max(0, remaining);
  }

  setExecutionDeadline(deadline: number, monotonic: () => number): void {
    this.executionDeadline = deadline;
    this.clock = monotonic;
  }

  private preparationTimeout(fixed: number): number {
    if (this.preparationDeadline == null) return fixed;
    const remaining = this.preparationDeadline - this.clock();
    if (remaining <= 0) throw new SandboxError("preparation budget exhausted");
    return Math.min(fixed, remaining);
  }

  private executionTimeout(fixed: number): number {
    if (this.executionDeadline == null) return fixed;
    const remaining = this.executionDeadline - this.clock();
    if (remaining <= 0) throw new SandboxError("execution budget exhausted");
    return Math.min(fixed, remaining);
  }

  private validateEndpoint(): void {
    const host = process.env.DOCKER_HOST?.trim() ?? "";
    const ctx = process.env.DOCKER_CONTEXT?.trim() ?? "";
    if (
      ctx ||
      (host &&
        !host.startsWith("unix://") &&
        !host.startsWith("npipe://") &&
        !host.startsWith("fd://") &&
        !host.startsWith("/"))
    ) {
      throw new SandboxError(`unsupported remote Docker endpoint ${host || ctx}`);
    }
  }

  async prepare(workspace: string, runId: string): Promise<void> {
    if (this.containerId) throw new SandboxError("already prepared");
    validateRunId(runId);
    if (process.getuid?.() === 0) throw new SandboxError("running as root unsupported");

    this.containerName = `flagagent-agent-${runId}`;
    if (this.preparationRemaining != null) {
      const remaining = this.preparationRemaining;
      this.preparationRemaining = null;
      if (remaining <= 0) throw new SandboxError("preparation budget exhausted");
      this.preparationDeadline = this.clock() + remaining;
    }

    try {
      this.validateEndpoint();
      if (this.opts.networkMode === "local") {
        await this.prepareLocal(workspace, runId);
      } else {
        await this.prepareNone(workspace, runId);
      }
    } finally {
      this.preparationDeadline = null;
    }
  }

  private async prepareNone(workspace: string, runId: string): Promise<void> {
    try {
      await this.createAgent(workspace, runId);
    } catch (error) {
      if (
        this.preparationDeadline != null &&
        this.clock() >= this.preparationDeadline
      ) {
        throw error;
      }
      await this.removeOwned();
      throw error;
    }
  }

  private async prepareLocal(workspace: string, runId: string): Promise<void> {
    this.networkName = `flagagent-net-${runId}`;
    this.targetName = `flagagent-target-${runId}`;
    try {
      await this.createNetwork(runId);
      await this.createTarget(runId);
      await this.waitTargetReady();
      await this.createAgent(workspace, runId);
    } catch (error) {
      if (
        this.preparationDeadline != null &&
        this.clock() >= this.preparationDeadline
      ) {
        throw error;
      }
      await this.removeOwned();
      throw error;
    }
  }

  private async createAgent(workspace: string, runId: string): Promise<void> {
    const args = [
      "run",
      "-d",
      "--name",
      this.containerName!,
      "--init",
      "--user",
      runtimeUser(),
      "-w",
      WORKSPACE_TARGET,
      "--mount",
      `type=bind,source=${workspace},target=${WORKSPACE_TARGET}`,
      "--memory",
      "2g",
      "--cpus",
      "2",
      "--pids-limit",
      "256",
      "--security-opt",
      "no-new-privileges",
      "--cap-drop",
      "ALL",
      "--network",
      this.opts.networkMode === "local" ? this.networkName! : "none",
      "--label",
      "flagagent.managed=true",
      "--label",
      `flagagent.run_id=${runId}`,
      "--label",
      "flagagent.role=agent",
      "--label",
      "flagagent.version=0.1.1",
      this.opts.image ?? SANDBOX_IMAGE,
      "sleep",
      "infinity",
    ];
    const result = await runDocker(args, this.preparationTimeout(60) * 1000);
    if (result.timedOut) throw new SandboxError("docker run timed out");
    if (result.error)
      throw new SandboxError(`docker run failed: ${resultDetail(result)}`);
    if (result.status !== 0) {
      throw new SandboxError(`docker run failed: ${resultDetail(result)}`);
    }
    const id = result.stdout.trim();
    if (!id) throw new SandboxError("docker run returned no id");
    this.containerId = id;
    if (!(await this.isContainerRunning(this.containerId))) {
      await this.forceRemove(this.containerId);
      this.containerId = null;
      throw new SandboxError("agent container not running");
    }
  }

  private async createNetwork(runId: string): Promise<void> {
    const args = [
      "network",
      "create",
      "--driver",
      "bridge",
      "--internal",
      "--label",
      "flagagent.managed=true",
      "--label",
      `flagagent.run_id=${runId}`,
      "--label",
      "flagagent.role=network",
      "--label",
      "flagagent.version=0.1.1",
      this.networkName!,
    ];
    const result = await runDocker(args, this.preparationTimeout(30) * 1000);
    if (result.timedOut) {
      throw new SandboxError("docker network create timed out");
    }
    if (result.error) {
      throw new SandboxError(`docker network create failed: ${resultDetail(result)}`);
    }
    if (result.status !== 0) {
      throw new SandboxError(`docker network create failed: ${resultDetail(result)}`);
    }
    const id = result.stdout.trim();
    if (!id) throw new SandboxError("docker network no id");
    this.networkId = id;
  }

  private async createTarget(runId: string): Promise<void> {
    const args = [
      "run",
      "-d",
      "--name",
      this.targetName!,
      "--network",
      this.networkName!,
      "--network-alias",
      "target",
      "--user",
      "target",
      "--init",
      "--memory",
      "256m",
      "--cpus",
      "0.5",
      "--pids-limit",
      "64",
      "--security-opt",
      "no-new-privileges",
      "--cap-drop",
      "ALL",
      "--label",
      "flagagent.managed=true",
      "--label",
      `flagagent.run_id=${runId}`,
      "--label",
      "flagagent.role=target",
      "--label",
      "flagagent.version=0.1.1",
      TARGET_IMAGE,
    ];
    const result = await runDocker(args, this.preparationTimeout(60) * 1000);
    if (result.timedOut) throw new SandboxError("docker target run timed out");
    if (result.error) {
      throw new SandboxError(`docker target run failed: ${resultDetail(result)}`);
    }
    if (result.status !== 0) {
      throw new SandboxError(`docker target run failed: ${resultDetail(result)}`);
    }
    const id = result.stdout.trim();
    if (!id) throw new SandboxError("target no id");
    this.targetId = id;
  }

  private async waitTargetReady(): Promise<void> {
    for (let attempt = 0; attempt < 30; attempt += 1) {
      if (
        this.preparationDeadline != null &&
        this.clock() >= this.preparationDeadline
      ) {
        throw new SandboxError("preparation budget exhausted");
      }
      const result = await runDocker(
        [
          "exec",
          this.targetId!,
          "python3",
          "-c",
          "import socket,sys; s=socket.create_connection(('127.0.0.1',9999),2); sys.stdout.write(s.recv(64).decode()); s.close()",
        ],
        this.preparationTimeout(5) * 1000,
      );
      if (
        !result.error &&
        !result.timedOut &&
        result.status === 0 &&
        result.stdout.includes("flagagent-target-ok")
      ) {
        return;
      }

      if (attempt === 29) break;
      let delayMs = 500;
      if (this.preparationDeadline != null) {
        delayMs = Math.min(
          delayMs,
          Math.max(0, this.preparationDeadline - this.clock()) * 1000,
        );
      }
      if (delayMs <= 0) throw new SandboxError("preparation budget exhausted");
      await new Promise<void>((resolve) => setTimeout(resolve, delayMs));
    }
    throw new SandboxError("target not ready");
  }

  async execute(command: string, timeoutSeconds: number): Promise<ShellResult> {
    if (!this.containerId) throw new SandboxError("not prepared");

    try {
      const running = await runDocker(
        ["inspect", "--format", "{{.State.Running}}", this.containerId],
        this.executionTimeout(10) * 1000,
      );
      if (
        running.error ||
        running.timedOut ||
        running.status !== 0 ||
        running.stdout.trim() !== "true"
      ) {
        throw new SandboxError("agent not running");
      }

      const commandDeadline = this.clock() + Math.max(0, timeoutSeconds);
      const boundedDeadline =
        this.executionDeadline == null
          ? commandDeadline
          : Math.min(commandDeadline, this.executionDeadline);
      const remaining = boundedDeadline - this.clock();
      if (remaining <= 0) throw new SandboxError("execution budget exhausted");

      const result = await runDocker(
        ["exec", "-w", WORKSPACE_TARGET, this.containerId, "/bin/bash", "-lc", command],
        remaining * 1000,
      );
      if (result.timedOut) {
        await this.recoverAfterTimeout();
        return new ShellResult(
          result.stdout,
          result.stderr,
          null,
          true,
          result.truncated,
        );
      }
      if (result.error) {
        throw new SandboxError(`docker exec failed: ${resultDetail(result)}`);
      }
      return new ShellResult(
        result.stdout,
        result.stderr,
        result.status ?? 1,
        false,
        result.truncated,
      );
    } finally {
      this.executionDeadline = null;
    }
  }

  private async recoverAfterTimeout(): Promise<void> {
    if (!this.containerId) {
      throw new SandboxError("timeout recovery failed: no agent container");
    }
    if (this.executionDeadline != null && this.clock() >= this.executionDeadline) {
      throw new SandboxError("execution budget exhausted");
    }

    const killed = await runDocker(
      ["kill", this.containerId],
      this.executionTimeout(30) * 1000,
    );
    if (killed.error || killed.timedOut || killed.status !== 0) {
      throw new SandboxError(
        `timeout recovery failed: docker kill failed: ${resultDetail(killed)}`,
      );
    }

    if (this.executionDeadline != null && this.clock() >= this.executionDeadline) {
      throw new SandboxError("execution budget exhausted");
    }
    const started = await runDocker(
      ["start", this.containerId],
      this.executionTimeout(60) * 1000,
    );
    if (started.error || started.timedOut || started.status !== 0) {
      throw new SandboxError(
        `timeout recovery failed: docker start failed: ${resultDetail(started)}`,
      );
    }
    if (this.executionDeadline != null && this.clock() >= this.executionDeadline) {
      throw new SandboxError("execution budget exhausted");
    }
    const running = await runDocker(
      ["inspect", "--format", "{{.State.Running}}", this.containerId],
      this.executionTimeout(10) * 1000,
    );
    if (
      running.error ||
      running.timedOut ||
      running.status !== 0 ||
      running.stdout.trim() !== "true"
    ) {
      throw new SandboxError(
        "timeout recovery failed: agent container not running after restart",
      );
    }
    if (this.executionDeadline != null && this.clock() >= this.executionDeadline) {
      throw new SandboxError("execution budget exhausted");
    }
    const probe = await runDocker(
      ["exec", this.containerId, "/bin/true"],
      this.executionTimeout(10) * 1000,
    );
    if (probe.error || probe.timedOut || probe.status !== 0) {
      throw new SandboxError(
        "timeout recovery failed: agent container not usable after restart",
      );
    }
  }

  private async isContainerRunning(id: string): Promise<boolean> {
    let timeout: number;
    try {
      timeout = this.preparationTimeout(10);
    } catch {
      return false;
    }
    const result = await runDocker(
      ["inspect", "--format", "{{.State.Running}}", id],
      timeout * 1000,
    );
    return (
      !result.error &&
      !result.timedOut &&
      result.status === 0 &&
      result.stdout.trim() === "true"
    );
  }

  private async forceRemove(id: string): Promise<void> {
    const result = await runDocker(["rm", "-f", id], 30000);
    // Removal is best effort during preparation.  The caller owns the
    // original preparation error and final cleanup retries known resources.
    void result;
  }

  private async removeOwned(): Promise<string[]> {
    const errors: string[] = [];

    if (this.containerId) {
      const id = this.containerId;
      const result = await this.removeDockerResource(["rm", "-f", id]);
      if (result !== null) {
        errors.push(result);
      } else {
        this.containerId = null;
        this.containerName = null;
      }
    }

    if (this.targetId) {
      const id = this.targetId;
      const result = await this.removeDockerResource(["rm", "-f", id]);
      if (result !== null) {
        errors.push(result);
      } else {
        this.targetId = null;
        this.targetName = null;
      }
    }

    if (this.networkId) {
      const id = this.networkId;
      const result = await this.removeDockerResource(["network", "rm", id]);
      if (result !== null) {
        errors.push(result);
      } else {
        this.networkId = null;
        this.networkName = null;
      }
    }

    return errors;
  }

  private async removeDockerResource(args: string[]): Promise<string | null> {
    let timeout = 30000;
    try {
      if (this.preparationDeadline != null)
        timeout = this.preparationTimeout(30) * 1000;
    } catch (error) {
      return asError(error).message;
    }
    const result = await runDocker(args, timeout);
    if (
      result.status === 0 ||
      result.stderr.includes("No such container") ||
      result.stderr.includes("No such network")
    ) {
      return null;
    }
    return resultDetail(result);
  }

  async cleanup(runId: string): Promise<void> {
    validateRunId(runId);
    const errors = await this.removeOwned();
    if (errors.length) throw new SandboxError(`cleanup failed: ${errors.join("; ")}`);
  }

  sandboxProvenance(): Record<string, unknown> {
    return {
      backend: "docker",
      image: this.opts.image ?? SANDBOX_IMAGE,
      network_mode: this.opts.networkMode,
      memory: "2g",
      cpus: "2",
      pids_limit: 256,
      container_user: runtimeUser(),
      security_relaxations: [],
    };
  }

  sandboxLifecycle(): Record<string, unknown> {
    const info: Record<string, unknown> = {};
    if (this.containerId) info.agent_container_id = this.containerId;
    if (this.networkId) info.network_id = this.networkId;
    if (this.targetId) info.target_container_id = this.targetId;
    return info;
  }
}
