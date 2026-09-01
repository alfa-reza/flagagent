import { describe, it, expect } from "vitest";
import {
  mkdtempSync,
  rmSync,
  mkdirSync,
  writeFileSync,
  chmodSync,
  statSync,
  symlinkSync,
} from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import {
  snapshotSourceFiles,
  stageSourceFiles,
  InvalidChallengeSourceError,
} from "../src/flagagent/staging.js";
import { Limits } from "../src/flagagent/limits.js";

describe("staging security", () => {
  it("symlink rejected", () => {
    const tmp = mkdtempSync(join(tmpdir(), "flagagent-"));
    try {
      const src = join(tmp, "src");
      mkdirSync(src);
      const outside = join(tmp, "outside");
      writeFileSync(outside, "secret");
      symlinkSync(outside, join(src, "link"));
      expect(() => snapshotSourceFiles(src)).toThrow(InvalidChallengeSourceError);
    } finally {
      rmSync(tmp, { recursive: true, force: true });
    }
  });

  it("executable bits preserved and digest unchanged by exec bits", () => {
    const tmp = mkdtempSync(join(tmpdir(), "flagagent-"));
    try {
      const a = join(tmp, "a");
      mkdirSync(a);
      writeFileSync(join(a, "file.txt"), "same");
      chmodSync(join(a, "file.txt"), 0o644);
      const b = join(tmp, "b");
      mkdirSync(b);
      writeFileSync(join(b, "file.txt"), "same");
      chmodSync(join(b, "file.txt"), 0o755);
      const r1 = snapshotSourceFiles(a);
      const r2 = snapshotSourceFiles(b);
      try {
        expect(r1.digest).toBe(r2.digest);
        const ws1 = mkdtempSync(join(tmpdir(), "flagagent-ws-"));
        stageSourceFiles(ws1, r1.files);
        const modePlain = statSync(join(ws1, "file.txt")).mode & 0o111;
        rmSync(ws1, { recursive: true, force: true });
        expect(modePlain).toBe(0);
        const ws2 = mkdtempSync(join(tmpdir(), "flagagent-ws-"));
        stageSourceFiles(ws2, r2.files);
        const modeExec = statSync(join(ws2, "file.txt")).mode & 0o111;
        rmSync(ws2, { recursive: true, force: true });
        expect(modeExec).not.toBe(0);
      } finally {
        r1.cleanup();
        r2.cleanup();
      }
    } finally {
      rmSync(tmp, { recursive: true, force: true });
    }
  });

  it("stage respects deadline", () => {
    const tmp = mkdtempSync(join(tmpdir(), "flagagent-"));
    try {
      const src = join(tmp, "src");
      mkdirSync(src);
      writeFileSync(join(src, "a.txt"), "a");
      writeFileSync(join(src, "b.txt"), "b");
      const snap = snapshotSourceFiles(src);
      try {
        const ws = mkdtempSync(join(tmpdir(), "flagagent-ws-"));
        let calls = 0;
        const expired = () => {
          calls++;
          return calls > 1;
        };
        stageSourceFiles(ws, snap.files, expired);
        rmSync(ws, { recursive: true, force: true });
      } finally {
        snap.cleanup();
      }
    } finally {
      rmSync(tmp, { recursive: true, force: true });
    }
  });

  it("limits enforced", () => {
    const tmp = mkdtempSync(join(tmpdir(), "flagagent-"));
    try {
      const src = join(tmp, "src");
      mkdirSync(src);
      writeFileSync(join(src, "big.bin"), "x".repeat(11));
      expect(() =>
        snapshotSourceFiles(src, new Limits({ maxSourceFileBytes: 10 })),
      ).toThrow(InvalidChallengeSourceError);
    } finally {
      rmSync(tmp, { recursive: true, force: true });
    }
  });

  it("fifo rejected", async () => {
    const tmp = mkdtempSync(join(tmpdir(), "flagagent-"));
    try {
      const src = join(tmp, "src");
      mkdirSync(src);
      const fifo = join(src, "fifo");
      const { execSync: execSyncFn } = await import("node:child_process");
      try {
        execSyncFn(`mkfifo ${fifo}`);
      } catch {
        return;
      }
      expect(() => snapshotSourceFiles(src)).toThrow(InvalidChallengeSourceError);
    } finally {
      rmSync(tmp, { recursive: true, force: true });
    }
  });

  it("accepts consecutive-dot filenames and directories (issue #61)", async () => {
    const { readFileSync } = await import("node:fs");
    const tmp = mkdtempSync(join(tmpdir(), "flagagent-"));
    try {
      const src = join(tmp, "src");
      mkdirSync(src);
      writeFileSync(join(src, "foo..bar"), "content-foo");
      writeFileSync(join(src, "notes..txt"), "content-notes");
      mkdirSync(join(src, "dir..name"));
      writeFileSync(join(src, "dir..name", "file.txt"), "content-nested");
      const snap = snapshotSourceFiles(src);
      try {
        const rels = snap.files.map(([, rel]) => rel).sort();
        expect(rels).toEqual(["dir..name/file.txt", "foo..bar", "notes..txt"]);
        const ws = mkdtempSync(join(tmpdir(), "flagagent-ws-"));
        try {
          stageSourceFiles(ws, snap.files);
          expect(readFileSync(join(ws, "foo..bar"), "utf8")).toBe("content-foo");
          expect(readFileSync(join(ws, "notes..txt"), "utf8")).toBe("content-notes");
          expect(readFileSync(join(ws, "dir..name", "file.txt"), "utf8")).toBe(
            "content-nested",
          );
        } finally {
          rmSync(ws, { recursive: true, force: true });
        }
      } finally {
        snap.cleanup();
      }
    } finally {
      rmSync(tmp, { recursive: true, force: true });
    }
  });
});
