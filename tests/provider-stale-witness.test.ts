import { describe, it, expect } from "vitest";
import {
  ChatCompletionsModel,
  ProviderError,
} from "../src/flagagent/providers/chat.js";
import { AnthropicMessagesModel } from "../src/flagagent/providers/anthropic.js";
import { ResponsesModel } from "../src/flagagent/providers/responses.js";
import { TOOL_DEFINITIONS } from "../src/flagagent/tools.js";

function chatResponse(content = "hi") {
  return {
    choices: [{ message: { content, tool_calls: null }, finish_reason: "stop" }],
    usage: null,
  };
}
function anthropicResponse(content = "hi") {
  return {
    stop_reason: "end_turn",
    content: [{ type: "text", text: content }],
    usage: null,
  };
}
function responsesOk() {
  return {
    status: "completed",
    output: [
      {
        type: "message",
        id: "msg_1",
        role: "assistant",
        status: "completed",
        content: [{ type: "output_text", text: "hi" }],
      },
    ],
    usage: null,
  };
}

describe("current-invocation witness: no reuse of T1 on second invocation", () => {
  it("Chat: second invocation fails before transport still not expose T1", async () => {
    let monotonic = 100;
    const completions = {
      create: async () => chatResponse("first"),
    };
    const client = { chat: { completions } } as unknown;
    const model = new ChatCompletionsModel({ model: "m", apiKey: "sk", client });
    model.setMonotonic(() => monotonic);
    await model.generate([{ role: "user", content: "hi" }], TOOL_DEFINITIONS as never);
    expect(model.lastCommittedAt).toBe(100);
    monotonic = 200;
    model.setRemaining(0);
    await expect(
      model.generate([{ role: "user", content: "hi2" }], TOOL_DEFINITIONS as never),
    ).rejects.toThrow(ProviderError);
    expect(model.lastCommittedAt).toBe(200);
    expect(model.lastCommittedAt).not.toBe(100);
  });

  it("Anthropic: second invocation fails in pre-transport serialization still records new witness", async () => {
    let monotonic = 100;
    const messages = {
      create: async () => anthropicResponse("first"),
    };
    const client = { messages } as unknown;
    const model = new AnthropicMessagesModel({ model: "m", apiKey: "sk", client });
    model.setMonotonic(() => monotonic);
    await model.generate([{ role: "user", content: "hi" }], TOOL_DEFINITIONS as never);
    expect(model.lastCommittedAt).toBe(100);
    monotonic = 200;
    const badMessages: unknown[] = [{ role: "unsupported" as string, content: "x" }];
    await expect(
      model.generate(badMessages as never, TOOL_DEFINITIONS as never),
    ).rejects.toThrow();
    expect(model.lastCommittedAt).toBe(200);
  });

  it("Responses: second invocation fails with exhausted budget still records new witness", async () => {
    let monotonic = 100;
    const responses = { create: async () => responsesOk() };
    const client = { responses } as unknown;
    const model = new ResponsesModel({ model: "m", apiKey: "sk", client });
    model.setMonotonic(() => monotonic);
    await model.generate([{ role: "user", content: "hi" }], TOOL_DEFINITIONS as never);
    expect(model.lastCommittedAt).toBe(100);
    monotonic = 200;
    model.setRemaining(0);
    await expect(
      model.generate([{ role: "user", content: "hi2" }], TOOL_DEFINITIONS as never),
    ).rejects.toThrow(ProviderError);
    expect(model.lastCommittedAt).toBe(200);
  });

  it("successful second invocation overwrites stale T1 with new timestamp", async () => {
    let monotonic = 100;
    const completions = {
      create: async () => chatResponse("ok"),
    };
    const client = { chat: { completions } } as unknown;
    const model = new ChatCompletionsModel({ model: "m", apiKey: "sk", client });
    model.setMonotonic(() => monotonic);
    await model.generate([{ role: "user", content: "a" }], TOOL_DEFINITIONS as never);
    expect(model.lastCommittedAt).toBe(100);
    monotonic = 300;
    await model.generate([{ role: "user", content: "b" }], TOOL_DEFINITIONS as never);
    expect(model.lastCommittedAt).toBe(300);
  });
});
