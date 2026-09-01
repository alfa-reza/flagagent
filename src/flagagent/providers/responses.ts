import OpenAI from "openai";
import { ModelResponse, ToolCall } from "../model.js";
import {
  ProviderError,
  ProviderBudgetTimeoutError,
  clientForBudget,
  usageField,
  toJson,
} from "./chat.js";

function toResponsesTool(tool: Record<string, unknown>): Record<string, unknown> {
  return {
    type: "function",
    name: tool.name,
    description: tool.description,
    parameters: tool.parameters,
    strict: true,
  };
}

function normalizeResponsesUsage(usage: unknown): Record<string, number> | null {
  if (usage == null) return null;
  const input = usageField(usage, "input_tokens");
  const output = usageField(usage, "output_tokens");
  const result: Record<string, number> = {};
  if (input != null) result.input_tokens = input;
  if (output != null) result.output_tokens = output;
  return Object.keys(result).length > 0 ? result : null;
}

function outputItemToDict(item: unknown): Record<string, unknown> {
  if (item != null && typeof item === "object" && !Array.isArray(item)) {
    const rec = item as Record<string, unknown>;
    // SDK may return plain objects already
    if (rec.type != null) return { ...(rec as Record<string, unknown>) };
  }
  if (item != null && typeof item === "object") {
    const rec = item as Record<string, unknown>;
    const modelDump = rec.model_dump as (() => Record<string, unknown>) | undefined;
    if (typeof modelDump === "function") return modelDump.call(item);
    // fallback: vars
    return { ...(rec as Record<string, unknown>) };
  }
  throw new ProviderError("unsupported responses output item");
}

function extractMessageText(itemDict: Record<string, unknown>): string {
  if (itemDict.role !== "assistant") throw new ProviderError("message role is invalid");
  const messageId = itemDict.id as string;
  if (typeof messageId !== "string" || messageId.length === 0)
    throw new ProviderError("message id is missing");
  const status = itemDict.status as string;
  if (status !== "in_progress" && status !== "completed" && status !== "incomplete")
    throw new ProviderError("message status is invalid");
  const content = itemDict.content as unknown;
  if (!Array.isArray(content))
    throw new ProviderError("message content must be a list");
  const parts: string[] = [];
  for (const part of content) {
    if (part == null || typeof part !== "object" || Array.isArray(part))
      throw new ProviderError("message content item is invalid");
    const rec = part as Record<string, unknown>;
    const partType = rec.type as string;
    let text: unknown;
    if (partType === "output_text") text = rec.text;
    else if (partType === "refusal") text = rec.refusal;
    else throw new ProviderError("message content item type is invalid");
    if (typeof text !== "string")
      throw new ProviderError("message content text is invalid");
    parts.push(text);
  }
  return parts.join("");
}

function validateReasoningItem(itemDict: Record<string, unknown>): void {
  const itemId = itemDict.id as string;
  if (typeof itemId !== "string" || itemId.length === 0)
    throw new ProviderError("reasoning id is missing");
  const summary = itemDict.summary as unknown;
  if (!Array.isArray(summary)) throw new ProviderError("reasoning summary is invalid");
  for (const part of summary) {
    if (part == null || typeof part !== "object" || Array.isArray(part))
      throw new ProviderError("reasoning summary item is invalid");
    const rec = part as Record<string, unknown>;
    if (rec.type !== "summary_text" || typeof rec.text !== "string")
      throw new ProviderError("reasoning summary item is invalid");
  }
  const enc = itemDict.encrypted_content;
  if (enc != null && typeof enc !== "string")
    throw new ProviderError("reasoning encrypted content is invalid");
}

function parseFunctionCall(itemDict: Record<string, unknown>): ToolCall {
  const callId = itemDict.call_id as string;
  if (typeof callId !== "string" || callId.length === 0)
    throw new ProviderError("function call missing call_id");
  const name = itemDict.name as string;
  if (typeof name !== "string" || name.length === 0)
    throw new ProviderError("function call missing name");
  const argStr = itemDict.arguments as string;
  if (typeof argStr !== "string" || argStr.trim().length === 0)
    throw new ProviderError("function call arguments missing");
  let args: unknown;
  try {
    args = JSON.parse(argStr);
  } catch (e) {
    throw new ProviderError("function call arguments are not valid JSON", {
      cause: e as Error,
    });
  }
  if (typeof args !== "object" || args == null || Array.isArray(args))
    throw new ProviderError("function call arguments must be a JSON object");
  try {
    return new ToolCall(callId, name, args);
  } catch (e) {
    throw new ProviderError("function call arguments are not strict JSON", {
      cause: e as Error,
    });
  }
}

function parseResponsesOutput(
  output: unknown,
  opts: { truncated?: boolean } = {},
): { content: string; toolCalls: ToolCall[]; replayItems: Record<string, unknown>[] } {
  if (!Array.isArray(output) || output.length === 0) {
    throw new ProviderError("responses output has no items");
  }
  const contentParts: string[] = [];
  const toolCalls: ToolCall[] = [];
  const replayItems: Record<string, unknown>[] = [];
  for (const item of output) {
    const dict = outputItemToDict(item);
    const t = dict.type as string;
    if (t === "message") {
      contentParts.push(extractMessageText(dict));
      replayItems.push(dict);
    } else if (t === "function_call") {
      if (opts.truncated) continue;
      toolCalls.push(parseFunctionCall(dict));
      replayItems.push(dict);
    } else if (t === "reasoning") {
      validateReasoningItem(dict);
      replayItems.push(dict);
    } else {
      throw new ProviderError("unsupported responses output item type");
    }
  }
  return { content: contentParts.join(""), toolCalls, replayItems };
}

export interface ResponsesOptions {
  model: string;
  apiKey: string;
  baseURL?: string | null;
  client?: unknown;
}

export class ResponsesModel {
  readonly model: string;
  readonly apiKey: string;
  readonly baseURL: string | null | undefined;
  readonly client: unknown;
  private remainingBudget: number | undefined;
  private monotonic: (() => number) | undefined;
  private _lastCommittedAt: number | undefined;
  readonly _clientInjected: boolean;
  private builtInput: Record<string, unknown>[] = [];
  private processedCount = 0;

  get lastCommittedAt(): number | undefined {
    return this._lastCommittedAt;
  }

  setMonotonic(monotonic: () => number): void {
    this.monotonic = monotonic;
  }

  private captureCommittedAt(): void {
    this._lastCommittedAt = this.monotonic?.() ?? Date.now() / 1000;
  }

  constructor(options: ResponsesOptions) {
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
    this._lastCommittedAt = undefined;
    try {
      if (this.remainingBudget != null && this.remainingBudget <= 0) {
        throw new ProviderError("responses request budget exhausted");
      }

      const instructions = messages.find((m) => m.role === "system")?.content as
        string | undefined;

      const newMessages = messages.slice(this.processedCount);
      for (const message of newMessages) {
        const role = message.role as string;
        if (role === "system") continue;
        if (role === "user") {
          this.builtInput.push({
            role: "user",
            content: (message.content as string) ?? "",
          });
        } else if (role === "assistant") {
          // assistant content is replayed via output items, not input
        } else if (role === "tool") {
          this.builtInput.push({
            type: "function_call_output",
            call_id: message.call_id,
            output: toJson(message.result),
          });
        } else {
          throw new ProviderError("unsupported message role");
        }
      }
      this.processedCount = messages.length;

      const params: Record<string, unknown> = {
        model: this.model,
        input: [...this.builtInput],
        tools: tools.map((t) => toResponsesTool(t)),
        store: false,
        include: ["reasoning.encrypted_content"],
      };
      if (instructions != null) params.instructions = instructions;

      let client: unknown = this.client;
      let requestOptions: Record<string, unknown> | undefined;
      if (this.remainingBudget != null) {
        const signal = (this as unknown as { _signal?: AbortSignal })._signal;
        const res = clientForBudget(this.client, this.remainingBudget, signal);
        client = res.client;
        requestOptions = res.options;
      }

      const c = client as Record<string, unknown>;
      const responses = c.responses as Record<string, unknown>;
      const create = responses.create as (p: unknown, o?: unknown) => Promise<unknown>;
      const response = await create.call(responses, params, requestOptions);

      let parsed: ModelResponse;
      try {
        const r = response as Record<string, unknown>;
        const status = r.status as string | null;
        let truncated = false;
        if (status === "incomplete") {
          const details = r.incomplete_details as Record<string, unknown> | null;
          const reason = details?.reason as string | undefined;
          // SDK may expose incomplete_details as object with reason, or via attribute
          const altReason = (r as Record<string, unknown>).incomplete_details as
            Record<string, unknown> | undefined;
          const finalReason = reason ?? (altReason?.reason as string | undefined);
          if (finalReason === "max_output_tokens") truncated = true;
          else throw new ProviderError(`responses ${status}`);
        } else if (status != null && status !== "completed") {
          throw new ProviderError(`responses ${status}`);
        }
        const output = r.output as unknown;
        const { content, toolCalls, replayItems } = parseResponsesOutput(output, {
          truncated,
        });
        const usage = normalizeResponsesUsage((r as Record<string, unknown>).usage);
        this.builtInput.push(...replayItems);
        parsed = new ModelResponse(content, toolCalls, usage, truncated);
      } catch (e) {
        if (e instanceof ProviderError) throw e;
        throw new ProviderError("malformed responses output", { cause: e as Error });
      }
      this.captureCommittedAt();
      return parsed;
    } catch (e) {
      this.captureCommittedAt();
      if (e instanceof ProviderError) throw e;
      const signal = (this as unknown as { _signal?: AbortSignal })._signal;
      const causeStr =
        String((e as Error)?.message ?? "") +
        " " +
        String((e as Record<string, unknown>)?.cause ?? "");
      const isAbortLike =
        (e as Error)?.name === "AbortError" ||
        /abort|cancel|timed?\s*out|timeout/i.test(causeStr);
      if ((isAbortLike || signal?.aborted) && this.remainingBudget != null) {
        throw new ProviderBudgetTimeoutError("provider request budget timeout", {
          cause: e as Error,
        });
      }
      throw new ProviderError("responses request failed", { cause: e as Error });
    }
  }
}
