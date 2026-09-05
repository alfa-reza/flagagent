import { describe, it, expect } from "vitest";
import { mkdtempSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { AgentLoop } from "../src/flagagent/loop.js";
import { Limits } from "../src/flagagent/limits.js";
import {
  ExactStringVerifier,
  ShellResult,
  TOOL_DEFINITIONS,
} from "../src/flagagent/tools.js";
import { ModelResponse, ScriptedModel } from "../src/flagagent/model.js";
import { ChatCompletionsModel } from "../src/flagagent/providers/chat.js";
import { ResponsesModel } from "../src/flagagent/providers/responses.js";
import { AnthropicMessagesModel } from "../src/flagagent/providers/anthropic.js";

const NOW = new Date("2026-08-14T16:15:30.000Z");
const IDENTITY = "IDENTITY_SENTINEL_7f3a";
const DESCRIPTION = "Inspect the evidence and solve the challenge.";
const TARGET_CTX = "TARGET_CTX_SENTINEL_9e2b";
const FLAG_SECRET = "FLAG_SENTINEL_SECRET_4d1c";

async function canonicalUserMessages() {
  const tmp = mkdtempSync(join(tmpdir(), "flagagent-identity-"));
  try {
    const model = new ScriptedModel([new ModelResponse("done")]);
    const loop = new AgentLoop({
      model: model as never,
      executor: {
        execute: async () => new ShellResult("", "", 0, false),
      } as never,
      verifier: new ExactStringVerifier(FLAG_SECRET) as never,
      challenge: {
        identity: IDENTITY,
        description: DESCRIPTION,
        targetContext: TARGET_CTX,
      },
      limits: new Limits({
        maxModelTurns: 2,
        wallTimeoutSeconds: 100,
        commandTimeoutSeconds: 10,
      }),
      runsRoot: tmp,
      monotonic: () => 0,
      utcNow: () => NOW,
      runId: "FA-20260814T161530Z-a13f4c2d",
    });
    await loop.run();
    const sent = model.calls[0]![0] as Record<string, unknown>[];
    return sent.filter((m) => m.role === "user");
  } finally {
    rmSync(tmp, { recursive: true, force: true });
  }
}

describe("challenge identity is model-visible", () => {
  it("canonical user content contains identity, description, and target context", async () => {
    const users = await canonicalUserMessages();
    expect(users.length).toBe(1);
    const content = users[0]!.content as string;
    expect(content).toContain(IDENTITY);
    expect(content).toContain(DESCRIPTION);
    expect(content).toContain(TARGET_CTX);
    expect(content).not.toContain(FLAG_SECRET);
    expect(users[0]).not.toHaveProperty("challenge_identity");
  });

  it("survives openai-chat serialization", async () => {
    const users = await canonicalUserMessages();
    let seen: unknown = null;
    const completions = {
      create: async (params: unknown) => {
        seen = params;
        return {
          choices: [
            { message: { content: "ok", tool_calls: null }, finish_reason: "stop" },
          ],
          usage: null,
        };
      },
    };
    const model = new ChatCompletionsModel({
      model: "test",
      apiKey: "sk-test",
      client: { chat: { completions } } as unknown,
    });
    await model.generate(users as never, TOOL_DEFINITIONS as never);
    const text = JSON.stringify(seen);
    expect(text).toContain(IDENTITY);
    expect(text).toContain(DESCRIPTION);
    expect(text).not.toContain("challenge_identity");
  });

  it("survives openai-responses serialization", async () => {
    const users = await canonicalUserMessages();
    let seen: unknown = null;
    const responses = {
      create: async (params: unknown) => {
        seen = params;
        return {
          status: "completed",
          output: [
            {
              type: "message",
              id: "msg_1",
              role: "assistant",
              status: "completed",
              content: [{ type: "output_text", text: "ok" }],
            },
          ],
          usage: null,
        };
      },
    };
    const model = new ResponsesModel({
      model: "test",
      apiKey: "sk-test",
      client: { responses } as unknown,
    });
    await model.generate(users as never, TOOL_DEFINITIONS as never);
    const text = JSON.stringify(seen);
    expect(text).toContain(IDENTITY);
    expect(text).toContain(DESCRIPTION);
    expect(text).not.toContain("challenge_identity");
  });

  it("survives anthropic serialization without promoting identity to system", async () => {
    const users = await canonicalUserMessages();
    let seen: unknown = null;
    const messages = {
      create: async (params: unknown) => {
        seen = params;
        return {
          stop_reason: "end_turn",
          content: [{ type: "text", text: "ok" }],
          usage: null,
        };
      },
    };
    const model = new AnthropicMessagesModel({
      model: "test",
      apiKey: "sk-test",
      client: { messages } as unknown,
    });
    await model.generate(users as never, TOOL_DEFINITIONS as never);
    const params = seen as Record<string, unknown>;
    expect(JSON.stringify(params.messages)).toContain(IDENTITY);
    expect(JSON.stringify(params.messages)).toContain(DESCRIPTION);
    expect(JSON.stringify(seen)).not.toContain("challenge_identity");
    expect(params.system ?? "").not.toContain(IDENTITY);
  });
});
