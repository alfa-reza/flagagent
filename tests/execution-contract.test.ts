import { describe, it, expect } from "vitest";
import { mkdtempSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { AgentLoop } from "../src/flagagent/loop.js";
import { Limits } from "../src/flagagent/limits.js";
import { ModelResponse, ScriptedModel } from "../src/flagagent/model.js";
import { ShellResult } from "../src/flagagent/tools.js";

const NOW = new Date("2026-09-04T13:53:57.000Z");

async function firstModelInput(opts: {
  networkMode: string;
  limits: Limits;
  targetContext?: string;
}): Promise<{ messages: unknown[]; tools: unknown[] }> {
  const tmp = mkdtempSync(join(tmpdir(), "flagagent-contract-"));
  try {
    const model = new ScriptedModel([new ModelResponse("stop")]);
    const loop = new AgentLoop({
      model,
      executor: { execute: async () => new ShellResult("", "", 0, false) },
      verifier: null,
      challenge: {
        identity: "contract",
        description: "solve it",
        targetContext: opts.targetContext,
        networkMode: opts.networkMode,
      },
      limits: opts.limits,
      runsRoot: tmp,
      monotonic: () => 0,
      utcNow: () => NOW,
      runId: "FA-20260904T135357Z-42e2fdd8",
    });
    await loop.run();
    const messages = model.calls[0]?.[0] as unknown[];
    const tools = model.calls[0]?.[1] as unknown[];
    return { messages, tools };
  } finally {
    rmSync(tmp, { recursive: true, force: true });
  }
}

describe("solver-visible execution contract", () => {
  it("exposes non-default resolved limits with network none", async () => {
    const limits = new Limits({
      maxModelTurns: 7,
      wallTimeoutSeconds: 123,
      commandTimeoutSeconds: 17,
    });
    const { messages, tools } = await firstModelInput({
      networkMode: "none",
      limits,
    });
    const user = (messages as Record<string, unknown>[]).find(
      (m) => m.role === "user",
    ) as Record<string, unknown>;
    const content = String(user.content);
    expect(content).toContain("solve it");
    expect(content).toContain("Network mode: none");
    expect(content).toContain("No challenge network");
    expect(content).toContain("17 seconds");
    expect(content).toContain("123 seconds");
    expect(content).toContain("7");
    expect(content).not.toContain("60 seconds");
    expect(content).not.toContain("1800 seconds");
    const shell = (tools as Record<string, unknown>[]).find(
      (t) => (t as Record<string, unknown>).name === "shell",
    ) as Record<string, unknown>;
    expect(String(shell.description)).toContain("17 seconds");
  });

  it("describes local as internal network without Internet egress", async () => {
    const limits = new Limits({
      maxModelTurns: 7,
      wallTimeoutSeconds: 123,
      commandTimeoutSeconds: 17,
    });
    const { messages } = await firstModelInput({
      networkMode: "local",
      limits,
    });
    const user = (messages as Record<string, unknown>[]).find(
      (m) => m.role === "user",
    ) as Record<string, unknown>;
    const content = String(user.content);
    expect(content).toContain("Network mode: local");
    expect(content).toContain("internal challenge network");
    expect(content).not.toMatch(/external Internet access is available/i);
  });

  it("preserves description and target context", async () => {
    const limits = new Limits({
      maxModelTurns: 7,
      wallTimeoutSeconds: 123,
      commandTimeoutSeconds: 17,
    });
    const { messages } = await firstModelInput({
      networkMode: "none",
      limits,
      targetContext: "target:9999",
    });
    const user = (messages as Record<string, unknown>[]).find(
      (m) => m.role === "user",
    ) as Record<string, unknown>;
    const content = String(user.content);
    expect(content).toContain("solve it");
    expect(content).toContain("Target context:\ntarget:9999");
    expect(content).toContain("Execution constraints:");
  });
});
