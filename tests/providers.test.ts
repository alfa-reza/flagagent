import { describe, it, expect } from "vitest";
import {
  ChatCompletionsModel,
  ProviderError,
} from "../src/flagagent/providers/chat.js";
import { ResponsesModel } from "../src/flagagent/providers/responses.js";
import { AnthropicMessagesModel } from "../src/flagagent/providers/anthropic.js";
import { TOOL_DEFINITIONS } from "../src/flagagent/tools.js";

function fakeCompletions(script: unknown[]) {
  const calls: Record<string, unknown>[] = [];
  let idx = 0;
  const obj = {
    create: async (params: unknown, opts?: unknown) => {
      calls.push({ params, opts });
      const item = script[idx++];
      if (item instanceof Error) throw item;
      return item;
    },
    _calls: calls,
  };
  return obj;
}

function chatResponse(
  content: string | null = "hello",
  toolCalls: unknown = null,
  usage: unknown = null,
  finish = "stop",
) {
  return {
    choices: [{ message: { content, tool_calls: toolCalls }, finish_reason: finish }],
    usage,
  };
}

describe("ChatCompletionsModel", () => {
  it("normalizes text and usage", async () => {
    const completions = fakeCompletions([
      chatResponse("hello model", null, { prompt_tokens: 42, completion_tokens: 7 }),
    ]);
    const client = { chat: { completions } } as unknown;
    const model = new ChatCompletionsModel({
      model: "test",
      apiKey: "sk-test",
      client,
    });
    const res = await model.generate(
      [{ role: "user", content: "hi" }],
      TOOL_DEFINITIONS as never,
    );
    expect(res.content).toBe("hello model");
    expect(res.usage).toEqual({ input_tokens: 42, output_tokens: 7 });
  });
  it("budgeted uses timeout ms and maxRetries 0 via withOptions", async () => {
    let withOpts: Record<string, unknown> | null = null;
    const completions = fakeCompletions([chatResponse("ok")]);
    const client: Record<string, unknown> = {
      chat: { completions },
      withOptions: (opts: Record<string, unknown>) => {
        withOpts = opts;
        return { chat: { completions } };
      },
    };
    const model = new ChatCompletionsModel({
      model: "test",
      apiKey: "sk",
      client: client as never,
    });
    model.setRemaining(12.5);
    await model.generate([{ role: "user", content: "hi" }], TOOL_DEFINITIONS as never);
    expect(withOpts).toEqual({ timeout: 12500, maxRetries: 0 });
  });
  it("budgeted without withOptions passes opts to create", async () => {
    const completions = fakeCompletions([chatResponse("ok")]);
    const client = { chat: { completions } } as unknown;
    const model = new ChatCompletionsModel({ model: "test", apiKey: "sk", client });
    model.setRemaining(2);
    await model.generate([{ role: "user", content: "hi" }], TOOL_DEFINITIONS as never);
    expect(
      (completions as unknown as { _calls: { opts: Record<string, unknown> }[] })
        ._calls[0]!.opts,
    ).toEqual({ timeout: 2000, maxRetries: 0 });
  });
  it("exhausted budget throws before request", async () => {
    const completions = fakeCompletions([chatResponse("ok")]);
    const client = { chat: { completions } } as unknown;
    const model = new ChatCompletionsModel({ model: "test", apiKey: "sk", client });
    model.setRemaining(0);
    await expect(
      model.generate([{ role: "user", content: "hi" }], TOOL_DEFINITIONS as never),
    ).rejects.toThrow(ProviderError);
    expect((completions as unknown as { _calls: unknown[] })._calls.length).toBe(0);
  });
  it("unbudgeted leaves defaults (no opts)", async () => {
    const completions = fakeCompletions([chatResponse("ok")]);
    const client = { chat: { completions } } as unknown;
    const model = new ChatCompletionsModel({ model: "test", apiKey: "sk", client });
    await model.generate([{ role: "user", content: "hi" }], TOOL_DEFINITIONS as never);
    expect(
      (completions as unknown as { _calls: { opts: unknown }[] })._calls[0]!.opts,
    ).toBeUndefined();
  });
  it("truncated length suppresses tool calls", async () => {
    const tc = [
      {
        id: "c1",
        type: "function",
        function: { name: "shell", arguments: '{"command":"hi"}' },
      },
    ];
    const completions = fakeCompletions([chatResponse("partial", tc, null, "length")]);
    const client = { chat: { completions } } as unknown;
    const model = new ChatCompletionsModel({ model: "test", apiKey: "sk", client });
    const res = await model.generate(
      [{ role: "user", content: "hi" }],
      TOOL_DEFINITIONS as never,
    );
    expect(res.truncated).toBe(true);
    expect(res.toolCalls.length).toBe(0);
  });
  it("malformed json throws ProviderError", async () => {
    const tc = [
      { id: "c1", type: "function", function: { name: "shell", arguments: "{bad" } },
    ];
    const completions = fakeCompletions([chatResponse(null, tc, null, "tool_calls")]);
    const client = { chat: { completions } } as unknown;
    const model = new ChatCompletionsModel({ model: "test", apiKey: "sk", client });
    await expect(
      model.generate([{ role: "user", content: "hi" }], TOOL_DEFINITIONS as never),
    ).rejects.toThrow(ProviderError);
  });
});

describe("ResponsesModel", () => {
  function msgItem(text: string) {
    return {
      type: "message",
      id: "msg_1",
      role: "assistant",
      status: "completed",
      content: [{ type: "output_text", text }],
    };
  }
  function fcItem(id: string, name: string, args: string) {
    return { type: "function_call", call_id: id, name, arguments: args };
  }
  function fakeResponses(script: unknown[]) {
    const calls: Record<string, unknown>[] = [];
    let idx = 0;
    return {
      create: async (params: unknown, opts?: unknown) => {
        calls.push({ params, opts });
        const item = script[idx++];
        if (item instanceof Error) throw item;
        return item;
      },
      _calls: calls,
    };
  }
  it("parses message and function_call", async () => {
    const resp = {
      status: "completed",
      output: [msgItem("hi"), fcItem("c1", "shell", '{"command":"ls"}')],
      usage: { input_tokens: 3, output_tokens: 5 },
    };
    const responses = fakeResponses([resp]);
    const client = { responses } as unknown;
    const model = new ResponsesModel({ model: "test", apiKey: "sk", client });
    const res = await model.generate(
      [{ role: "user", content: "hi" }],
      TOOL_DEFINITIONS as never,
    );
    expect(res.content).toBe("hi");
    expect(res.toolCalls[0]!.callId).toBe("c1");
    expect(res.usage).toEqual({ input_tokens: 3, output_tokens: 5 });
  });
  it("truncated max_output_tokens suppresses tool calls", async () => {
    const resp = {
      status: "incomplete",
      incomplete_details: { reason: "max_output_tokens" },
      output: [msgItem("partial"), fcItem("c1", "shell", '{"command":"hi"}')],
      usage: null,
    };
    const responses = fakeResponses([resp]);
    const client = { responses } as unknown;
    const model = new ResponsesModel({ model: "test", apiKey: "sk", client });
    const res = await model.generate(
      [{ role: "user", content: "hi" }],
      TOOL_DEFINITIONS as never,
    );
    expect(res.truncated).toBe(true);
    expect(res.toolCalls.length).toBe(0);
    expect(res.content).toBe("partial");
  });
  it("budgeted uses ms conversion", async () => {
    const resp = { status: "completed", output: [msgItem("ok")], usage: null };
    let withOpts: Record<string, unknown> | null = null;
    const responses = fakeResponses([resp]);
    const client: Record<string, unknown> = {
      responses,
      withOptions: (o: Record<string, unknown>) => {
        withOpts = o;
        return { responses };
      },
    };
    const model = new ResponsesModel({
      model: "test",
      apiKey: "sk",
      client: client as never,
    });
    model.setRemaining(5);
    await model.generate([{ role: "user", content: "hi" }], TOOL_DEFINITIONS as never);
    expect(withOpts).toEqual({ timeout: 5000, maxRetries: 0 });
  });
});

describe("AnthropicMessagesModel", () => {
  function fakeMessages(script: unknown[]) {
    const calls: Record<string, unknown>[] = [];
    let idx = 0;
    return {
      create: async (params: unknown, opts?: unknown) => {
        calls.push({ params, opts });
        const item = script[idx++];
        if (item instanceof Error) throw item;
        return item;
      },
      _calls: calls,
    };
  }
  it("parses text and tool_use", async () => {
    const resp = {
      stop_reason: "tool_use",
      content: [
        { type: "text", text: "hi " },
        { type: "tool_use", id: "c1", name: "shell", input: { command: "ls" } },
      ],
      usage: { input_tokens: 10, output_tokens: 5 },
    };
    const messages = fakeMessages([resp]);
    const client = { messages } as unknown;
    const model = new AnthropicMessagesModel({ model: "test", apiKey: "sk", client });
    const res = await model.generate(
      [{ role: "user", content: "hi" }],
      TOOL_DEFINITIONS as never,
    );
    expect(res.content).toBe("hi ");
    expect(res.toolCalls[0]!.callId).toBe("c1");
    expect(res.usage).toEqual({ input_tokens: 10, output_tokens: 5 });
  });
  it("truncated preserves text not tool calls", async () => {
    const resp = {
      stop_reason: "max_tokens",
      content: [
        { type: "text", text: "partial" },
        { type: "tool_use", id: "c1", name: "shell", input: { command: "ls" } },
      ],
      usage: null,
    };
    const messages = fakeMessages([resp]);
    const client = { messages } as unknown;
    const model = new AnthropicMessagesModel({ model: "test", apiKey: "sk", client });
    const res = await model.generate(
      [{ role: "user", content: "hi" }],
      TOOL_DEFINITIONS as never,
    );
    expect(res.truncated).toBe(true);
    expect(res.toolCalls.length).toBe(0);
    expect(res.content).toBe("partial");
  });
  it("budgeted uses ms", async () => {
    const resp = {
      stop_reason: "end_turn",
      content: [{ type: "text", text: "ok" }],
      usage: null,
    };
    let withOpts: Record<string, unknown> | null = null;
    const messages = fakeMessages([resp]);
    const client: Record<string, unknown> = {
      messages,
      withOptions: (o: Record<string, unknown>) => {
        withOpts = o;
        return { messages };
      },
    };
    const model = new AnthropicMessagesModel({
      model: "test",
      apiKey: "sk",
      client: client as never,
    });
    model.setRemaining(3);
    await model.generate([{ role: "user", content: "hi" }], TOOL_DEFINITIONS as never);
    expect(withOpts).toEqual({ timeout: 3000, maxRetries: 0 });
  });
});
