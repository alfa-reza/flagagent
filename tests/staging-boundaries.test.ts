import { describe, it, expect } from "vitest";
import { mkdtempSync, rmSync, mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import {
  snapshotSourceFiles,
  InvalidChallengeSourceError,
} from "../src/flagagent/staging.js";
import { Limits } from "../src/flagagent/limits.js";

describe("staging exact boundaries", () => {
  it("aggregate size exact passes, +1 fails", () => {
    const tmp = mkdtempSync(join(tmpdir(), "flagagent-"));
    try {
      const src = join(tmp, "src");
      mkdirSync(src);
      writeFileSync(join(src, "a.txt"), "12345");
      writeFileSync(join(src, "b.txt"), "12345");
      const snap = snapshotSourceFiles(src, new Limits({ maxSourceTotalBytes: 10 }));
      snap.cleanup();
      const src2 = join(tmp, "src2");
      mkdirSync(src2);
      writeFileSync(join(src2, "a.txt"), "12345");
      writeFileSync(join(src2, "b.txt"), "123456");
      expect(() =>
        snapshotSourceFiles(src2, new Limits({ maxSourceTotalBytes: 10 })),
      ).toThrow(InvalidChallengeSourceError);
    } finally {
      rmSync(tmp, { recursive: true, force: true });
    }
  });

  it("entry count exact passes at limit, +1 fails", () => {
    const tmp = mkdtempSync(join(tmpdir(), "flagagent-"));
    try {
      const srcPass = join(tmp, "srcPass");
      mkdirSync(srcPass);
      writeFileSync(join(srcPass, "a.txt"), "x");
      writeFileSync(join(srcPass, "b.txt"), "x");
      const snap = snapshotSourceFiles(srcPass, new Limits({ maxSourceEntries: 2 }));
      expect(snap.files.length).toBe(2);
      snap.cleanup();

      const srcFail = join(tmp, "srcFail");
      mkdirSync(srcFail);
      writeFileSync(join(srcFail, "a.txt"), "x");
      writeFileSync(join(srcFail, "b.txt"), "x");
      writeFileSync(join(srcFail, "c.txt"), "x");
      expect(() =>
        snapshotSourceFiles(srcFail, new Limits({ maxSourceEntries: 2 })),
      ).toThrow(InvalidChallengeSourceError);
    } finally {
      rmSync(tmp, { recursive: true, force: true });
    }
  });

  it("success at exact file count limit, +1 fails", () => {
    const tmp = mkdtempSync(join(tmpdir(), "flagagent-"));
    try {
      const src = join(tmp, "src");
      mkdirSync(src);
      writeFileSync(join(src, "a.txt"), "a");
      writeFileSync(join(src, "b.txt"), "b");
      const snap = snapshotSourceFiles(src, new Limits({ maxSourceFiles: 2 }));
      snap.cleanup();
      const src2 = join(tmp, "src2");
      mkdirSync(src2);
      writeFileSync(join(src2, "a.txt"), "a");
      writeFileSync(join(src2, "b.txt"), "b");
      writeFileSync(join(src2, "c.txt"), "c");
      expect(() =>
        snapshotSourceFiles(src2, new Limits({ maxSourceFiles: 2 })),
      ).toThrow(InvalidChallengeSourceError);
    } finally {
      rmSync(tmp, { recursive: true, force: true });
    }
  });

  it("digest is deterministic and canonical framing sorted", async () => {
    const { createHash } = await import("node:crypto");
    const tmp = mkdtempSync(join(tmpdir(), "flagagent-"));
    try {
      const src = join(tmp, "src");
      mkdirSync(src);
      writeFileSync(join(src, "b.txt"), "bbb");
      writeFileSync(join(src, "a.txt"), "aaa");
      const s1 = snapshotSourceFiles(src);
      const s2 = snapshotSourceFiles(src);
      expect(s1.digest).toBe(s2.digest);
      const hash = createHash("sha256");
      hash.update(Buffer.from("FLAGAGENT-SOURCE-V1", "utf8"));
      for (const rel of ["a.txt", "b.txt"]) {
        const content = rel === "a.txt" ? "aaa" : "bbb";
        const relBytes = Buffer.from(rel, "utf8");
        const lb = Buffer.allocUnsafe(8);
        lb.writeBigUInt64BE(BigInt(relBytes.length), 0);
        hash.update(lb);
        hash.update(relBytes);
        const sb = Buffer.allocUnsafe(8);
        sb.writeBigUInt64BE(BigInt(content.length), 0);
        hash.update(sb);
        hash.update(Buffer.from(content, "utf8"));
      }
      expect(s1.digest).toBe(hash.digest("hex"));
      s1.cleanup();
      s2.cleanup();
    } finally {
      rmSync(tmp, { recursive: true, force: true });
    }
  });

  it("empty source digest is FLAGAGENT-SOURCE-V1 hash", async () => {
    const { createHash } = await import("node:crypto");
    const expected = createHash("sha256")
      .update(Buffer.from("FLAGAGENT-SOURCE-V1", "utf8"))
      .digest("hex");
    const tmp = mkdtempSync(join(tmpdir(), "flagagent-"));
    try {
      const src = join(tmp, "src");
      mkdirSync(src);
      const snap = snapshotSourceFiles(src);
      expect(snap.digest).toBe(expected);
      snap.cleanup();
    } finally {
      rmSync(tmp, { recursive: true, force: true });
    }
  });

  it("staged files match snapshot bytes exactly", async () => {
    const { readFileSync, statSync } = await import("node:fs");
    const { stageSourceFiles } = await import("../src/flagagent/staging.js");
    const tmp = mkdtempSync(join(tmpdir(), "flagagent-"));
    try {
      const src = join(tmp, "src");
      mkdirSync(src);
      writeFileSync(join(src, "a.txt"), "hello");
      const snap = snapshotSourceFiles(src);
      const ws = mkdtempSync(join(tmpdir(), "flagagent-ws-"));
      stageSourceFiles(ws, snap.files);
      expect(statSync(join(ws, "a.txt")).size).toBe(5);
      expect(readFileSync(join(ws, "a.txt"), "utf8")).toBe("hello");
      rmSync(ws, { recursive: true, force: true });
      snap.cleanup();
    } finally {
      rmSync(tmp, { recursive: true, force: true });
    }
  });
});
