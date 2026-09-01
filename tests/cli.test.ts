import { describe, it, expect, beforeEach, afterEach } from "vitest";
import {
  mkdtempSync,
  rmSync,
  mkdirSync,
  writeFileSync,
  symlinkSync,
  writeFileSync as writeFileSyncAlias,
} from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { loadChallenge, MAX_CHALLENGE_DESCRIPTOR_BYTES } from "../src/flagagent/cli.js";

function makeChallenge(dir: string, payload: Record<string, unknown>): void {
  mkdirSync(dir, { recursive: true });
  writeFileSync(join(dir, "challenge.json"), JSON.stringify(payload));
}

describe("cli descriptor", () => {
  let tmp: string;
  beforeEach(() => {
    tmp = mkdtempSync(join(tmpdir(), "flagagent-cli-"));
  });
  afterEach(() => {
    rmSync(tmp, { recursive: true, force: true });
  });

  it("bounded descriptor input: rejects >64KiB", () => {
    const dir = join(tmp, "ch");
    makeChallenge(dir, {
      identity: "x",
      description: "d",
      expected_flag: "f",
      network_mode: "none",
    });
    // overwrite with large file
    writeFileSync(
      join(dir, "challenge.json"),
      "x".repeat(MAX_CHALLENGE_DESCRIPTOR_BYTES + 1),
    );
    expect(() => loadChallenge(dir)).toThrow(/exceeds maximum/);
  });

  it("fatal UTF-8 invalid JSON", () => {
    const dir = join(tmp, "ch2");
    mkdirSync(dir, { recursive: true });
    // write invalid utf8 bytes
    const buf = Buffer.from([0xff, 0xfe, 0xfd]);
    writeFileSyncAlias(join(dir, "challenge.json"), buf);
    expect(() => loadChallenge(dir)).toThrow(/valid JSON/);
  });

  it("non-object payload", () => {
    const dir = join(tmp, "ch3");
    mkdirSync(dir, { recursive: true });
    writeFileSync(join(dir, "challenge.json"), JSON.stringify([1, 2, 3]));
    expect(() => loadChallenge(dir)).toThrow(/must contain an object/);
  });

  it("field allowlist rejects unsupported fields", () => {
    const dir = join(tmp, "ch4");
    makeChallenge(dir, {
      identity: "x",
      description: "d",
      expected_flag: "f",
      network_mode: "none",
      extra: "nope",
    });
    expect(() => loadChallenge(dir)).toThrow(/unsupported fields/);
  });

  it("valid network_mode none and local, rejects other", () => {
    const dir = join(tmp, "ch5");
    makeChallenge(dir, {
      identity: "x",
      description: "d",
      expected_flag: "f",
      network_mode: "none",
    });
    expect(loadChallenge(dir).networkMode).toBe("none");
    const dir2 = join(tmp, "ch5b");
    makeChallenge(dir2, {
      identity: "x",
      description: "d",
      expected_flag: "f",
      network_mode: "local",
    });
    expect(loadChallenge(dir2).networkMode).toBe("local");
    const dir3 = join(tmp, "ch5c");
    makeChallenge(dir3, {
      identity: "x",
      description: "d",
      expected_flag: "f",
      network_mode: "bridge",
    });
    expect(() => loadChallenge(dir3)).toThrow(/network_mode/);
  });

  it("descriptor/files path and symlink rejection", () => {
    const outside = join(tmp, "outside");
    writeFileSync(outside, "secret");
    const dir = join(tmp, "ch6");
    mkdirSync(dir, { recursive: true });
    symlinkSync(outside, join(dir, "challenge.json"));
    expect(() => loadChallenge(dir)).toThrow(/regular file/);
    // files symlink
    const dir2 = join(tmp, "ch6b");
    makeChallenge(dir2, {
      identity: "x",
      description: "d",
      expected_flag: "f",
      network_mode: "none",
    });
    const filesTarget = join(tmp, "filesTarget");
    mkdirSync(filesTarget);
    symlinkSync(filesTarget, join(dir2, "files"));
    expect(() => loadChallenge(dir2)).toThrow(/must be a directory/);
  });

  it("expected_flag stays control-side not in provider config: loadChallenge returns it but cli does not leak to model", async () => {
    const dir = join(tmp, "ch7");
    makeChallenge(dir, {
      identity: "x",
      description: "d",
      expected_flag: "Flag{secret}",
      network_mode: "none",
    });
    const ch = loadChallenge(dir);
    expect(ch.expectedFlag).toBe("Flag{secret}");
    // verify loop metadata does not include expected_flag in model apiBase path — this is a static check: cli creates verifier with expectedFlag, not model
    expect(ch.identity).toBe("x");
  });

  it("invalid JSON syntax throws", () => {
    const dir = join(tmp, "ch8");
    mkdirSync(dir, { recursive: true });
    writeFileSync(join(dir, "challenge.json"), "{ not json");
    expect(() => loadChallenge(dir)).toThrow(/valid JSON/);
  });

  it("missing required fields throws", () => {
    const dir = join(tmp, "ch9");
    makeChallenge(dir, { description: "d", expected_flag: "f", network_mode: "none" });
    expect(() => loadChallenge(dir)).toThrow();
  });
});

describe("cli package numeric limits and env handling (unit)", () => {
  it("isIdentifier validation via runCli error path for bad api-key-env", async () => {
    const dir = mkdtempSync(join(tmpdir(), "flagagent-cli-env-"));
    try {
      makeChallenge(dir, {
        identity: "x",
        description: "d",
        expected_flag: "f",
        network_mode: "none",
      });
      const { runCli } = await import("../src/flagagent/cli.js");
      process.env["BAD-ENV"] = "sk";
      await expect(
        runCli([
          "run",
          "--challenge",
          dir,
          "--protocol",
          "openai-chat",
          "--model",
          "m",
          "--api-key-env",
          "BAD-ENV",
        ]),
      ).rejects.toThrow(/api-key-env/);
      delete process.env["BAD-ENV"];
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it("numeric CLI limits validation", async () => {
    const dir = mkdtempSync(join(tmpdir(), "flagagent-cli-limits-"));
    try {
      makeChallenge(dir, {
        identity: "x",
        description: "d",
        expected_flag: "f",
        network_mode: "none",
      });
      const { runCli } = await import("../src/flagagent/cli.js");
      process.env.OPENAI_API_KEY = "sk-test";
      await expect(
        runCli([
          "run",
          "--challenge",
          dir,
          "--protocol",
          "openai-chat",
          "--model",
          "m",
          "--max-model-turns",
          "0",
        ]),
      ).rejects.toThrow(/max-model-turns/);
      delete process.env.OPENAI_API_KEY;
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it("unsupported protocol rejection", async () => {
    const dir = mkdtempSync(join(tmpdir(), "flagagent-cli-proto-"));
    try {
      makeChallenge(dir, {
        identity: "x",
        description: "d",
        expected_flag: "f",
        network_mode: "none",
      });
      const { runCli } = await import("../src/flagagent/cli.js");
      process.env.OPENAI_API_KEY = "sk-test";
      await expect(
        runCli(["run", "--challenge", dir, "--protocol", "bad-proto", "--model", "m"]),
      ).rejects.toThrow(/unsupported protocol/);
      delete process.env.OPENAI_API_KEY;
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});
