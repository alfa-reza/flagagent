import { randomBytes } from "node:crypto";
import {
  appendFileSync,
  closeSync,
  existsSync,
  fsyncSync,
  mkdirSync,
  openSync,
  readFileSync,
  renameSync,
  rmSync,
  unlinkSync,
  writeFileSync,
} from "node:fs";
import { join } from "node:path";
import type { JsonObject } from "./model.js";
import { isRecord, snapshotJson } from "./model.js";

export class EventStreamPoisoned extends Error {
  constructor(message = "event stream is poisoned") {
    super(message);
    this.name = "EventStreamPoisoned";
  }
}

const RUN_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;

export function validateRunId(runId: unknown): string {
  if (
    typeof runId !== "string" ||
    runId.includes("..") ||
    !RUN_ID_PATTERN.test(runId)
  ) {
    throw new Error(`invalid run id: ${String(runId)}`);
  }
  return runId;
}

function timestamp(value: Date): string {
  return value.toISOString();
}

function strictJsonObject(value: unknown, message: string): JsonObject {
  if (!isRecord(value)) {
    throw new TypeError(message);
  }
  return snapshotJson(value) as JsonObject;
}

function serialize(value: unknown): string {
  try {
    return JSON.stringify(snapshotJson(value));
  } catch (error) {
    if (error instanceof TypeError) {
      throw new TypeError("value must be strict JSON", { cause: error });
    }
    throw error;
  }
}

function atomicJson(path: string, value: unknown): void {
  const encoded = serialize(value);
  const temporary = `${path}.${randomBytes(8).toString("hex")}.tmp`;
  try {
    const descriptor = openSync(temporary, "wx", 0o600);
    try {
      writeFileSync(descriptor, encoded, { encoding: "utf8" });
      fsyncSync(descriptor);
    } finally {
      closeSync(descriptor);
    }
    renameSync(temporary, path);
  } catch (error) {
    try {
      unlinkSync(temporary);
    } catch {
      // Preserve the original failure. Cleanup is best effort.
    }
    throw error;
  }
}

export function readEvents(path: string): JsonObject[] {
  const content = readFileSync(path, "utf8");
  const lines = content.split(/\r?\n/);
  const hasTrailingNewline = content.endsWith("\n") || content.endsWith("\r");
  const events: JsonObject[] = [];

  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    if (line === "") {
      continue;
    }
    try {
      const event: unknown = JSON.parse(line);
      if (!isRecord(event)) {
        throw new TypeError("event must be a JSON object");
      }
      events.push(snapshotJson(event) as JsonObject);
    } catch (error) {
      const isFinalIncompleteLine = index === lines.length - 1 && !hasTrailingNewline;
      if (isFinalIncompleteLine && error instanceof SyntaxError) {
        break;
      }
      if (
        error instanceof TypeError &&
        error.message === "event must be a JSON object"
      ) {
        throw error;
      }
      throw new Error("events contain interior corruption", { cause: error });
    }
  }
  return events;
}

export type Now = () => Date;

export interface RunArtifactsCreateOptions {
  runId?: string;
  now?: Now;
}

export class RunArtifacts {
  readonly runId: string;
  readonly directory: string;
  private readonly now: Now;
  private readonly eventDescriptor: number;
  private sequence = 0;
  private poisoned = false;

  private constructor(
    runId: string,
    directory: string,
    now: Now,
    eventDescriptor: number,
  ) {
    this.runId = runId;
    this.directory = directory;
    this.now = now;
    this.eventDescriptor = eventDescriptor;
  }

  get runPath(): string {
    return join(this.directory, "run.json");
  }

  get eventsPath(): string {
    return join(this.directory, "events.jsonl");
  }

  get resultPath(): string {
    return join(this.directory, "result.json");
  }

  get workspace(): string {
    return join(this.directory, "workspace");
  }

  static generateRunId(
    now: Now = () => new Date(),
    tokenHex: (bytes: number) => string = (bytes) => randomBytes(bytes).toString("hex"),
  ): string {
    const date = now();
    const iso = date.toISOString();
    const stamp = iso
      .replace(/[-:]/g, "")
      .replace(/\.\d{3}Z$/, "Z")
      .replace(/T(\d{2})(\d{2})(\d{2})Z$/, "T$1$2$3Z");
    return `FA-${stamp}-${tokenHex(4)}`;
  }

  static create(
    root: string,
    metadata: Record<string, unknown>,
    options: RunArtifactsCreateOptions = {},
  ): RunArtifacts {
    const now = options.now ?? (() => new Date());
    const selectedId = options.runId ?? RunArtifacts.generateRunId(now);
    validateRunId(selectedId);
    const metadataObject = strictJsonObject(metadata, "metadata must be an object");
    if (metadataObject.run_id !== selectedId) {
      throw new Error("metadata run_id must match the selected run id");
    }

    mkdirSync(root, { recursive: true });
    const directory = join(root, selectedId);
    mkdirSync(directory);
    let eventDescriptor: number | undefined;
    try {
      mkdirSync(join(directory, "workspace"));
      atomicJson(join(directory, "run.json"), metadataObject);
      const descriptor = openSync(join(directory, "events.jsonl"), "a", 0o600);
      eventDescriptor = descriptor;
      return new RunArtifacts(selectedId, directory, now, descriptor);
    } catch (error) {
      if (eventDescriptor !== undefined) {
        try {
          closeSync(eventDescriptor);
        } catch {
          // Cleanup is best effort after creation failure.
        }
      }
      try {
        rmSync(directory, { recursive: true, force: true });
      } catch {
        // Preserve the original failure.
      }
      throw error;
    }
  }

  appendEvent(eventType: string, payload: Record<string, unknown>): JsonObject {
    if (this.poisoned) {
      throw new EventStreamPoisoned();
    }
    try {
      const event = {
        schema_version: 1,
        seq: this.sequence + 1,
        timestamp: timestamp(this.now()),
        type: eventType,
        payload: strictJsonObject(payload, "payload must be an object"),
      };
      const encoded = serialize(event);
      appendFileSync(this.eventDescriptor, `${encoded}\n`, { encoding: "utf8" });
      this.sequence += 1;
      return snapshotJson(event) as JsonObject;
    } catch (error) {
      this.poisoned = true;
      throw error;
    }
  }

  commitResult(result: Record<string, unknown>): void {
    if (existsSync(this.resultPath)) {
      throw new Error(`result already committed: ${this.resultPath}`);
    }
    atomicJson(this.resultPath, strictJsonObject(result, "result must be an object"));
  }

  close(): void {
    closeSync(this.eventDescriptor);
  }
}

export const validate_run_id = validateRunId;
export const read_events = readEvents;
