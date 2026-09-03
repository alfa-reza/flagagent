import { describe, it, expect } from "vitest";
import { mkdtempSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { AgentLoop } from "../src/flagagent/loop.js";
import { Limits } from "../src/flagagent/limits.js";
import { ModelResponse, ToolCall, ScriptedModel } from "../src/flagagent/model.js";
import {
  ExactStringVerifier,
  ShellResult,
  SandboxError,
} from "../src/flagagent/tools.js";
import { readEvents } from "../src/flagagent/artifacts.js";

const NOW = new Date("2026-08-14T16:15:30.000Z");

function tmpDir(): string {
  return mkdtempSync(join(tmpdir(), "flagagent-sandbox-detail-"));
}

describe("SandboxError detail persistence (issue #68)", () => {
  it("prepare SandboxError persists detail and result remains error:sandbox_error", async () => {
    const tmp = tmpDir();
    try {
      const diag = `docker run failed: invalid mount config for type "bind": hoisted`;
      const executor = {
        prepare: async () => {
          throw new SandboxError(diag);
        },
        execute: async () => new ShellResult("", "", 0, false),
      };
      const loop = new AgentLoop({
        model: new ScriptedModel([new ModelResponse("unused")]),
        executor: executor as never,
        verifier: new ExactStringVerifier("Flag{ok}"),
        challenge: { identity: "fixture", description: "solve it" },
        limits: new Limits({
          maxModelTurns: 5,
          wallTimeoutSeconds: 100,
          commandTimeoutSeconds: 10,
        }),
        runsRoot: tmp,
        monotonic: () => 0,
        utcNow: () => NOW,
        runId: "FA-20260814T161530Z-a13f4c2d",
      });
      const result = await loop.run();
      expect(result.status).toBe("error");
      expect(result.reason).toBe("sandbox_error");
      expect(result["status:reason"]).toBe("error:sandbox_error");
      const events = readEvents(
        (loop as unknown as { artifacts: { eventsPath: string } }).artifacts.eventsPath,
      );
      const err = events.find((e) => e.type === "error")!;
      expect(err).toBeDefined();
      const payload = err.payload as Record<string, unknown>;
      expect(payload.reason).toBe("sandbox_error");
      expect(payload.operation).toBe("sandbox");
      expect(payload.detail).toBe(diag);
      expect(payload.call_id).toBeUndefined();
    } finally {
      rmSync(tmp, { recursive: true, force: true });
    }
  });

  it("shell SandboxError persists detail with call_id and result remains error:sandbox_error", async () => {
    const tmp = tmpDir();
    try {
      const diag = `docker exec failed: No such container`;
      const executor = {
        execute: async () => {
          throw new SandboxError(diag);
        },
      };
      const loop = new AgentLoop({
        model: new ScriptedModel([
          new ModelResponse("", [new ToolCall("c1", "shell", { command: "echo hi" })]),
        ]),
        executor: executor as never,
        verifier: new ExactStringVerifier("Flag{ok}"),
        challenge: { identity: "fixture", description: "solve it" },
        limits: new Limits({
          maxModelTurns: 5,
          wallTimeoutSeconds: 100,
          commandTimeoutSeconds: 10,
        }),
        runsRoot: tmp,
        monotonic: () => 0,
        utcNow: () => NOW,
        runId: "FA-20260814T161530Z-a13f4c2d",
      });
      const result = await loop.run();
      expect(result.status).toBe("error");
      expect(result.reason).toBe("sandbox_error");
      expect(result["status:reason"]).toBe("error:sandbox_error");
      const events = readEvents(
        (loop as unknown as { artifacts: { eventsPath: string } }).artifacts.eventsPath,
      );
      const err = events.find((e) => e.type === "error")!;
      const payload = err.payload as Record<string, unknown>;
      expect(payload.reason).toBe("sandbox_error");
      expect(payload.operation).toBe("sandbox");
      expect(payload.detail).toBe(diag);
      expect(payload.call_id).toBe("c1");
    } finally {
      rmSync(tmp, { recursive: true, force: true });
    }
  });

  it("generic prepare error does not expose detail", async () => {
    const tmp = tmpDir();
    try {
      const executor = {
        prepare: async () => {
          throw new Error("generic boom");
        },
        execute: async () => new ShellResult("", "", 0, false),
      };
      const loop = new AgentLoop({
        model: new ScriptedModel([new ModelResponse("unused")]),
        executor: executor as never,
        verifier: new ExactStringVerifier("Flag{ok}"),
        challenge: { identity: "fixture", description: "solve it" },
        limits: new Limits({
          maxModelTurns: 5,
          wallTimeoutSeconds: 100,
          commandTimeoutSeconds: 10,
        }),
        runsRoot: tmp,
        monotonic: () => 0,
        utcNow: () => NOW,
        runId: "FA-20260814T161530Z-a13f4c2d",
      });
      const result = await loop.run();
      expect(result["status:reason"]).toBe("error:sandbox_error");
      const events = readEvents(
        (loop as unknown as { artifacts: { eventsPath: string } }).artifacts.eventsPath,
      );
      const payload = events.find((e) => e.type === "error")!.payload as Record<
        string,
        unknown
      >;
      expect(payload.reason).toBe("sandbox_error");
      expect(payload.detail).toBeUndefined();
      expect(payload.call_id).toBeUndefined();
    } finally {
      rmSync(tmp, { recursive: true, force: true });
    }
  });

  it("generic shell error maps to tool_error without detail", async () => {
    const tmp = tmpDir();
    try {
      const executor = {
        execute: async () => {
          throw new Error("generic exec fail");
        },
      };
      const loop = new AgentLoop({
        model: new ScriptedModel([
          new ModelResponse("", [new ToolCall("c2", "shell", { command: "echo hi" })]),
        ]),
        executor: executor as never,
        verifier: new ExactStringVerifier("Flag{ok}"),
        challenge: { identity: "fixture", description: "solve it" },
        limits: new Limits({
          maxModelTurns: 5,
          wallTimeoutSeconds: 100,
          commandTimeoutSeconds: 10,
        }),
        runsRoot: tmp,
        monotonic: () => 0,
        utcNow: () => NOW,
        runId: "FA-20260814T161530Z-a13f4c2d",
      });
      const result = await loop.run();
      expect(result["status:reason"]).toBe("error:tool_error");
      const events = readEvents(
        (loop as unknown as { artifacts: { eventsPath: string } }).artifacts.eventsPath,
      );
      const payload = events.find((e) => e.type === "error")!.payload as Record<
        string,
        unknown
      >;
      expect(payload.reason).toBe("tool_error");
      expect(payload.detail).toBeUndefined();
      expect(payload.call_id).toBe("c2");
    } finally {
      rmSync(tmp, { recursive: true, force: true });
    }
  });

  it("wall deadline wins over SandboxError thrown during prepare", async () => {
    const tmp = tmpDir();
    try {
      let monotonicVal = 0;
      const monotonic = () => monotonicVal;
      const diag = "docker run failed: should be masked";
      const executor = {
        prepare: async () => {
          monotonicVal = 100;
          throw new SandboxError(diag);
        },
        execute: async () => new ShellResult("", "", 0, false),
      };
      const loop = new AgentLoop({
        model: new ScriptedModel([new ModelResponse("unused")]),
        executor: executor as never,
        verifier: new ExactStringVerifier("Flag{ok}"),
        challenge: { identity: "fixture", description: "solve it" },
        limits: new Limits({
          maxModelTurns: 5,
          wallTimeoutSeconds: 1,
          commandTimeoutSeconds: 10,
        }),
        runsRoot: tmp,
        monotonic,
        utcNow: () => NOW,
        runId: "FA-20260814T161530Z-a13f4c2d",
      });
      const result = await loop.run();
      expect(result["status:reason"]).toBe("unsolved:wall_limit");
      const events = readEvents(
        (loop as unknown as { artifacts: { eventsPath: string } }).artifacts.eventsPath,
      );
      expect(
        events.some(
          (e) => (e.payload as Record<string, unknown>).reason === "sandbox_error",
        ),
      ).toBe(false);
    } finally {
      rmSync(tmp, { recursive: true, force: true });
    }
  });

  it("wall deadline during shell execution masks SandboxError", async () => {
    const tmp = tmpDir();
    try {
      let monotonicVal = 0;
      const monotonic = () => monotonicVal;
      const diag = "docker exec failed: should be masked";
      const executor = {
        execute: async () => {
          monotonicVal = 10;
          throw new SandboxError(diag);
        },
      };
      const loop = new AgentLoop({
        model: new ScriptedModel([
          new ModelResponse("", [new ToolCall("c1", "shell", { command: "echo hi" })]),
        ]),
        executor: executor as never,
        verifier: new ExactStringVerifier("Flag{ok}"),
        challenge: { identity: "fixture", description: "solve it" },
        limits: new Limits({
          maxModelTurns: 5,
          wallTimeoutSeconds: 1,
          commandTimeoutSeconds: 10,
        }),
        runsRoot: tmp,
        monotonic,
        utcNow: () => NOW,
        runId: "FA-20260814T161530Z-a13f4c2d",
      });
      const result = await loop.run();
      expect(result["status:reason"]).toBe("unsolved:wall_limit");
      const events = readEvents(
        (loop as unknown as { artifacts: { eventsPath: string } }).artifacts.eventsPath,
      );
      expect(
        events.some(
          (e) => (e.payload as Record<string, unknown>).reason === "sandbox_error",
        ),
      ).toBe(false);
      expect(
        events.some((e) => (e.payload as Record<string, unknown>).detail === diag),
      ).toBe(false);
    } finally {
      rmSync(tmp, { recursive: true, force: true });
    }
  });
});
