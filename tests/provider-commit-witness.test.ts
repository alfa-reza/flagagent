import { describe, it, expect } from "vitest";
import { ChatCompletionsModel } from "../src/flagagent/providers/chat.js";
import { ResponsesModel } from "../src/flagagent/providers/responses.js";
import { AnthropicMessagesModel } from "../src/flagagent/providers/anthropic.js";
import { TOOL_DEFINITIONS } from "../src/flagagent/tools.js";

function chatOk(content = "hi") {
  return {
    choices: [{ message: { content, tool_calls: null }, finish_reason: "stop" }],
    usage: { prompt_tokens: 1, completion_tokens: 1 },
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
    usage: { input_tokens: 1, output_tokens: 1 },
  };
}
function anthropicOk() {
  return {
    stop_reason: "end_turn",
    content: [{ type: "text", text: "hi" }],
    usage: { input_tokens: 1, output_tokens: 1 },
  };
}

void anthropicOk;
describe("provider commit witness ordering", () => {
  it("Chat: capture happens after parse and before return", async () => {
    let capturedAt: number | undefined = undefined;
    const completions = {
      create: async () => chatOk("parsed-ok"),
    };
    const client = { chat: { completions } } as unknown;
    const model = new ChatCompletionsModel({ model: "m", apiKey: "sk", client });
    const mono = 10;
    model.setMonotonic(() => mono);
    const res = await model.generate(
      [{ role: "user", content: "hi" }],
      TOOL_DEFINITIONS as never,
    );
    capturedAt = model.lastCommittedAt;
    expect(res.content).toBe("parsed-ok");
    expect(capturedAt).toBe(10);
    expect(model.lastCommittedAt).toBe(10);
  });

  it("Responses: builtInput updated before witness", async () => {
    const fake = {
      create: async () => responsesOk(),
    };
    const client = { responses: fake } as unknown;
    const model = new ResponsesModel({ model: "m", apiKey: "sk", client });
    const mono = 20;
    model.setMonotonic(() => mono);
    const before = (model as unknown as { builtInput: unknown[] }).builtInput.length;
    const res = await model.generate(
      [{ role: "user", content: "hi" }],
      TOOL_DEFINITIONS as never,
    );
    expect(res.content).toBe("hi");
    expect(model.lastCommittedAt).toBe(20);
    const after = (model as unknown as { builtInput: unknown[] }).builtInput.length;
    expect(after).toBeGreaterThan(before);
  });

  it("Anthropic: thinkingHistory updated before witness", async () => {
    const resp = {
      stop_reason: "end_turn",
      content: [
        { type: "thinking", thinking: "plan", signature: "sig-1" },
        { type: "text", text: "hi" },
      ],
      usage: { input_tokens: 1, output_tokens: 1 },
    };
    const messages = { create: async () => resp };
    const client = { messages } as unknown;
    const model = new AnthropicMessagesModel({ model: "m", apiKey: "sk", client });
    const mono = 30;
    model.setMonotonic(() => mono);
    const res = await model.generate(
      [{ role: "user", content: "hi" }],
      TOOL_DEFINITIONS as never,
    );
    expect(res.content).toBe("hi");
    expect(model.lastCommittedAt).toBe(30);
    const hist = (model as unknown as { thinkingHistory: unknown[][] }).thinkingHistory;
    expect(hist.length).toBe(1);
    expect((hist[0]![0] as Record<string, unknown>).signature).toBe("sig-1");
  });

  it("complete error captures witness before throwing", async () => {
    const completions = {
      create: async () => {
        throw new Error("network fail");
      },
    };
    const client = { chat: { completions } } as unknown;
    const model = new ChatCompletionsModel({ model: "m", apiKey: "sk", client });
    const mono = 40;
    model.setMonotonic(() => mono);
    await expect(
      model.generate([{ role: "user", content: "hi" }], TOOL_DEFINITIONS as never),
    ).rejects.toThrow();
    expect(model.lastCommittedAt).toBe(40);
  });

  it("post-deadline: loop would reject if committedAt >= deadline (no tool start)", async () => {
    const completions = {
      create: async () => chatOk("late"),
    };
    const client = { chat: { completions } } as unknown;
    const model = new ChatCompletionsModel({ model: "m", apiKey: "sk", client });
    let mono = 100;
    model.setMonotonic(() => mono);
    mono = 1000;
    const res = await model.generate(
      [{ role: "user", content: "hi" }],
      TOOL_DEFINITIONS as never,
    );
    expect(res.content).toBe("late");
    expect(model.lastCommittedAt).toBe(1000);
  });
});
