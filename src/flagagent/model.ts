import { isDeepStrictEqual } from "node:util";

export type JsonPrimitive = string | number | boolean | null;
export type JsonValue = JsonPrimitive | JsonObject | JsonValue[];
export type JsonObject = { [key: string]: JsonValue };

export function isRecord(value: unknown): value is Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    return false;
  }

  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

/**
 * Make a detached copy while enforcing the JSON value subset used by the
 * normalized contracts and persisted artifacts.
 */
export function snapshotJson(value: unknown): JsonValue {
  if (value === null) {
    return null;
  }

  if (typeof value === "string" || typeof value === "boolean") {
    return value;
  }

  if (typeof value === "number") {
    if (!Number.isFinite(value)) {
      throw new TypeError("value must be strict JSON");
    }
    return value;
  }

  if (Array.isArray(value)) {
    return value.map((item) => snapshotJson(item));
  }

  if (isRecord(value)) {
    const copy: JsonObject = Object.create(null) as JsonObject;
    for (const [key, item] of Object.entries(value)) {
      Object.defineProperty(copy, key, {
        value: snapshotJson(item),
        writable: true,
        enumerable: true,
        configurable: true,
      });
    }
    return copy;
  }

  throw new TypeError("value must be strict JSON");
}

function snapshotObject(value: unknown, message: string): JsonObject {
  if (!isRecord(value)) {
    throw new TypeError(message);
  }
  return snapshotJson(value) as JsonObject;
}

export interface ToolCallObject {
  call_id: string;
  name: string;
  arguments: JsonObject;
}

export class ToolCall {
  readonly callId: string;
  readonly name: string;
  readonly arguments: JsonObject;

  constructor(callId: string, name: string, argumentsValue: unknown) {
    if (typeof callId !== "string" || callId.length === 0) {
      throw new Error("callId must be a non-empty string");
    }
    if (typeof name !== "string" || name.length === 0) {
      throw new Error("name must be a non-empty string");
    }

    this.callId = callId;
    this.name = name;
    this.arguments = snapshotObject(argumentsValue, "arguments must be an object");
  }

  toObject(): ToolCallObject {
    return {
      call_id: this.callId,
      name: this.name,
      arguments: snapshotJson(this.arguments) as JsonObject,
    };
  }

  toDict(): ToolCallObject {
    return this.toObject();
  }

  to_dict(): ToolCallObject {
    return this.toObject();
  }
}

export interface ModelResponseObject {
  content: string;
  tool_calls: ToolCallObject[];
  usage: JsonValue | null;
  truncated: boolean;
}

export class ModelResponse {
  readonly content: string;
  readonly toolCalls: readonly ToolCall[];
  readonly usage: JsonValue | null;
  readonly truncated: boolean;

  constructor(
    content = "",
    toolCalls: readonly ToolCall[] = [],
    usage: unknown = null,
    truncated = false,
  ) {
    if (typeof content !== "string") {
      throw new TypeError("content must be a string");
    }
    if (!Array.isArray(toolCalls)) {
      throw new TypeError("toolCalls must be a sequence");
    }
    if (!toolCalls.every((call) => call instanceof ToolCall)) {
      throw new TypeError("toolCalls must contain ToolCall values");
    }
    if (typeof truncated !== "boolean") {
      throw new TypeError("truncated must be a bool");
    }

    this.content = content;
    this.toolCalls = toolCalls.map(
      (call) => new ToolCall(call.callId, call.name, call.arguments),
    );
    this.usage = usage === null ? null : snapshotJson(usage);
    this.truncated = truncated;
  }

  toObject(): ModelResponseObject {
    return {
      content: this.content,
      tool_calls: this.toolCalls.map((call) => call.toObject()),
      usage: this.usage == null ? null : (snapshotJson(this.usage) as JsonValue | null),
      truncated: this.truncated,
    };
  }

  toDict(): ModelResponseObject {
    return this.toObject();
  }

  to_dict(): ModelResponseObject {
    return this.toObject();
  }
}

export type ModelMessage = Record<string, unknown>;
export type ModelTool = Record<string, unknown>;

export interface Model {
  generate(messages: ModelMessage[], tools: ModelTool[]): Promise<ModelResponse>;
}

export type ScriptItem = ModelResponse | Error;

export class ScriptedModel implements Model {
  readonly script: readonly ScriptItem[];
  readonly calls: Array<[JsonValue[], JsonValue[]]> = [];
  private index = 0;

  constructor(script: readonly ScriptItem[]) {
    if (!Array.isArray(script)) {
      throw new TypeError("script must be a sequence");
    }
    if (
      !script.every((item) => item instanceof ModelResponse || item instanceof Error)
    ) {
      throw new TypeError("script entry must be a ModelResponse or Error");
    }
    this.script = [...script];
  }

  async generate(messages: ModelMessage[], tools: ModelTool[]): Promise<ModelResponse> {
    const messageSnapshot = snapshotJson(messages);
    const toolSnapshot = snapshotJson(tools);
    if (!Array.isArray(messageSnapshot) || !Array.isArray(toolSnapshot)) {
      throw new TypeError("messages and tools must be arrays");
    }
    this.calls.push([messageSnapshot, toolSnapshot]);

    if (this.index >= this.script.length) {
      throw new Error("scripted model exhausted");
    }

    const item = this.script[this.index];
    this.index += 1;
    if (item instanceof Error) {
      throw item;
    }

    return new ModelResponse(item.content, item.toolCalls, item.usage, item.truncated);
  }
}

export function jsonValuesEqual(left: unknown, right: unknown): boolean {
  return isDeepStrictEqual(left, right);
}
