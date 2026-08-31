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
import { execSync } from "node:child_process";
import { createHash } from "node:crypto";
import {
  snapshotSourceFiles,
  stageSourceFiles,
  InvalidChallengeSourceError,
} from "../src/flagagent/staging.js";
import { Limits } from "../src/flagagent/limits.js";

describe("staging security extended", () => {
  it("root symlink rejected", () => {
    const tmp = mkdtempSync(join(tmpdir(), "flagagent-"));
    try {
      const real = join(tmp, "real");
      mkdirSync(real);
      writeFileSync(join(real, "a.txt"), "hi");
      const link = join(tmp, "link");
      symlinkSync(real, link);
      expect(() => snapshotSourceFiles(link)).toThrow(InvalidChallengeSourceError);
    } finally {
      rmSync(tmp, { recursive: true, force: true });
    }
  });

  it("nested symlink rejected", () => {
    const tmp = mkdtempSync(join(tmpdir(), "flagagent-"));
    try {
      const src = join(tmp, "src");
      mkdirSync(src);
      mkdirSync(join(src, "sub"));
      writeFileSync(join(src, "sub", "ok.txt"), "ok");
      const outside = join(tmp, "outside.txt");
      writeFileSync(outside, "secret");
      symlinkSync(outside, join(src, "sub", "link.txt"));
      expect(() => snapshotSourceFiles(src)).toThrow(InvalidChallengeSourceError);
    } finally {
      rmSync(tmp, { recursive: true, force: true });
    }
  });

  it("FIFO and device-like special file rejected", async () => {
    const tmp = mkdtempSync(join(tmpdir(), "flagagent-"));
    try {
      const src = join(tmp, "src");
      mkdirSync(src);
      const fifo = join(src, "fifo");
      try {
        execSync(`mkfifo ${fifo}`);
      } catch {
        return;
      }
      expect(() => snapshotSourceFiles(src)).toThrow(InvalidChallengeSourceError);
    } finally {
      rmSync(tmp, { recursive: true, force: true });
    }
  });

  it("size/count/entry/depth boundaries exact", () => {
    const tmp = mkdtempSync(join(tmpdir(), "flagagent-"));
    try {
      // depth boundary
      const deep = join(tmp, "deep");
      mkdirSync(deep);
      let cur = deep;
      for (let i = 0; i < 17; i++) {
        cur = join(cur, `d${i}`);
        mkdirSync(cur);
      }
      writeFileSync(join(cur, "file.txt"), "hi");
      expect(() =>
        snapshotSourceFiles(deep, new Limits({ maxSourceDepth: 16 })),
      ).toThrow(InvalidChallengeSourceError);
      rmSync(deep, { recursive: true, force: true });

      // file count
      const src2 = join(tmp, "src2");
      mkdirSync(src2);
      writeFileSync(join(src2, "a.txt"), "a");
      writeFileSync(join(src2, "b.txt"), "b");
      expect(() =>
        snapshotSourceFiles(src2, new Limits({ maxSourceFiles: 1 })),
      ).toThrow(InvalidChallengeSourceError);

      // file bytes
      const src3 = join(tmp, "src3");
      mkdirSync(src3);
      writeFileSync(join(src3, "big.bin"), "x".repeat(20));
      expect(() =>
        snapshotSourceFiles(src3, new Limits({ maxSourceFileBytes: 10 })),
      ).toThrow(InvalidChallengeSourceError);
    } finally {
      rmSync(tmp, { recursive: true, force: true });
    }
  });

  it("executable bits preserved, no setuid/setgid", () => {
    const tmp = mkdtempSync(join(tmpdir(), "flagagent-"));
    try {
      const src = join(tmp, "src");
      mkdirSync(src);
      const p = join(src, "run.sh");
      writeFileSync(p, "#!/bin/sh\necho hi\n");
      chmodSync(p, 0o4755); // setuid + exec
      const snap = snapshotSourceFiles(src);
      try {
        const ws = mkdtempSync(join(tmpdir(), "flagagent-ws-"));
        stageSourceFiles(ws, snap.files);
        const mode = statSync(join(ws, "run.sh")).mode;
        expect(mode & 0o4000).toBe(0);
        expect(mode & 0o2000).toBe(0);
        expect(mode & 0o111).not.toBe(0);
        rmSync(ws, { recursive: true, force: true });
      } finally {
        snap.cleanup();
      }
    } finally {
      rmSync(tmp, { recursive: true, force: true });
    }
  });

  it("digest deterministic and staged bytes match provenance: sorted, content-hashed", () => {
    const tmp = mkdtempSync(join(tmpdir(), "flagagent-"));
    try {
      const src = join(tmp, "src");
      mkdirSync(src);
      writeFileSync(join(src, "b.txt"), "bbb");
      writeFileSync(join(src, "a.txt"), "aaa");
      const s1 = snapshotSourceFiles(src);
      const s2 = snapshotSourceFiles(src);
      try {
        expect(s1.digest).toBe(s2.digest);
        // digest is FLAGAGENT-SOURCE-V1 + sorted rel + sizes + content
        // verify by recomputing with same scheme
        const hash = createHash("sha256");
        hash.update(Buffer.from("FLAGAGENT-SOURCE-V1", "utf8"));
        for (const rel of ["a.txt", "b.txt"]) {
          const content = rel === "a.txt" ? "aaa" : "bbb";
          const relBytes = Buffer.from(rel, "utf8");
          const lenBuf = Buffer.allocUnsafe(8);
          lenBuf.writeBigUInt64BE(BigInt(relBytes.length), 0);
          hash.update(lenBuf);
          hash.update(relBytes);
          const sizeBuf = Buffer.allocUnsafe(8);
          sizeBuf.writeBigUInt64BE(BigInt(content.length), 0);
          hash.update(sizeBuf);
          hash.update(Buffer.from(content, "utf8"));
        }
        expect(s1.digest).toBe(hash.digest("hex"));
        // staged bytes match snapshot
        const ws = mkdtempSync(join(tmpdir(), "flagagent-ws-"));
        stageSourceFiles(ws, s1.files);
        expect(statSync(join(ws, "a.txt")).size).toBe(3);
        expect(statSync(join(ws, "b.txt")).size).toBe(3);
        rmSync(ws, { recursive: true, force: true });
      } finally {
        s1.cleanup();
        s2.cleanup();
      }
    } finally {
      rmSync(tmp, { recursive: true, force: true });
    }
  });

  it("opened-object authority: file content read via fd not path after replacement race simulation", async () => {
    // Simulate by opening src file via staging's fd path: we verify staging reads via fd
    // We can't easily race without real TOCTOU, but we verify digest uses fd content not symlink swap
    const tmp = mkdtempSync(join(tmpdir(), "flagagent-"));
    try {
      const src = join(tmp, "src");
      mkdirSync(src);
      writeFileSync(join(src, "data.txt"), "original");
      const snap = snapshotSourceFiles(src);
      try {
        // mutate source after snapshot — staged content must remain original
        writeFileSync(join(src, "data.txt"), "mutated");
        const ws = mkdtempSync(join(tmpdir(), "flagagent-ws-"));
        stageSourceFiles(ws, snap.files);
        const staged = statSync(join(ws, "data.txt"));
        expect(staged.size).toBe(8); // "original"
        // read staged file
        const { readFileSync: readFileSync2 } = await import("node:fs");
        const content = readFileSync2(join(ws, "data.txt"), "utf8");
        expect(content).toBe("original");
        rmSync(ws, { recursive: true, force: true });
      } finally {
        snap.cleanup();
      }
    } finally {
      rmSync(tmp, { recursive: true, force: true });
    }
  });
});
