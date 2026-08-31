/* eslint-disable no-useless-escape */
import { describe, it, expect } from "vitest";
import { mkdtempSync, rmSync, writeFileSync, chmodSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { DockerExecutor } from "../src/flagagent/docker.js";

function makeDescendantFake(dir: string): void {
  const script = `#!/usr/bin/env bash
set -euo pipefail
LOG="\${FAKE_DOCKER_LOG:-/tmp/fake-docker.log}"
echo "docker $*" >> "\$LOG"
case "\$1" in
  context)
    if [ "\$2" = "show" ]; then echo "default"; exit 0; fi
    if [ "\$2" = "inspect" ]; then echo "unix:///var/run/docker.sock"; exit 0; fi
    ;;
  run)
    echo "id-desc-\$(date +%s%N)"
    exit 0
    ;;
  inspect)
    if [[ "\$*" == *".State.Running"* ]]; then echo "true"; exit 0; fi
    echo "true"; exit 0
    ;;
  exec)
    if [[ "\$*" == *"/bin/true"* ]]; then exit 0; fi
    echo "hello-descendant"
    ( sleep 10 & )
    exit 0
    ;;
  kill|start|rm|ps) exit 0 ;;
  network)
    if [ "\$2" = "create" ]; then echo "net-1"; exit 0; fi
    if [ "\$2" = "ls" ]; then exit 0; fi
    if [ "\$2" = "rm" ]; then exit 0; fi
    ;;
  *) exit 0 ;;
esac
`;
  const p = join(dir, "docker");
  writeFileSync(p, script, { mode: 0o755 });
  chmodSync(p, 0o755);
}

describe("docker descendant stdio lifecycle bounded", () => {
  it("execute bounded even when descendant retains stdout pipe", async () => {
    const fakeBin = mkdtempSync(join(tmpdir(), "flagagent-fake-desc-"));
    const origPath = process.env.PATH ?? "";
    const tmp = mkdtempSync(join(tmpdir(), "flagagent-docker-desc-"));
    try {
      makeDescendantFake(fakeBin);
      process.env.PATH = `${fakeBin}:${origPath}`;
      const ex = new DockerExecutor();
      const ws = mkdtempSync(join(tmp, "ws-"));
      await ex.prepare(ws, "FA-20260814T000000Z-aaaa");
      const start = Date.now();
      const res = await ex.execute("echo hi", 2);
      const elapsed = (Date.now() - start) / 1000;
      expect(elapsed).toBeLessThan(4);
      expect(res.stdout).toContain("hello-descendant");
      // Bounded truncation semantics preserved
      expect(typeof res.truncated).toBe("boolean");
      await ex.cleanup("FA-20260814T000000Z-aaaa");
    } finally {
      process.env.PATH = origPath;
      rmSync(tmp, { recursive: true, force: true });
      rmSync(fakeBin, { recursive: true, force: true });
    }
  });

  it("runDocker timeout bounded even with descendant holding pipe", async () => {
    const fakeBin = mkdtempSync(join(tmpdir(), "flagagent-fake-desc2-"));
    const origPath = process.env.PATH ?? "";
    const tmp = mkdtempSync(join(tmpdir(), "flagagent-docker-desc2-"));
    try {
      const script = `#!/usr/bin/env bash
set -euo pipefail
LOG="\${FAKE_DOCKER_LOG:-/tmp/fake-docker.log}"
echo "docker $*" >> "\$LOG"
case "\$1" in
  context) if [ "\$2" = "show" ]; then echo "default"; else echo "unix:///var/run/docker.sock"; fi; exit 0;;
  run) echo "id-\$(date +%s%N)"; exit 0;;
  inspect) if [[ "\$*" == *".State.Running"* ]]; then echo "true"; exit 0; fi; echo "true"; exit 0;;
  exec)
    if [[ "\$*" == *"/bin/true"* ]]; then exit 0; fi
    echo "slow"
    sleep 10
    exit 0
    ;;
  kill|start|rm|ps) exit 0;;
  network) if [ "\$2" = "create" ]; then echo "net-1"; exit 0; fi; if [ "\$2" = "ls" ]; then exit 0; fi; if [ "\$2" = "rm" ]; then exit 0; fi;;
  *) exit 0;;
esac`;
      const p = join(fakeBin, "docker");
      writeFileSync(p, script, { mode: 0o755 });
      chmodSync(p, 0o755);
      process.env.PATH = `${fakeBin}:${origPath}`;
      const ex = new DockerExecutor();
      const ws = mkdtempSync(join(tmp, "ws-"));
      await ex.prepare(ws, "FA-20260814T000000Z-aaaa");
      const now = 1000;
      const clock = () => now;
      ex.setExecutionDeadline(now + 0.3, clock);
      const start = Date.now();
      const res = await ex.execute("sleep 10", 10);
      const elapsed = (Date.now() - start) / 1000;
      expect(elapsed).toBeLessThan(2);
      expect(res.timedOut).toBe(true);
      await ex.cleanup("FA-20260814T000000Z-aaaa");
    } finally {
      process.env.PATH = origPath;
      rmSync(tmp, { recursive: true, force: true });
      rmSync(fakeBin, { recursive: true, force: true });
    }
  });

  it("probes during execute use execution deadline not preparation", async () => {
    const fakeBin = mkdtempSync(join(tmpdir(), "flagagent-fake-probe-"));
    const origPath = process.env.PATH ?? "";
    const tmp = mkdtempSync(join(tmpdir(), "flagagent-probe-"));
    try {
      const logFile = join(tmp, "docker.log");
      const script = `#!/usr/bin/env bash
set -euo pipefail
LOG="\${FAKE_DOCKER_LOG:-/tmp/fake-docker.log}"
echo "docker $*" >> "\$LOG"
case "\$1" in
  context) if [ "\$2" = "show" ]; then echo "default"; else echo "unix:///var/run/docker.sock"; fi; exit 0;;
  run) echo "id-\$(date +%s%N)"; exit 0;;
  inspect)
    if [[ "\$*" == *".State.Running"* ]]; then
      # Simulate slow inspect that would exceed execution budget if using preparation timeout
      sleep 1
      echo "true"; exit 0
    fi
    echo "true"; exit 0;;
  exec)
    if [[ "\$*" == *"/bin/true"* ]]; then sleep 1; exit 0; fi
    echo "Error response from daemon: No such container" >&2; exit 125;;
  kill|start|rm|ps) exit 0;;
  network) if [ "\$2" = "create" ]; then echo "net-1"; exit 0; fi; if [ "\$2" = "ls" ]; then exit 0; fi; if [ "\$2" = "rm" ]; then exit 0; fi;;
  *) exit 0;;
esac`;
      const p = join(fakeBin, "docker");
      writeFileSync(p, script, { mode: 0o755 });
      chmodSync(p, 0o755);
      process.env.PATH = `${fakeBin}:${origPath}`;
      process.env.FAKE_DOCKER_LOG = logFile;
      const ex = new DockerExecutor();
      const ws = mkdtempSync(join(tmp, "ws-"));
      await ex.prepare(ws, "FA-20260814T000000Z-aaaa");
      const now = 1000;
      const clock = () => now;
      ex.setExecutionDeadline(now + 0.15, clock);
      await expect(ex.execute("false", 5)).rejects.toThrow(
        /execution budget|timed out|agent not running/,
      );
      delete process.env.FAKE_DOCKER_LOG;
      await ex.cleanup("FA-20260814T000000Z-aaaa");
    } finally {
      process.env.PATH = origPath;
      delete process.env.FAKE_DOCKER_LOG;
      rmSync(tmp, { recursive: true, force: true });
      rmSync(fakeBin, { recursive: true, force: true });
    }
  });
});
