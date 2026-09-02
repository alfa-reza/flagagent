/* eslint-disable @typescript-eslint/no-explicit-any, no-empty, no-useless-escape */
import { describe, it, expect, beforeEach, afterEach } from "vitest";
import {
  mkdtempSync,
  rmSync,
  writeFileSync,
  chmodSync,
  readFileSync,
  mkdirSync,
} from "node:fs";
import { join, resolve, isAbsolute, relative } from "node:path";
import { tmpdir } from "node:os";
import { DockerExecutor } from "../src/flagagent/docker.js";

/**
 * Docker lifecycle / provenance tests using deterministic fake Docker CLI injected via PATH.
 * Requires a shim `docker` script that handles the subset of commands used by DockerExecutor.
 */

function makeFakeDocker(dir: string, behavior: string): string {
  const script = `#!/usr/bin/env bash
set -euo pipefail
BEHAVIOR="${behavior}"
LOG="\${FAKE_DOCKER_LOG:-/tmp/fake-docker.log}"
echo "docker $*" >> "\$LOG"
case "\$1" in
  context)
    if [ "\$2" = "show" ]; then echo "default"; exit 0; fi
    if [ "\$2" = "inspect" ]; then echo "unix:///var/run/docker.sock"; exit 0; fi
    ;;
  run)
    # agent/target create - emit id, but for some behaviors simulate failure/timeout
    if [[ "\$BEHAVIOR" == "large-output" ]]; then
      echo "id-\$(date +%s%N)"
      exit 0
    fi
    if [[ "\$BEHAVIOR" == "adopt-zero" || "\$BEHAVIOR" == "adopt-one" || "\$BEHAVIOR" == "adopt-multi" || "\$BEHAVIOR" == "ownership-mismatch" ]]; then
      echo "id-\$(date +%s%N)"
      exit 0
    fi
    echo "id-\$(date +%s%N)"
    exit 0
    ;;
  network)
    if [ "\$2" = "create" ]; then echo "net-\$(date +%s%N)"; exit 0; fi
    if [ "\$2" = "ls" ]; then
      if [[ "\$BEHAVIOR" == "adopt-one" ]]; then
        echo "cid-adopt-1"
        exit 0
      elif [[ "\$BEHAVIOR" == "adopt-zero" ]]; then
        exit 0
      elif [[ "\$BEHAVIOR" == "adopt-multi" ]]; then
        echo -e "cid1\\ncid2"
        exit 0
      elif [[ "\$BEHAVIOR" == "ownership-mismatch" ]]; then
        echo "cid-mismatch"
        exit 0
      fi
      exit 0
    fi
    if [ "\$2" = "rm" ]; then exit 0; fi
    ;;
  ps)
    if [[ "\$BEHAVIOR" == "adopt-one" ]]; then
      echo "cid-adopt-1"
      exit 0
    elif [[ "\$BEHAVIOR" == "adopt-zero" ]]; then
      exit 0
    elif [[ "\$BEHAVIOR" == "adopt-multi" ]]; then
      echo -e "cid1\\ncid2"
      exit 0
    elif [[ "\$BEHAVIOR" == "ownership-mismatch" ]]; then
      echo "cid-mismatch"
      exit 0
    fi
    exit 0
    ;;
  inspect)
    # Handle --format; check for Running vs labeled inspect
    if [[ "\$*" == *".State.Running"* ]]; then
      echo "true"
      exit 0
    fi
    # Labeled inspect for adopt tests
    if [[ "\$BEHAVIOR" == "adopt-one" ]]; then
      # Return one candidate with correct labels and expected name
      # Need to know expected name: extract from previous ps filter? Simplification: use flagagent-agent-* pattern
      echo "cid-adopt-1 /flagagent-agent-FA-20260814T000000Z-aaaa {\\"flagagent.managed\\":\\"true\\",\\"flagagent.run_id\\":\\"FA-20260814T000000Z-aaaa\\",\\"flagagent.role\\":\\"agent\\"}"
      exit 0
    fi
    if [[ "\$BEHAVIOR" == "ownership-mismatch" ]]; then
      echo "cid-mismatch /flagagent-agent-FA-20260814T000000Z-aaaa {\\"flagagent.managed\\":\\"false\\",\\"flagagent.run_id\\":\\"wrong\\",\\"flagagent.role\\":\\"agent\\"}"
      exit 0
    fi
    if [[ "\$BEHAVIOR" == "adopt-multi" ]]; then
      echo "cid1 /flagagent-agent-FA-20260814T000000Z-aaaa {\\"flagagent.managed\\":\\"true\\",\\"flagagent.run_id\\":\\"FA-20260814T000000Z-aaaa\\",\\"flagagent.role\\":\\"agent\\"}"
      echo "cid2 /flagagent-agent-FA-20260814T000000Z-aaaa {\\"flagagent.managed\\":\\"true\\",\\"flagagent.run_id\\":\\"FA-20260814T000000Z-aaaa\\",\\"flagagent.role\\":\\"agent\\"}"
      exit 0
    fi
    echo "true"
    exit 0
    ;;
  exec)
    # target ready probe + shell exec
    if [[ "\$*" == *"create_connection"* ]]; then
      echo "flagagent-target-ok"
      exit 0
    fi
    if [[ "\$BEHAVIOR" == "large-output" ]]; then
      # Emit large stdout
      python3 -c "import sys; sys.stdout.write('X'*70000)"
      exit 0
    fi
    if [[ "\$BEHAVIOR" == "timeout" ]]; then
      sleep 10
      exit 0
    fi
    if [[ "\$BEHAVIOR" == "control-plane-failure" ]]; then
      echo "Error response from daemon: No such container" >&2
      exit 125
    fi
    if [[ "\$*" == *"/bin/true"* ]]; then
      exit 0
    fi
    echo "hello"
    exit 0
    ;;
  kill|start|rm)
    exit 0
    ;;
  *)
    echo "unknown command \$1" >&2
    exit 1
    ;;
esac
`;
  const p = join(dir, "docker");
  writeFileSync(p, script, { mode: 0o755 });
  chmodSync(p, 0o755);
  return p;
}

describe("docker lifecycle with fake CLI", () => {
  let tmp: string;
  let origPath: string;
  let fakeBin: string;

  beforeEach(() => {
    tmp = mkdtempSync(join(tmpdir(), "flagagent-docker-"));
    origPath = process.env.PATH ?? "";
    fakeBin = mkdtempSync(join(tmpdir(), "flagagent-fakebin-"));
  });
  afterEach(() => {
    process.env.PATH = origPath;
    try {
      rmSync(tmp, { recursive: true, force: true });
    } catch {}
    try {
      rmSync(fakeBin, { recursive: true, force: true });
    } catch {}
  });

  it("success prepare + execute + provenance", async () => {
    makeFakeDocker(fakeBin, "success");
    process.env.PATH = `${fakeBin}:${origPath}`;
    const ex = new DockerExecutor();
    const ws = mkdtempSync(join(tmp, "ws-"));
    await ex.prepare(ws, "FA-20260814T000000Z-aaaa");
    const res = await ex.execute("echo hi", 5);
    expect(res.exitCode).toBe(0);
    expect(res.stdout).toContain("hello");
    const prov = ex.sandboxProvenance();
    expect(prov.backend).toBe("docker");
    expect(prov.image).toBeDefined();
    expect((prov as any).network_mode).toBeDefined();
    const life = ex.sandboxLifecycle();
    expect((life as any).agent_container_id).toBeDefined();
    await ex.cleanup("FA-20260814T000000Z-aaaa");
  });

  it("non-zero shell result is execution evidence", async () => {
    // Fake returns exit 1 with output
    const script = `#!/usr/bin/env bash
set -euo pipefail
case "\$1" in
  context) if [ "\$2" = "show" ]; then echo "default"; else echo "unix:///var/run/docker.sock"; fi; exit 0;;
  run) echo "id-\$(date +%s%N)"; exit 0;;
  network) if [ "\$2" = "create" ]; then echo "net-1"; exit 0; fi; if [ "\$2" = "ls" ]; then exit 0; fi; if [ "\$2" = "rm" ]; then exit 0; fi;;
  inspect) if [[ "\$*" == *".State.Running"* ]]; then echo "true"; exit 0; fi; echo "true"; exit 0;;
  exec) if [[ "\$*" == *"create_connection"* ]]; then echo "flagagent-target-ok"; exit 0; fi; if [[ "\$*" == *"/bin/true"* ]]; then exit 0; fi; echo "err out" >&2; echo "out"; exit 2;;
  kill|start|rm) exit 0;;
  *) exit 0;;
esac`;
    const p = join(fakeBin, "docker");
    writeFileSync(p, script, { mode: 0o755 });
    chmodSync(p, 0o755);
    process.env.PATH = `${fakeBin}:${origPath}`;
    const ex = new DockerExecutor();
    const ws = mkdtempSync(join(tmp, "ws-"));
    await ex.prepare(ws, "FA-20260814T000000Z-aaaa");
    const res = await ex.execute("false", 5);
    expect(res.exitCode).toBe(2);
    expect(res.timedOut).toBe(false);
  });

  it("large stdout bounded head/tail and truncated", async () => {
    makeFakeDocker(fakeBin, "large-output");
    process.env.PATH = `${fakeBin}:${origPath}`;
    const ex = new DockerExecutor();
    const ws = mkdtempSync(join(tmp, "ws-"));
    await ex.prepare(ws, "FA-20260814T000000Z-aaaa");
    const res = await ex.execute("cat big", 5);
    expect(res.truncated).toBe(true);
    expect(res.stdout.length).toBeGreaterThan(0);
  });

  it("control-plane failure throws SandboxError", async () => {
    makeFakeDocker(fakeBin, "control-plane-failure");
    process.env.PATH = `${fakeBin}:${origPath}`;
    const ex = new DockerExecutor();
    const ws = mkdtempSync(join(tmp, "ws-"));
    await ex.prepare(ws, "FA-20260814T000000Z-aaaa");
    await expect(ex.execute("echo hi", 5)).rejects.toThrow();
  });

  it("effective local context passes validation, remote fails", async () => {
    // local: default fake returns unix socket -> should pass
    makeFakeDocker(fakeBin, "success");
    process.env.PATH = `${fakeBin}:${origPath}`;
    const exLocal = new DockerExecutor();
    const ws = mkdtempSync(join(tmp, "wsLocal-"));
    await expect(
      exLocal.prepare(ws, "FA-20260814T000000Z-aaaa"),
    ).resolves.toBeUndefined();
    await exLocal.cleanup("FA-20260814T000000Z-aaaa");

    // remote: fake context inspect returns tcp:// remote host
    const remoteScript = `#!/usr/bin/env bash
set -euo pipefail
case "\$1" in
  context) if [ "\$2" = "show" ]; then echo "default"; else echo "tcp://remote:2375"; fi; exit 0;;
  *) echo "should not be called for remote" >&2; exit 1;;
esac`;
    const p2 = join(fakeBin, "docker");
    writeFileSync(p2, remoteScript, { mode: 0o755 });
    chmodSync(p2, 0o755);
    const exRemote = new DockerExecutor();
    const ws2 = mkdtempSync(join(tmp, "wsRemote-"));
    await expect(exRemote.prepare(ws2, "FA-20260814T000000Z-bbbb")).rejects.toThrow(
      /unsupported remote/,
    );
  });

  it("reconciliation zero/one/multiple and ownership mismatch", async () => {
    // This tests classifyCandidates indirectly via cleanup reconciliation paths.
    // We trigger pending states by making create fail after resource creation ambiguity.
    // Instead directly test via pending flags + fake inspect responses.

    // adopt-zero: pending but no candidate -> cleanup succeeds (absent)
    makeFakeDocker(fakeBin, "adopt-zero");
    process.env.PATH = `${fakeBin}:${origPath}`;
    const exZero = new DockerExecutor();
    // Force pending by directly setting private fields
    (exZero as any).pendingAgent = true;
    (exZero as any).containerName = "flagagent-agent-FA-20260814T000000Z-aaaa";
    await expect(exZero.cleanup("FA-20260814T000000Z-aaaa")).resolves.toBeUndefined();
    expect((exZero as any).pendingAgent).toBe(false);

    // adopt-one: one candidate with correct labels -> adopted
    makeFakeDocker(fakeBin, "adopt-one");
    const exOne = new DockerExecutor();
    (exOne as any).pendingAgent = true;
    (exOne as any).containerName = "flagagent-agent-FA-20260814T000000Z-aaaa";
    await expect(exOne.cleanup("FA-20260814T000000Z-aaaa")).resolves.toBeUndefined();

    // adopt-multi: multiple candidates -> should report ambiguous ownership error
    makeFakeDocker(fakeBin, "adopt-multi");
    const exMulti = new DockerExecutor();
    (exMulti as any).pendingAgent = true;
    (exMulti as any).containerName = "flagagent-agent-FA-20260814T000000Z-aaaa";
    await expect(exMulti.cleanup("FA-20260814T000000Z-aaaa")).rejects.toThrow(
      /ambiguous/,
    );

    // ownership-mismatch: candidate lacks labels -> ambiguous/ownership failure
    makeFakeDocker(fakeBin, "ownership-mismatch");
    const exMis = new DockerExecutor();
    (exMis as any).pendingAgent = true;
    (exMis as any).containerName = "flagagent-agent-FA-20260814T000000Z-aaaa";
    await expect(exMis.cleanup("FA-20260814T000000Z-aaaa")).rejects.toThrow(
      /ambiguous/,
    );
  });

  it("shared deadline and monotonic clock domain for execute", async () => {
    makeFakeDocker(fakeBin, "success");
    process.env.PATH = `${fakeBin}:${origPath}`;
    const ex = new DockerExecutor();
    const ws = mkdtempSync(join(tmp, "ws-"));
    await ex.prepare(ws, "FA-20260814T000000Z-aaaa");
    let now = 1000;
    const clock = () => now;
    ex.setExecutionDeadline(now + 0.05, clock);
    // Advance past deadline before execute
    now = 2000;
    await expect(ex.execute("echo hi", 5)).rejects.toThrow(
      /execution budget exhausted/,
    );
  });

  it("cleanup convergence: removes owned resources", async () => {
    makeFakeDocker(fakeBin, "success");
    process.env.PATH = `${fakeBin}:${origPath}`;
    const ex = new DockerExecutor({ networkMode: "none" });
    const ws = mkdtempSync(join(tmp, "ws-"));
    await ex.prepare(ws, "FA-20260814T000000Z-aaaa");
    await expect(ex.cleanup("FA-20260814T000000Z-aaaa")).resolves.toBeUndefined();
  });

  it("provenance fields include version, docker info not mocked (smoke)", async () => {
    const logFile = join(tmp, "docker-provenance.log");
    process.env.FAKE_DOCKER_LOG = logFile;
    makeFakeDocker(fakeBin, "success");
    process.env.PATH = `${fakeBin}:${origPath}`;
    const ex = new DockerExecutor();
    const ws = mkdtempSync(join(tmp, "ws-"));
    await ex.prepare(ws, "FA-20260814T000000Z-aaaa");
    const prov = ex.sandboxProvenance();
    expect(prov.backend).toBe("docker");
    expect(prov.flagagent_version).toBe("0.2.0");
    expect(prov.image).toBeDefined();
    expect(prov.network_mode).toBe("none");
    expect("docker_engine" in prov).toBe(true);
    expect("rootless" in prov).toBe(true);
    const life = ex.sandboxLifecycle();
    expect(life.agent_container_id).toBeDefined();
    expect(life.image_id).toBeDefined();
    const log = readFileSync(logFile, "utf8");
    expect(log).toContain("flagagent.version=0.2.0");
    expect(log).toContain("flagagent.managed=true");
    expect(log).toContain("flagagent.run_id=FA-20260814T000000Z-aaaa");
    delete process.env.FAKE_DOCKER_LOG;
    await ex.cleanup("FA-20260814T000000Z-aaaa");
  });

  it("relative workspace is resolved to absolute bind source at Docker boundary", async () => {
    const logFile = join(tmp, "docker-relative.log");
    process.env.FAKE_DOCKER_LOG = logFile;
    makeFakeDocker(fakeBin, "success");
    process.env.PATH = `${fakeBin}:${origPath}`;
    const projectRoot = mkdtempSync(join(tmpdir(), "flagagent-relative-"));
    try {
      const rel = join("runs", "FA-20260814T000000Z-aaaa", "workspace");
      const absWs = join(projectRoot, rel);
      mkdirSync(absWs, { recursive: true });
      const origCwd = process.cwd();
      process.chdir(projectRoot);
      try {
        const relWs = relative(process.cwd(), absWs);
        expect(isAbsolute(relWs)).toBe(false);
        const ex = new DockerExecutor();
        await ex.prepare(relWs, "FA-20260814T000000Z-aaaa");
        const log = readFileSync(logFile, "utf8");
        const mountLines = log.split("\n").filter((l) => l.includes("--mount"));
        expect(mountLines.length).toBeGreaterThan(0);
        for (const line of mountLines) {
          const m = line.match(/source=([^,]+),target=\/workspace/);
          expect(m).not.toBeNull();
          const src = m![1]!;
          expect(isAbsolute(src)).toBe(true);
          expect(src).toBe(resolve(relWs));
        }
        expect(log).toContain("target=/workspace");
        expect(log).toContain("--network none");
        await ex.cleanup("FA-20260814T000000Z-aaaa");
        const ex2 = new DockerExecutor({ networkMode: "local" });
        const rel2 = join("runs", "FA-20260814T000000Z-bbbb", "workspace");
        const absWs2 = join(projectRoot, rel2);
        mkdirSync(absWs2, { recursive: true });
        const relWs2 = relative(process.cwd(), absWs2);
        await ex2.prepare(relWs2, "FA-20260814T000000Z-bbbb");
        const log2 = readFileSync(logFile, "utf8");
        const agentMounts = log2
          .split("\n")
          .filter((l) => l.includes("flagagent-agent-") && l.includes("--mount"));
        expect(agentMounts.length).toBeGreaterThan(0);
        const lastMount = agentMounts[agentMounts.length - 1]!;
        const m2 = lastMount.match(/source=([^,]+),target=\/workspace/);
        expect(m2).not.toBeNull();
        expect(isAbsolute(m2![1]!)).toBe(true);
        await ex2.cleanup("FA-20260814T000000Z-bbbb");
      } finally {
        process.chdir(origCwd);
      }
    } finally {
      rmSync(projectRoot, { recursive: true, force: true });
      delete process.env.FAKE_DOCKER_LOG;
    }
  });
});
