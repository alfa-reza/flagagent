import {
  closeSync,
  fsyncSync,
  openSync,
  readFileSync,
  renameSync,
  unlinkSync,
  writeSync,
} from "node:fs";
import { join } from "node:path";
import type { JsonObject, JsonValue } from "./model.js";
import { isRecord } from "./model.js";
import { readEvents } from "./artifacts.js";

function readJsonObject(path: string): JsonObject {
  const value: unknown = JSON.parse(readFileSync(path, "utf8"));
  if (!isRecord(value)) {
    throw new TypeError(`${path.split("/").at(-1) ?? path} must contain an object`);
  }
  return value as JsonObject;
}

function stringValue(value: unknown): string {
  if (typeof value === "string") return value;
  if (value === null) return "null";
  if (value === undefined) return "undefined";
  if (typeof value === "object") return "[object Object]";
  return String(value);
}

export function codeSpan(value: unknown): string {
  const text = stringValue(value).replace(/\r\n|\r|\n/g, " ");
  let longest = 0;
  let current = 0;
  for (const character of text) {
    if (character === "`") {
      current += 1;
      longest = Math.max(longest, current);
    } else {
      current = 0;
    }
  }
  const delimiter = "`".repeat(longest + 1);
  if (text.startsWith("`") || text.endsWith("`")) {
    return `${delimiter} ${text} ${delimiter}`;
  }
  if (text.startsWith(" ") && text.endsWith(" ") && text.trim() !== "") {
    return `${delimiter} ${text} ${delimiter}`;
  }
  return `${delimiter}${text}${delimiter}`;
}

function renderActions(events: JsonObject[]): string[] {
  const lines: string[] = [];
  for (const event of events) {
    const eventType = event.type;
    const payload = event.payload;
    if (typeof eventType !== "string" || !isRecord(payload)) {
      continue;
    }

    if (eventType === "tool_call") {
      const name = payload.name ?? "unknown";
      const callId = payload.call_id ?? "";
      const argumentsValue = payload.arguments;
      if (name === "shell" && isRecord(argumentsValue)) {
        const command = argumentsValue.command;
        if (typeof command === "string") {
          lines.push(`- \`shell\` call ${codeSpan(callId)}: ${codeSpan(command)}`);
          continue;
        }
      }
      lines.push(`- ${codeSpan(name)} call ${codeSpan(callId)}`);
    } else if (eventType === "flag_submission") {
      lines.push(`- \`submit_flag\` candidate: ${codeSpan(payload.candidate ?? "")}`);
    } else if (eventType === "verifier_result") {
      lines.push(`- verifier outcome: ${codeSpan(payload.outcome ?? "")}`);
    }
  }
  return lines.length > 0 ? lines : ["- no tool actions recorded"];
}

function valueAt(object: JsonObject, key: string): JsonValue | undefined {
  return object[key];
}

export function renderWriteup(
  run: JsonObject,
  events: JsonObject[],
  result: JsonObject,
): string {
  const challenge = isRecord(run.challenge) ? run.challenge : {};
  const model = isRecord(run.model) ? run.model : {};
  const prompt = isRecord(run.prompt) ? run.prompt : {};
  const lines = [
    "# FlagAgent Run",
    "",
    `- Run ID: ${codeSpan(valueAt(run, "run_id") ?? "")}`,
    `- Challenge: ${codeSpan(challenge.identity ?? "")}`,
    `- Status: ${codeSpan(result.status ?? "")}`,
    `- Reason: ${codeSpan(result.reason ?? "")}`,
    `- Model: ${codeSpan(model.name ?? "")}`,
    `- Protocol: ${codeSpan(model.protocol ?? "")}`,
    `- Prompt version: ${codeSpan(prompt.version ?? "")}`,
    `- Prompt SHA-256: ${codeSpan(prompt.sha256 ?? "")}`,
    "",
    "## Actions",
    "",
    ...renderActions(events),
    "",
    "## Metrics",
    "",
    `- Duration seconds: ${codeSpan(result.duration_seconds ?? "")}`,
    `- Model calls: ${codeSpan(result.model_calls ?? "")}`,
    `- Tool calls: ${codeSpan(result.tool_calls ?? "")}`,
    `- Flag submissions: ${codeSpan(result.flag_submissions ?? "")}`,
  ];
  if ("input_tokens" in result) {
    lines.push(`- Input tokens: ${codeSpan(result.input_tokens)}`);
  }
  if ("output_tokens" in result) {
    lines.push(`- Output tokens: ${codeSpan(result.output_tokens)}`);
  }
  lines.push("", "Structured artifacts remain authoritative.", "");
  return lines.join("\n");
}

export function writeWriteup(runDirectory: string): string {
  const directory = runDirectory;
  const run = readJsonObject(join(directory, "run.json"));
  const events = readEvents(join(directory, "events.jsonl"));
  const result = readJsonObject(join(directory, "result.json"));
  const destination = join(directory, "writeup.md");
  const temporary = join(directory, `.writeup.md.${process.pid}.${Date.now()}.tmp`);
  let descriptor: number | undefined;
  try {
    const openedDescriptor = openSync(temporary, "wx", 0o600);
    descriptor = openedDescriptor;
    const text = renderWriteup(run, events, result);
    writeFileDescriptor(openedDescriptor, text);
    fsyncSync(descriptor);
    closeSync(descriptor);
    descriptor = undefined;
    renameSync(temporary, destination);
  } catch (error) {
    if (descriptor !== undefined) {
      try {
        closeSync(descriptor);
      } catch {
        // Preserve the original failure.
      }
    }
    try {
      unlinkSync(temporary);
    } catch {
      // Cleanup is best effort.
    }
    throw error;
  }
  return destination;
}

function writeFileDescriptor(descriptor: number, text: string): void {
  const buffer = Buffer.from(text, "utf8");
  let offset = 0;
  while (offset < buffer.length) {
    offset += writeSync(descriptor, buffer, offset);
  }
}

export const _code_span = codeSpan;
export const write_writeup = writeWriteup;
