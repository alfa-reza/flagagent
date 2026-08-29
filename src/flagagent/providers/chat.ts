import OpenAI from "openai";
import { ModelResponse, ToolCall } from "../model.js";

export class ProviderError extends Error {
  constructor(message: string, options?: ErrorOptions) {
    super(message, options);
    this.name = "ProviderError";
  }
}

function toJson(value: unknown): string {
  return JSON.stringify(value);
}

function usageField(usage: unknown, name: string): number | undefined {
  if (usage == null || typeof usage !== "object") return undefined;
  const rec = usage as Record<string, unknown>;
  const v = rec[name];
  if (typeof v === "number" && Number.isInteger(v)) return v;
  return undefined;
}

function toChatTool(tool: Record<string, unknown>): Record<string, unknown> {
  return {
    type: "function",
    function: {
      name: tool.name,
      description: tool.description,
      parameters: tool.parameters,
    },
  };
}

function toChatToolCall(call: Record<string, unknown>): Record<string, unknown> {
  return {
    id: call.call_id,
    type: "function",
    function: {
      name: call.name,
      arguments: toJson(call.arguments),
    },
  };
}

function toChatMessage(message: Record<string, unknown>): Record<string, unknown> {
  const role = message.role as string;
  if (role === "system" || role === "user") {
    return { role, content: (message.content as string) ?? "" };
  }
  if (role === "assistant") {
    const toolCalls = (message.tool_calls as unknown[]) ?? [];
    const chatMessage: Record<string, unknown> = {
      role: "assistant",
      content: (message.content as string) || null,
    };
    if (Array.isArray(toolCalls) && toolCalls.length > 0) {
      chatMessage.tool_calls = toolCalls.map((c) =>
        toChatToolCall(c as Record<string, unknown>),
      );
    }
    return chatMessage;
  }
  if (role === "tool") {
    return {
      role: "tool",
      tool_call_id: message.call_id,
      content: toJson(message.result),
    };
  }
  throw new ProviderError("unsupported message role");
}

function parseChatResponse(response: unknown): ModelResponse {
  const r = response as Record<string, unknown>;
  const choices = r.choices as unknown[];
  if (!Array.isArray(choices) || choices.length === 0) {
    throw new ProviderError("chat completions response has no choices");
  }
  const choice = choices[0] as Record<string, unknown>;
  const finishReason = choice.finish_reason as string | null;
  const truncated = finishReason === "length";
  if (!truncated && finishReason !== "stop" && finishReason !== "tool_calls") {
    throw new ProviderError("chat completions finish reason is not normal");
  }
  const message = choice.message as Record<string, unknown> | null;
  if (message == null)
    throw new ProviderError("chat completions response missing message");
  const rawContent = message.content;
  const content = typeof rawContent === "string" ? rawContent : "";
  const rawToolCalls = message.tool_calls as unknown;
  let toolCalls: ToolCall[] = [];
  if (truncated) {
    toolCalls = [];
  } else {
    const list = rawToolCalls == null ? [] : rawToolCalls;
    if (!Array.isArray(list)) throw new ProviderError("tool calls must be a list");
    for (const raw of list) {
      const rec = raw as Record<string, unknown>;
      const callId = rec.id as string;
      if (typeof callId !== "string" || callId.length === 0)
        throw new ProviderError("tool call missing id");
      const fn = rec.function as Record<string, unknown> | null;
      if (fn == null) throw new ProviderError("tool call missing function");
      const name = fn.name as string;
      if (typeof name !== "string" || name.length === 0)
        throw new ProviderError("tool call missing function name");
      const argStr = fn.arguments as string;
      if (typeof argStr !== "string" || argStr.trim().length === 0)
        throw new ProviderError("tool call arguments missing");
      let args: unknown;
      try {
        args = JSON.parse(argStr);
      } catch (e) {
        throw new ProviderError("tool call arguments are not valid JSON", {
          cause: e as Error,
        });
      }
      if (typeof args !== "object" || args == null || Array.isArray(args)) {
        throw new ProviderError("tool call arguments must be a JSON object");
      }
      try {
        toolCalls.push(new ToolCall(callId, name, args));
      } catch (e) {
        throw new ProviderError("tool call arguments are not strict JSON", {
          cause: e as Error,
        });
      }
    }
  }
  const usageRaw = r.usage as Record<string, unknown> | null | undefined;
  let usage: Record<string, number> | null = null;
  if (usageRaw != null && typeof usageRaw === "object") {
    const prompt =
      typeof usageRaw.prompt_tokens === "number" ? usageRaw.prompt_tokens : undefined;
    const completion =
      typeof usageRaw.completion_tokens === "number"
        ? usageRaw.completion_tokens
        : undefined;
    const out: Record<string, number> = {};
    if (typeof prompt === "number" && Number.isInteger(prompt))
      out.input_tokens = prompt;
    if (typeof completion === "number" && Number.isInteger(completion))
      out.output_tokens = completion;
    usage = Object.keys(out).length > 0 ? out : null;
  }
  return new ModelResponse(content, toolCalls, usage, truncated);
}

function clientForBudget(
  client: unknown,
  budgetSeconds: number,
  signal?: AbortSignal,
): { client: unknown; options?: Record<string, unknown> } {
  const ms = Math.ceil(budgetSeconds * 1000);
  // timeout/maxRetries are client options (via withOptions), signal is per-request
  const clientOpts: Record<string, unknown> = { timeout: ms, maxRetries: 0 };
  const withOptions = (client as Record<string, unknown>).withOptions as
    ((opts: Record<string, unknown>) => unknown) | undefined;
  if (typeof withOptions === "function") {
    const newClient = withOptions.call(client, clientOpts);
    if (signal) return { client: newClient, options: { signal } };
    return { client: newClient };
  }
  if (signal) return { client, options: { timeout: ms, maxRetries: 0, signal } };
  return { client, options: { timeout: ms, maxRetries: 0 } };
}

export interface ChatCompletionsOptions {
  model: string;
  apiKey: string;
  baseURL?: string | null;
  client?: unknown;
}

export class ChatCompletionsModel {
  readonly model: string;
  readonly apiKey: string;
  readonly baseURL: string | null | undefined;
  readonly client: unknown;
  private remainingBudget: number | undefined;
  readonly _clientInjected: boolean;

  constructor(options: ChatCompletionsOptions) {
    this.model = options.model;
    this.apiKey = options.apiKey;
    this.baseURL = options.baseURL ?? null;
    this._clientInjected = options.client != null;
    if (options.client != null) {
      this.client = options.client;
    } else {
      this.client = new OpenAI({
        apiKey: this.apiKey,
        baseURL: this.baseURL ?? undefined,
      });
    }
  }

  setRemaining(remaining: number): void {
    if (typeof remaining === "boolean" || typeof remaining !== "number") {
      throw new TypeError("remaining budget must be a number");
    }
    if (!Number.isFinite(remaining))
      throw new TypeError("remaining budget must be a finite number");
    this.remainingBudget = remaining;
  }

  set_remaining(remaining: number): void {
    this.setRemaining(remaining);
  }

  async generate(
    messages: Record<string, unknown>[],
    tools: Record<string, unknown>[],
  ): Promise<ModelResponse> {
    const requestMessages = messages.map((m) => toChatMessage(m));
    const requestTools = tools.map((t) => toChatTool(t));
    const params: Record<string, unknown> = {
      model: this.model,
      messages: requestMessages,
      tools: requestTools,
    };

    let client: unknown = this.client;
    let requestOptions: Record<string, unknown> | undefined;

    if (this.remainingBudget != null) {
      if (this.remainingBudget <= 0)
        throw new ProviderError("chat completions request budget exhausted");
      const signal = (this as unknown as { _signal?: AbortSignal })._signal;
      const res = clientForBudget(this.client, this.remainingBudget, signal);
      client = res.client;
      requestOptions = res.options;
    }

    try {
      const c = client as Record<string, unknown>;
      const chat = c.chat as Record<string, unknown>;
      const completions = chat.completions as Record<string, unknown>;
      const create = completions.create as (
        params: unknown,
        opts?: unknown,
      ) => Promise<unknown>;
      const response = await create.call(completions, params, requestOptions);
      try {
        return parseChatResponse(response);
      } catch (e) {
        if (e instanceof ProviderError) throw e;
        throw new ProviderError("malformed chat completions response", {
          cause: e as Error,
        });
      }
    } catch (e) {
      if (e instanceof ProviderError) throw e;
      throw new ProviderError("chat completions request failed", { cause: e as Error });
    }
  }
}

export {
  clientForBudget,
  usageField,
  toJson,
  toChatTool,
  toChatMessage,
  parseChatResponse,
};
