import {
  LOGGED_TOOL_OUTPUT_BYTES,
  MODEL_TOOL_OUTPUT_BYTES,
  TRUNCATION_MARKER,
} from "./limits.js";
import type { JsonObject } from "./model.js";
import { isRecord } from "./model.js";

export { LOGGED_TOOL_OUTPUT_BYTES, MODEL_TOOL_OUTPUT_BYTES, TRUNCATION_MARKER };

export interface ToolParameters {
  type: "object";
  properties: Record<string, { type: "string" }>;
  required: readonly string[];
  additionalProperties: false;
}

export interface ToolDefinition {
  name: string;
  description: string;
  parameters: ToolParameters;
}

export const TOOL_DEFINITIONS: readonly ToolDefinition[] = [
  {
    name: "shell",
    description: "Run one non-interactive shell command.",
    parameters: {
      type: "object",
      properties: { command: { type: "string" } },
      required: ["command"],
      additionalProperties: false,
    },
  },
  {
    name: "submit_flag",
    description: "Submit one candidate flag for authoritative verification.",
    parameters: {
      type: "object",
      properties: { candidate: { type: "string" } },
      required: ["candidate"],
      additionalProperties: false,
    },
  },
];

export class UnknownToolError extends Error {
  constructor(name: string) {
    super(name);
    this.name = "UnknownToolError";
  }
}

export function validateToolArguments(
  name: string,
  argumentsValue: unknown,
): Record<string, string> {
  if (!isRecord(argumentsValue)) {
    throw new TypeError("arguments must be an object");
  }

  const required =
    name === "shell" ? "command" : name === "submit_flag" ? "candidate" : null;
  if (required === null) {
    throw new UnknownToolError(name);
  }

  if (
    Object.keys(argumentsValue).length !== 1 ||
    !Object.hasOwn(argumentsValue, required)
  ) {
    throw new Error("arguments must contain exactly the required field");
  }

  const value = (argumentsValue as Record<string, unknown>)[required];
  if (typeof value !== "string" || value.trim().length === 0) {
    throw new Error(`${required} must be a non-empty string`);
  }
  return { [required]: value };
}

export interface ShellResultObject {
  stdout: string;
  stderr: string;
  exit_code: number | null;
  timed_out: boolean;
  truncated: boolean;
}

export class ShellResult {
  readonly stdout: string;
  readonly stderr: string;
  readonly exitCode: number | null;
  readonly timedOut: boolean;
  readonly truncated: boolean;

  constructor(
    stdout: string,
    stderr: string,
    exitCode: number | null,
    timedOut: boolean,
    truncated = false,
  ) {
    if (typeof stdout !== "string" || typeof stderr !== "string") {
      throw new TypeError("stdout and stderr must be strings");
    }
    if (typeof timedOut !== "boolean" || typeof truncated !== "boolean") {
      throw new TypeError("timedOut and truncated must be booleans");
    }
    if (timedOut) {
      if (exitCode !== null) {
        throw new Error("timed out results require a null exit code");
      }
    } else if (
      typeof exitCode !== "number" ||
      !Number.isInteger(exitCode) ||
      !Number.isFinite(exitCode)
    ) {
      throw new TypeError("completed results require an integer exit code");
    }

    this.stdout = stdout;
    this.stderr = stderr;
    this.exitCode = exitCode;
    this.timedOut = timedOut;
    this.truncated = truncated;
  }

  toObject(): ShellResultObject {
    return {
      stdout: this.stdout,
      stderr: this.stderr,
      exit_code: this.exitCode,
      timed_out: this.timedOut,
      truncated: this.truncated,
    };
  }

  toDict(): ShellResultObject {
    return this.toObject();
  }
}

function utf8Width(byte: number): number {
  if (byte <= 0x7f) return 1;
  if (byte >= 0xc2 && byte <= 0xdf) return 2;
  if (byte >= 0xe0 && byte <= 0xef) return 3;
  if (byte >= 0xf0 && byte <= 0xf4) return 4;
  return 0;
}

function isContinuation(byte: number): boolean {
  return byte >= 0x80 && byte <= 0xbf;
}

function validCodePointAt(data: Buffer, start: number): number {
  const width = utf8Width(data[start] ?? 0);
  if (width === 0 || start + width > data.length) return 0;
  for (let index = start + 1; index < start + width; index += 1) {
    if (!isContinuation(data[index] ?? 0)) return 0;
  }
  return width;
}

function pyPrefix(data: Buffer, limit: number): string {
  return data
    .subarray(0, limit)
    .toString("utf8")
    .replace(/\uFFFD/g, "");
}

function utf8Prefix(data: Buffer, limit: number): string {
  return pyPrefix(data, limit);
}

function utf8Suffix(data: Buffer, limit: number): string {
  let start = Math.max(0, data.length - limit);
  while (start < data.length && validCodePointAt(data, start) === 0) {
    start += 1;
  }
  return data.subarray(start).toString("utf8");
}

export function truncateUtf8(value: string, limit: number): [string, boolean] {
  if (typeof value !== "string") {
    throw new TypeError("value must be a string");
  }
  if (!Number.isInteger(limit) || limit <= 0) {
    throw new Error("output limit must be a positive integer");
  }

  const data = Buffer.from(value, "utf8");
  if (data.length <= limit) {
    return [value, false];
  }

  const marker = Buffer.from(TRUNCATION_MARKER, "utf8");
  if (limit <= marker.length) {
    return [utf8Prefix(marker, limit), true];
  }

  const available = limit - marker.length;
  let headBytes = Math.floor((available + 1) / 2);
  const tailBytes = Math.floor(available / 2);
  let rendered =
    utf8Prefix(data, headBytes) + TRUNCATION_MARKER + utf8Suffix(data, tailBytes);
  // Shrink head until the UTF-8 byte length fits the limit, mirroring Python's
  // loop that compensates for multi-byte boundary adjustments.
  while (Buffer.byteLength(rendered, "utf8") > limit && headBytes > 0) {
    headBytes -= 1;
    rendered =
      utf8Prefix(data, headBytes) + TRUNCATION_MARKER + utf8Suffix(data, tailBytes);
  }
  return [rendered, true];
}

function normalizeView(result: ShellResult, limit: number): ShellResult {
  const [stdout, stdoutTruncated] = truncateUtf8(result.stdout, limit);
  const [stderr, stderrTruncated] = truncateUtf8(result.stderr, limit);
  return new ShellResult(
    stdout,
    stderr,
    result.exitCode,
    result.timedOut,
    result.truncated || stdoutTruncated || stderrTruncated,
  );
}

export function normalizeShellResult(
  result: ShellResult,
  modelLimit = MODEL_TOOL_OUTPUT_BYTES,
  loggedLimit = LOGGED_TOOL_OUTPUT_BYTES,
): [ShellResult, ShellResult] {
  if (loggedLimit < modelLimit) {
    throw new Error("logged output limit must be at least the model output limit");
  }
  return [normalizeView(result, modelLimit), normalizeView(result, loggedLimit)];
}

export class SandboxError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "SandboxError";
  }
}

export interface Executor {
  execute(command: string, timeoutSeconds: number): ShellResult;
}

export type VerifierOutcome = "correct" | "incorrect";

export interface Verifier {
  check(candidate: string): VerifierOutcome;
}

export class ExactStringVerifier implements Verifier {
  readonly expected: string;

  constructor(expected: string) {
    if (typeof expected !== "string") {
      throw new TypeError("expected must be a string");
    }
    this.expected = expected;
  }

  check(candidate: string): VerifierOutcome {
    if (typeof candidate !== "string") {
      throw new TypeError("candidate must be a string");
    }
    return candidate.trim() === this.expected ? "correct" : "incorrect";
  }
}

export type ToolResult = JsonObject;
