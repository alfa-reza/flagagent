import Anthropic from "@anthropic-ai/sdk";
import { ModelResponse, ToolCall } from "../model.js";
import { ProviderError, clientForBudget, usageField, toJson } from "./chat.js";

export const ANTHROPIC_MAX_TOKENS = 4096;

function toAnthropicTool(tool: Record<string, unknown>): Record<string, unknown> {
  return {
    name: tool.name,
    description: tool.description,
    input_schema: tool.parameters,
  };
}

function serializeToolResult(result: unknown): string {
  try {
    return toJson(result);
  } catch {
    return String(result);
  }
}

function toAnthropicMessages(
  messages: Record<string, unknown>[],
  thinkingHistory: Record<string, unknown>[][] | null = null,
): Record<string, unknown>[] {
  const result: Record<string, unknown>[] = [];
  let index = 0;
  let assistantIndex = 0;
  while (index < messages.length) {
    const message = messages[index] as Record<string, unknown>;
    const role = message.role as string;
    if (role === "user") {
      result.push({ role: "user", content: (message.content as string) ?? "" });
      index += 1;
    } else if (role === "assistant") {
      const blocks: Record<string, unknown>[] = [];
      if (thinkingHistory != null && assistantIndex < thinkingHistory.length) {
        for (const tb of thinkingHistory[assistantIndex]!) blocks.push({ ...tb });
      }
      const content = (message.content as string) || "";
      if (content) blocks.push({ type: "text", text: content });
      for (const call of (message.tool_calls as Record<string, unknown>[]) ?? []) {
        blocks.push({
          type: "tool_use",
          id: call.call_id,
          name: call.name,
          input: call.arguments,
        });
      }
      if (blocks.length > 0) result.push({ role: "assistant", content: blocks });
      assistantIndex += 1;
      index += 1;
    } else if (role === "tool") {
      const toolResults: Record<string, unknown>[] = [];
      while (
        index < messages.length &&
        (messages[index] as Record<string, unknown>).role === "tool"
      ) {
        const tm = messages[index] as Record<string, unknown>;
        toolResults.push({
          type: "tool_result",
          tool_use_id: tm.call_id,
          content: serializeToolResult(tm.result),
        });
        index += 1;
      }
      result.push({ role: "user", content: toolResults });
    } else {
      throw new ProviderError("unsupported message role");
    }
  }
  return result;
}

function normalizeAnthropicUsage(usage: unknown): Record<string, number> | null {
  if (usage == null) return null;
  const input = usageField(usage, "input_tokens");
  const output = usageField(usage, "output_tokens");
  const result: Record<string, number> = {};
  if (input != null) result.input_tokens = input;
  if (output != null) result.output_tokens = output;
  return Object.keys(result).length > 0 ? result : null;
}

function parseAnthropicResponse(response: unknown): {
  modelResponse: ModelResponse;
  thinkingBlocks: Record<string, unknown>[];
} {
  const r = response as Record<string, unknown>;
  const stopReason = r.stop_reason as string;
  const truncated =
    stopReason === "max_tokens" || stopReason === "model_context_window_exceeded";
  if (!truncated && stopReason !== "end_turn" && stopReason !== "tool_use") {
    throw new ProviderError("messages response has non-normal stop reason");
  }
  const contentList = r.content as unknown[];
  if (!Array.isArray(contentList) || contentList.length === 0) {
    throw new ProviderError("messages response has no content");
  }
  const textParts: string[] = [];
  const toolCalls: ToolCall[] = [];
  const thinkingBlocks: Record<string, unknown>[] = [];
  let seenToolUse = false;
  for (const block of contentList) {
    const b = block as Record<string, unknown>;
    const blockType = b.type as string;
    if (blockType === "text") {
      const text = b.text as string;
      if (typeof text !== "string") throw new ProviderError("text block missing text");
      if (seenToolUse)
        throw new ProviderError("text block after tool use is not supported");
      textParts.push(text);
    } else if (blockType === "thinking") {
      const thinking = b.thinking as string;
      const signature = b.signature as string;
      if (typeof thinking !== "string" || typeof signature !== "string")
        throw new ProviderError("thinking block missing required fields");
      if (seenToolUse)
        throw new ProviderError("thinking block after tool use is not supported");
      thinkingBlocks.push({ type: "thinking", thinking, signature });
    } else if (blockType === "redacted_thinking") {
      const data = b.data as string;
      if (typeof data !== "string")
        throw new ProviderError("redacted thinking block missing data");
      if (seenToolUse)
        throw new ProviderError(
          "redacted thinking block after tool use is not supported",
        );
      thinkingBlocks.push({ type: "redacted_thinking", data });
    } else if (blockType === "tool_use") {
      if (truncated) continue;
      const callId = b.id as string;
      if (typeof callId !== "string" || callId.length === 0)
        throw new ProviderError("tool use missing id");
      const name = b.name as string;
      if (typeof name !== "string" || name.length === 0)
        throw new ProviderError("tool use missing name");
      const inputVal = b.input as unknown;
      if (inputVal == null || typeof inputVal !== "object" || Array.isArray(inputVal))
        throw new ProviderError("tool use input must be a JSON object");
      try {
        toolCalls.push(new ToolCall(callId, name, inputVal));
      } catch (e) {
        throw new ProviderError("tool use arguments are not strict JSON", {
          cause: e as Error,
        });
      }
      seenToolUse = true;
    } else {
      throw new ProviderError("unsupported content block type");
    }
  }
  if (!truncated) {
    if (stopReason === "tool_use" && toolCalls.length === 0)
      throw new ProviderError("tool_use stop reason without client tool_use block");
    if (stopReason === "end_turn" && toolCalls.length > 0)
      throw new ProviderError("end_turn stop reason with client tool_use block");
  }
  const content = textParts.join("");
  const usage = normalizeAnthropicUsage((r as Record<string, unknown>).usage);
  return {
    modelResponse: new ModelResponse(content, toolCalls, usage, truncated),
    thinkingBlocks,
  };
}

export interface AnthropicOptions {
  model: string;
  apiKey: string;
  baseURL?: string | null;
  client?: unknown;
}

export class AnthropicMessagesModel {
  readonly model: string;
  readonly apiKey: string;
  readonly baseURL: string | null | undefined;
  readonly client: unknown;
  private remainingBudget: number | undefined;
  private monotonic: (() => number) | undefined;
  private _lastCommittedAt: number | undefined;
  readonly _clientInjected: boolean;
  private thinkingHistory: Record<string, unknown>[][] = [];

  get lastCommittedAt(): number | undefined {
    return this._lastCommittedAt;
  }

  setMonotonic(monotonic: () => number): void {
    this.monotonic = monotonic;
  }

  private captureCommittedAt(): void {
    this._lastCommittedAt = this.monotonic?.() ?? Date.now() / 1000;
  }

  constructor(options: AnthropicOptions) {
    this.model = options.model;
    this.apiKey = options.apiKey;
    this.baseURL = options.baseURL ?? null;
    this._clientInjected = options.client != null;
    if (options.client != null) {
      this.client = options.client;
    } else {
      this.client = new Anthropic({
        apiKey: this.apiKey,
        baseURL: this.baseURL ?? undefined,
      });
    }
  }

  setRemaining(remaining: number): void {
    if (typeof remaining === "boolean" || typeof remaining !== "number")
      throw new TypeError("remaining budget must be a number");
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
    const systemPrompt = messages.find((m) => m.role === "system")?.content as
      string | undefined;
    const requestMessages = toAnthropicMessages(
      messages.filter((m) => m.role !== "system"),
      this.thinkingHistory,
    );
    const requestTools = tools.map((t) => toAnthropicTool(t));
    const params: Record<string, unknown> = {
      model: this.model,
      max_tokens: ANTHROPIC_MAX_TOKENS,
      messages: requestMessages,
      tools: requestTools,
    };
    if (systemPrompt != null) params.system = systemPrompt;

    let client: unknown = this.client;
    let requestOptions: Record<string, unknown> | undefined;
    if (this.remainingBudget != null) {
      if (this.remainingBudget <= 0)
        throw new ProviderError("messages request budget exhausted");
      const signal = (this as unknown as { _signal?: AbortSignal })._signal;
      const res = clientForBudget(this.client, this.remainingBudget, signal);
      client = res.client;
      requestOptions = res.options;
    }

    try {
      const c = client as Record<string, unknown>;
      const msgs = c.messages as Record<string, unknown>;
      const create = msgs.create as (p: unknown, o?: unknown) => Promise<unknown>;
      const response = await create.call(msgs, params, requestOptions);
      let modelResponse: ModelResponse;
      try {
        const parsed = parseAnthropicResponse(response);
        this.thinkingHistory.push(parsed.thinkingBlocks);
        modelResponse = parsed.modelResponse;
      } catch (e) {
        if (e instanceof ProviderError) throw e;
        throw new ProviderError("malformed messages response", { cause: e as Error });
      }
      this.captureCommittedAt();
      return modelResponse;
    } catch (e) {
      this.captureCommittedAt();
      if (e instanceof ProviderError) throw e;
      throw new ProviderError("messages request failed", { cause: e as Error });
    }
  }
}
