export const MODEL_TOOL_OUTPUT_BYTES = 16 * 1024;
export const LOGGED_TOOL_OUTPUT_BYTES = 64 * 1024;
export const TRUNCATION_MARKER = "[truncated]";

export interface LimitsOptions {
  maxModelTurns?: number;
  wallTimeoutSeconds?: number;
  commandTimeoutSeconds?: number;
  maxModelToolOutputBytes?: number;
  maxLoggedToolOutputBytes?: number;
  maxSourceFileBytes?: number;
  maxSourceTotalBytes?: number;
  maxSourceFiles?: number;
  maxSourceEntries?: number;
  maxSourceDepth?: number;
}

export const DEFAULT_LIMITS = {
  maxModelTurns: 100,
  wallTimeoutSeconds: 1800,
  commandTimeoutSeconds: 60,
  maxModelToolOutputBytes: MODEL_TOOL_OUTPUT_BYTES,
  maxLoggedToolOutputBytes: LOGGED_TOOL_OUTPUT_BYTES,
  maxSourceFileBytes: 10 * 1024 * 1024,
  maxSourceTotalBytes: 50 * 1024 * 1024,
  maxSourceFiles: 1024,
  maxSourceEntries: 2048,
  maxSourceDepth: 16,
} as const;

function positiveInteger(value: number): boolean {
  return Number.isInteger(value) && value > 0;
}

function positiveFiniteNumber(value: number): boolean {
  return Number.isFinite(value) && value > 0;
}

export class Limits {
  readonly maxModelTurns: number;
  readonly wallTimeoutSeconds: number;
  readonly commandTimeoutSeconds: number;
  readonly maxModelToolOutputBytes: number;
  readonly maxLoggedToolOutputBytes: number;
  readonly maxSourceFileBytes: number;
  readonly maxSourceTotalBytes: number;
  readonly maxSourceFiles: number;
  readonly maxSourceEntries: number;
  readonly maxSourceDepth: number;

  constructor(options: LimitsOptions = {}) {
    const values = {
      ...DEFAULT_LIMITS,
      ...options,
    };

    const integerValues = [
      values.maxModelTurns,
      values.maxModelToolOutputBytes,
      values.maxLoggedToolOutputBytes,
      values.maxSourceFileBytes,
      values.maxSourceTotalBytes,
      values.maxSourceFiles,
      values.maxSourceEntries,
      values.maxSourceDepth,
    ];
    if (!integerValues.every(positiveInteger)) {
      throw new Error("integer limits must be positive integers");
    }

    const timeoutValues = [values.wallTimeoutSeconds, values.commandTimeoutSeconds];
    if (!timeoutValues.every(positiveFiniteNumber)) {
      throw new Error("timeouts must be positive finite numbers");
    }
    if (values.maxLoggedToolOutputBytes < values.maxModelToolOutputBytes) {
      throw new Error("logged output limit must be at least model output limit");
    }

    this.maxModelTurns = values.maxModelTurns;
    this.wallTimeoutSeconds = values.wallTimeoutSeconds;
    this.commandTimeoutSeconds = values.commandTimeoutSeconds;
    this.maxModelToolOutputBytes = values.maxModelToolOutputBytes;
    this.maxLoggedToolOutputBytes = values.maxLoggedToolOutputBytes;
    this.maxSourceFileBytes = values.maxSourceFileBytes;
    this.maxSourceTotalBytes = values.maxSourceTotalBytes;
    this.maxSourceFiles = values.maxSourceFiles;
    this.maxSourceEntries = values.maxSourceEntries;
    this.maxSourceDepth = values.maxSourceDepth;
  }

  toObject(): Record<string, number> {
    return {
      max_model_turns: this.maxModelTurns,
      wall_timeout_seconds: this.wallTimeoutSeconds,
      command_timeout_seconds: this.commandTimeoutSeconds,
      max_model_tool_output_bytes: this.maxModelToolOutputBytes,
      max_logged_tool_output_bytes: this.maxLoggedToolOutputBytes,
      max_source_file_bytes: this.maxSourceFileBytes,
      max_source_total_bytes: this.maxSourceTotalBytes,
      max_source_files: this.maxSourceFiles,
      max_source_entries: this.maxSourceEntries,
      max_source_depth: this.maxSourceDepth,
    };
  }

  toDict(): Record<string, number> {
    return this.toObject();
  }
}
