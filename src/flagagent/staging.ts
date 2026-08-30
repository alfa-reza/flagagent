import { createHash } from "node:crypto";
import { createWriteStream } from "node:fs";
import {
  chmodSync,
  closeSync,
  constants,
  mkdirSync,
  mkdtempSync,
  openSync,
  readSync,
  rmSync,
  statSync,
  writeSync,
} from "node:fs";
import { opendirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Limits } from "./limits.js";

export class InvalidChallengeSourceError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "InvalidChallengeSourceError";
  }
}

function isSymlinkMode(mode: number): boolean {
  return (mode & 0o170000) === 0o120000;
}
function isDirMode(mode: number): boolean {
  return (mode & 0o170000) === 0o040000;
}
function isRegMode(mode: number): boolean {
  return (mode & 0o170000) === 0o100000;
}

function procFdPath(fd: number, name?: string): string {
  return name == null ? `/proc/self/fd/${fd}` : `/proc/self/fd/${fd}/${name}`;
}

export function snapshotSourceFiles(
  sourceDir: string | null | undefined,
  limits: Limits = new Limits(),
): {
  files: Array<[string, string]>;
  digest: string | null;
  tmpDir: string | null;
  cleanup: () => void;
} {
  if (sourceDir == null)
    return { files: [], digest: null, tmpDir: null, cleanup: () => {} };
  const st = (() => {
    try {
      return statSync(sourceDir);
    } catch {
      throw new InvalidChallengeSourceError("challenge source_dir must be a directory");
    }
  })();
  void st;
  const tmpBase = mkdtempSync(join(tmpdir(), "flagagent-source-"));
  const cleanup = () => {
    try {
      rmSync(tmpBase, { recursive: true, force: true });
    } catch {
      /* ignore */
    }
  };
  let rootFd: number | null = null;
  try {
    rootFd = openSync(
      sourceDir,
      constants.O_RDONLY | constants.O_DIRECTORY | constants.O_NOFOLLOW,
    );
  } catch {
    cleanup();
    throw new InvalidChallengeSourceError("challenge source_dir must be a directory");
  }

  const files: Array<[string, string]> = [];
  let totalBytes = 0;
  let fileCount = 0;
  let totalEntries = 0;

  const visit = (dirFd: number, relative: string, depth: number): void => {
    if (depth > limits.maxSourceDepth)
      throw new InvalidChallengeSourceError("challenge source too deep");
    let dir: ReturnType<typeof opendirSync> | null = null;
    const names: string[] = [];
    try {
      dir = opendirSync(procFdPath(dirFd));
      let entry: import("node:fs").Dirent | null;
      while ((entry = dir.readSync()) !== null) {
        totalEntries++;
        if (totalEntries > limits.maxSourceEntries)
          throw new InvalidChallengeSourceError(
            "challenge source has too many entries",
          );
        names.push(entry.name);
      }
    } catch (e) {
      if (e instanceof InvalidChallengeSourceError) throw e;
      throw new InvalidChallengeSourceError("challenge source cannot be read");
    } finally {
      try {
        dir?.closeSync();
      } catch {
        /* ignore */
      }
    }
    for (const name of names.sort()) {
      const entryRelative = relative ? `${relative}/${name}` : name;
      if (entryRelative.includes("..") || entryRelative.startsWith("/"))
        throw new InvalidChallengeSourceError("challenge source path is unsafe");
      let entryMode: number;
      try {
        const s = statSync(procFdPath(dirFd, name), { throwIfNoEntry: true });
        if (s.isSymbolicLink())
          throw new InvalidChallengeSourceError("challenge source contains a symlink");
        entryMode = s.mode;
      } catch (e) {
        if (e instanceof InvalidChallengeSourceError) throw e;
        throw new InvalidChallengeSourceError("challenge source cannot be inspected");
      }
      if (isSymlinkMode(entryMode))
        throw new InvalidChallengeSourceError("challenge source contains a symlink");
      if (isDirMode(entryMode)) {
        let childFd: number | null = null;
        try {
          childFd = openSync(
            procFdPath(dirFd, name),
            constants.O_RDONLY | constants.O_DIRECTORY | constants.O_NOFOLLOW,
          );
        } catch {
          throw new InvalidChallengeSourceError("challenge source directory changed");
        }
        try {
          visit(childFd, entryRelative, depth + 1);
        } finally {
          if (childFd != null)
            try {
              closeSync(childFd);
            } catch {
              /* ignore */
            }
        }
      } else if (isRegMode(entryMode)) {
        if (fileCount >= limits.maxSourceFiles)
          throw new InvalidChallengeSourceError("challenge source has too many files");
        const snapshotPath = join(tmpBase, entryRelative);
        mkdirSync(join(snapshotPath, ".."), { recursive: true });
        let srcFd: number | null = null;
        try {
          srcFd = openSync(
            procFdPath(dirFd, name),
            constants.O_RDONLY | constants.O_NOFOLLOW,
          );
        } catch {
          throw new InvalidChallengeSourceError("challenge source file changed");
        }
        let execBits = 0;
        try {
          const fst = statSync(procFdPath(srcFd));
          if (!isRegMode(fst.mode))
            throw new InvalidChallengeSourceError(
              "challenge source contains a special file",
            );
          execBits = fst.mode & 0o111;
          const logicalSize = Number(fst.size);
          if (logicalSize > limits.maxSourceFileBytes)
            throw new InvalidChallengeSourceError("challenge source file too large");
          if (totalBytes + logicalSize > limits.maxSourceTotalBytes)
            throw new InvalidChallengeSourceError("challenge source too large");
          const buf = Buffer.allocUnsafe(1024 * 1024);
          let fileBytes = 0;
          let dstFd: number | null = null;
          try {
            dstFd = openSync(
              snapshotPath,
              constants.O_WRONLY | constants.O_CREAT | constants.O_TRUNC,
              0o644,
            );
            while (true) {
              const n = readSync(srcFd, buf, 0, buf.length, null);
              if (n === 0) break;
              if (fileBytes + n > limits.maxSourceFileBytes)
                throw new InvalidChallengeSourceError(
                  "challenge source file too large",
                );
              if (totalBytes + n > limits.maxSourceTotalBytes)
                throw new InvalidChallengeSourceError("challenge source too large");
              let written = 0;
              while (written < n) {
                const w = writeSync(dstFd, buf, written, n - written);
                written += w;
              }
              fileBytes += n;
              totalBytes += n;
            }
          } finally {
            if (dstFd != null)
              try {
                closeSync(dstFd);
              } catch {
                /* ignore */
              }
          }
          if (execBits) {
            try {
              const snapMode = statSync(snapshotPath).mode;
              chmodSync(snapshotPath, snapMode | execBits);
            } catch {
              throw new InvalidChallengeSourceError(
                "challenge source cannot be inspected",
              );
            }
          }
        } finally {
          if (srcFd != null)
            try {
              closeSync(srcFd);
            } catch {
              /* ignore */
            }
        }
        files.push([snapshotPath, entryRelative]);
        fileCount++;
      } else {
        throw new InvalidChallengeSourceError(
          "challenge source contains a special file",
        );
      }
    }
  };

  try {
    visit(rootFd, "", 0);
  } catch (e) {
    try {
      closeSync(rootFd);
    } catch {
      /* ignore */
    }
    cleanup();
    throw e;
  }
  try {
    closeSync(rootFd);
  } catch {
    /* ignore */
  }

  const hash = createHash("sha256");
  hash.update(Buffer.from("FLAGAGENT-SOURCE-V1", "utf8"));
  const sorted = [...files].sort((a, b) => (a[1] < b[1] ? -1 : a[1] > b[1] ? 1 : 0));
  for (const [snap, rel] of sorted) {
    const relBytes = Buffer.from(rel, "utf8");
    const size = statSync(snap).size;
    const lenBuf = Buffer.allocUnsafe(8);
    lenBuf.writeBigUInt64BE(BigInt(relBytes.length), 0);
    hash.update(lenBuf);
    hash.update(relBytes);
    const sizeBuf = Buffer.allocUnsafe(8);
    sizeBuf.writeBigUInt64BE(BigInt(size), 0);
    hash.update(sizeBuf);
    const fd = openSync(snap, constants.O_RDONLY);
    try {
      const buf = Buffer.allocUnsafe(1024 * 1024);
      while (true) {
        const n = readSync(fd, buf, 0, buf.length, null);
        if (n === 0) break;
        hash.update(buf.subarray(0, n));
      }
    } finally {
      try {
        closeSync(fd);
      } catch {
        /* ignore */
      }
    }
  }
  return { files, digest: hash.digest("hex"), tmpDir: tmpBase, cleanup };
}

export function stageSourceFiles(
  workspace: string,
  files: Array<[string, string]>,
  expired?: () => boolean,
): void {
  for (const [src, rel] of files) {
    if (expired?.()) break;
    const dest = join(workspace, rel);
    mkdirSync(join(dest, ".."), { recursive: true });
    const srcFd = openSync(src, constants.O_RDONLY);
    let dstFd: number | null = null;
    try {
      dstFd = openSync(
        dest,
        constants.O_WRONLY | constants.O_CREAT | constants.O_TRUNC,
        0o644,
      );
      const buf = Buffer.allocUnsafe(1024 * 1024);
      while (true) {
        const n = readSync(srcFd, buf, 0, buf.length, null);
        if (n === 0) break;
        let written = 0;
        while (written < n) written += writeSync(dstFd, buf, written, n - written);
      }
    } finally {
      try {
        closeSync(srcFd);
      } catch {
        /* ignore */
      }
      if (dstFd != null)
        try {
          closeSync(dstFd);
        } catch {
          /* ignore */
        }
    }
    const mode = statSync(src).mode;
    const execBits = mode & 0o111;
    if (execBits) {
      const destMode = statSync(dest).mode;
      chmodSync(dest, destMode | execBits);
    }
  }
  void createWriteStream;
}
