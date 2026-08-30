import { spawnSync, spawn } from "node:child_process";
import { SandboxError, ShellResult } from "./tools.js";
import { validateRunId } from "./artifacts.js";

const SANDBOX_IMAGE = "flagagent-sandbox:dev";
const TARGET_IMAGE = "flagagent-target:dev";
const WORKSPACE_TARGET = "/workspace";

function runtimeUser(): string {
  return `${process.getuid?.() ?? 1000}:${process.getgid?.() ?? 1000}`;
}

function runDocker(
  args: string[],
  timeoutMs = 30000,
): { stdout: string; stderr: string; status: number | null } {
  const r = spawnSync("docker", args, { encoding: "utf8", timeout: timeoutMs });
  return {
    stdout: (r.stdout as string) ?? "",
    stderr: (r.stderr as string) ?? "",
    status: r.status,
  };
}

export class DockerExecutor {
  private containerId: string | null = null;
  private containerName: string | null = null;
  private networkId: string | null = null;
  private networkName: string | null = null;
  private targetId: string | null = null;
  private targetName: string | null = null;
  private executionDeadline: number | null = null;
  private executionMonotonic: (() => number) | null = null;
  private preparationDeadline: number | null = null;
  private preparationRemaining: number | null = null;

  constructor(private opts: { image?: string; networkMode?: string } = {}) {
    this.opts.networkMode ??= "none";
    if (!["none", "local"].includes(this.opts.networkMode))
      throw new Error(`unsupported network_mode ${this.opts.networkMode}`);
  }

  setRemaining(remaining: number): void {
    this.preparationRemaining = Math.max(0, remaining);
  }
  setExecutionDeadline(deadline: number, monotonic: () => number): void {
    this.executionDeadline = deadline;
    this.executionMonotonic = monotonic;
  }

  private preparationTimeout(fixed: number): number {
    if (this.preparationDeadline == null) return fixed;
    const rem =
      this.preparationDeadline - (this.executionMonotonic?.() ?? Date.now() / 1000);
    if (rem <= 0) throw new SandboxError("preparation budget exhausted");
    return Math.min(fixed, rem);
  }
  private executionTimeout(fixed: number): number {
    if (this.executionDeadline == null) return fixed;
    const rem =
      this.executionDeadline - (this.executionMonotonic?.() ?? Date.now() / 1000);
    if (rem <= 0) throw new SandboxError("execution budget exhausted");
    return Math.min(fixed, rem);
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

  prepare(workspace: string, runId: string): void {
    if (this.containerId) throw new SandboxError("already prepared");
    validateRunId(runId);
    if (process.getuid?.() === 0) throw new SandboxError("running as root unsupported");
    this.containerName = `flagagent-agent-${runId}`;
    if (this.preparationRemaining != null) {
      if (this.preparationRemaining <= 0)
        throw new SandboxError("preparation budget exhausted");
      this.preparationDeadline =
        (this.executionMonotonic?.() ?? Date.now() / 1000) + this.preparationRemaining;
      this.preparationRemaining = null;
    }
    try {
      this.validateEndpoint();
      if (this.opts.networkMode === "local") this.prepareLocal(workspace, runId);
      else this.prepareNone(workspace, runId);
    } finally {
      this.preparationDeadline = null;
    }
  }

  private prepareNone(workspace: string, runId: string): void {
    try {
      this.createAgent(workspace, runId);
    } catch (e) {
      if (
        this.preparationDeadline != null &&
        (this.executionMonotonic?.() ?? Date.now() / 1000) >= this.preparationDeadline
      )
        throw e;
      this.removeOwned();
      throw e;
    }
  }
  private prepareLocal(workspace: string, runId: string): void {
    this.networkName = `flagagent-net-${runId}`;
    this.targetName = `flagagent-target-${runId}`;
    try {
      this.createNetwork(runId);
      this.createTarget(runId);
      this.waitTargetReady();
      this.createAgent(workspace, runId);
    } catch (e) {
      if (
        this.preparationDeadline != null &&
        (this.executionMonotonic?.() ?? Date.now() / 1000) >= this.preparationDeadline
      )
        throw e;
      this.removeOwned();
      throw e;
    }
  }

  private createAgent(workspace: string, runId: string): void {
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
    const r = runDocker(args, this.preparationTimeout(60) * 1000);
    if (r.status !== 0) throw new SandboxError(`docker run failed: ${r.stderr.trim()}`);
    const id = r.stdout.trim();
    if (!id) throw new SandboxError("docker run returned no id");
    this.containerId = id;
    if (!this.isContainerRunning(this.containerId)) {
      this.forceRemove(this.containerId);
      this.containerId = null;
      throw new SandboxError("agent container not running");
    }
  }
  private createNetwork(runId: string): void {
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
    const r = runDocker(args, this.preparationTimeout(30) * 1000);
    if (r.status !== 0)
      throw new SandboxError(`docker network create failed: ${r.stderr.trim()}`);
    const id = r.stdout.trim();
    if (!id) throw new SandboxError("docker network no id");
    this.networkId = id;
  }
  private createTarget(runId: string): void {
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
    const r = runDocker(args, this.preparationTimeout(60) * 1000);
    if (r.status !== 0)
      throw new SandboxError(`docker target run failed: ${r.stderr.trim()}`);
    const id = r.stdout.trim();
    if (!id) throw new SandboxError("target no id");
    this.targetId = id;
  }
  private waitTargetReady(): void {
    for (let i = 0; i < 30; i++) {
      if (
        this.preparationDeadline != null &&
        (this.executionMonotonic?.() ?? Date.now() / 1000) >= this.preparationDeadline
      )
        throw new SandboxError("preparation budget exhausted");
      const r = runDocker(
        [
          "exec",
          this.targetId!,
          "python3",
          "-c",
          "import socket,sys; s=socket.create_connection(('127.0.0.1',9999),2); sys.stdout.write(s.recv(64).decode()); s.close()",
        ],
        5000,
      );
      if (r.status === 0 && r.stdout.includes("flagagent-target-ok")) return;
      Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 500);
    }
    throw new SandboxError("target not ready");
  }

  execute(command: string, timeoutSeconds: number): ShellResult {
    if (!this.containerId) throw new SandboxError("not prepared");
    const running = runDocker(
      ["inspect", "--format", "{{.State.Running}}", this.containerId],
      this.executionTimeout(10) * 1000,
    );
    if (running.stdout.trim() !== "true") throw new SandboxError("agent not running");
    const deadline =
      (this.executionMonotonic?.() ?? Date.now() / 1000) + timeoutSeconds;
    const boundedDeadline =
      this.executionDeadline != null
        ? Math.min(deadline, this.executionDeadline)
        : deadline;
    try {
      const proc = spawn("docker", [
        "exec",
        "-w",
        WORKSPACE_TARGET,
        this.containerId,
        "/bin/bash",
        "-lc",
        command,
      ]);
      let stdout = Buffer.alloc(0),
        stderr = Buffer.alloc(0);
      let timedOut = false;
      const limit = 64 * 1024;
      proc.stdout?.on("data", (d: Buffer) => {
        stdout = Buffer.concat([stdout, d]);
        if (stdout.length > limit * 2)
          stdout = stdout.subarray(stdout.length - limit * 2);
      });
      proc.stderr?.on("data", (d: Buffer) => {
        stderr = Buffer.concat([stderr, d]);
        if (stderr.length > limit * 2)
          stderr = stderr.subarray(stderr.length - limit * 2);
      });
      // Use sync wait with timeout via polling
      const code = (() => {
        const timeoutMs = Math.max(0, boundedDeadline * 1000 - Date.now());
        let result: number | null = null;
        let done = false;
        proc.on("close", (c) => {
          result = c;
          done = true;
        });
        const deadlineMs = Date.now() + timeoutMs;
        while (!done && Date.now() < deadlineMs)
          Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 20);
        if (!done) {
          timedOut = true;
          try {
            proc.kill("SIGKILL");
          } catch {
            /* ignore */
          }
        }
        // wait a bit for close
        const end = Date.now() + 1000;
        while (!done && Date.now() < end)
          Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 20);
        return result;
      })();
      if (timedOut) {
        this.recoverAfterTimeout();
        return new ShellResult(
          stdout.toString("utf8", 0, Math.min(stdout.length, limit)),
          stderr.toString("utf8", 0, Math.min(stderr.length, limit)),
          null,
          true,
        );
      }
      return new ShellResult(
        stdout.toString("utf8"),
        stderr.toString("utf8"),
        code ?? 0,
        false,
      );
    } finally {
      this.executionDeadline = null;
      this.executionMonotonic = null;
    }
  }

  private recoverAfterTimeout(): void {
    if (this.containerId) {
      runDocker(["kill", this.containerId], 30000);
      runDocker(["start", this.containerId], 60000);
    }
  }

  private isContainerRunning(id: string): boolean {
    const r = runDocker(["inspect", "--format", "{{.State.Running}}", id], 10000);
    return r.stdout.trim() === "true";
  }
  private forceRemove(id: string): void {
    try {
      runDocker(["rm", "-f", id], 30000);
    } catch {
      /* ignore */
    }
  }
  private removeOwned(): string[] {
    const errs: string[] = [];
    if (this.containerId) {
      const r = runDocker(["rm", "-f", this.containerId], 30000);
      if (r.status !== 0 && !r.stderr.includes("No such container"))
        errs.push(r.stderr);
      else {
        this.containerId = null;
        this.containerName = null;
      }
    }
    if (this.targetId) {
      const r = runDocker(["rm", "-f", this.targetId], 30000);
      if (r.status !== 0) errs.push(r.stderr);
      else {
        this.targetId = null;
        this.targetName = null;
      }
    }
    if (this.networkId) {
      const r = runDocker(["network", "rm", this.networkId], 30000);
      if (r.status !== 0) errs.push(r.stderr);
      else {
        this.networkId = null;
        this.networkName = null;
      }
    }
    return errs;
  }

  cleanup(runId: string): void {
    validateRunId(runId);
    const errs = this.removeOwned();
    if (errs.length) throw new SandboxError(`cleanup failed: ${errs.join("; ")}`);
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
