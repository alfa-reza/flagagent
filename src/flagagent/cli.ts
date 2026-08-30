#!/usr/bin/env node
import { lstatSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { AgentLoop } from "./loop.js";
import { Limits } from "./limits.js";
import { ExactStringVerifier } from "./tools.js";
import { DockerExecutor } from "./docker.js";
import { ChatCompletionsModel } from "./providers/chat.js";
import { ResponsesModel } from "./providers/responses.js";
import { AnthropicMessagesModel } from "./providers/anthropic.js";
import {
  SOLVER_PROMPT,
  SOLVER_PROMPT_SHA256,
  SOLVER_PROMPT_VERSION,
} from "./prompt.js";
import { writeWriteup } from "./writeup.js";

export const MAX_CHALLENGE_DESCRIPTOR_BYTES = 64 * 1024;
const PROTOCOL_NAMES = ["openai-chat", "openai-responses", "anthropic"] as const;
type Protocol = (typeof PROTOCOL_NAMES)[number];

function isIdentifier(value: string): boolean {
  return /^[\p{ID_Start}_][\p{ID_Continue}_]*$/u.test(value);
}

function requireString(payload: Record<string, unknown>, key: string): string {
  const value = payload[key];
  if (typeof value !== "string" || value.trim().length === 0) {
    throw new Error(`challenge descriptor requires non-empty ${key}`);
  }
  return value;
}

export function loadChallenge(challengeDir: string): {
  identity: string;
  description: string;
  expectedFlag: string;
  networkMode: "none" | "local";
  targetContext?: string;
  sourceDir?: string;
} {
  const challengeStat = lstatSync(challengeDir);
  if (challengeStat.isSymbolicLink() || !challengeStat.isDirectory()) {
    throw new Error("challenge must be a directory");
  }

  const descriptorPath = join(challengeDir, "challenge.json");
  const descriptorStat = lstatSync(descriptorPath);
  if (descriptorStat.isSymbolicLink() || !descriptorStat.isFile()) {
    throw new Error("challenge.json must be a regular file");
  }
  if (descriptorStat.size > MAX_CHALLENGE_DESCRIPTOR_BYTES) {
    throw new Error(
      `challenge.json exceeds maximum size of ${MAX_CHALLENGE_DESCRIPTOR_BYTES} bytes`,
    );
  }

  const data = readFileSync(descriptorPath);
  if (data.byteLength > MAX_CHALLENGE_DESCRIPTOR_BYTES) {
    throw new Error(
      `challenge.json exceeds maximum size of ${MAX_CHALLENGE_DESCRIPTOR_BYTES} bytes`,
    );
  }
  let payloadValue: unknown;
  try {
    payloadValue = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(data));
  } catch (error) {
    throw new Error("challenge.json must be valid JSON", { cause: error });
  }
  if (
    payloadValue === null ||
    typeof payloadValue !== "object" ||
    Array.isArray(payloadValue)
  ) {
    throw new TypeError("challenge.json must contain an object");
  }
  const payload = payloadValue as Record<string, unknown>;
  const allowed = new Set([
    "identity",
    "description",
    "expected_flag",
    "network_mode",
    "target_context",
  ]);
  if (Object.keys(payload).some((key) => !allowed.has(key))) {
    throw new Error("challenge.json contains unsupported fields");
  }

  const identity = requireString(payload, "identity");
  const description = payload.description;
  if (typeof description !== "string") {
    throw new TypeError("challenge descriptor requires string description");
  }
  const expectedFlag = requireString(payload, "expected_flag");
  const networkMode = payload.network_mode;
  if (networkMode !== "none" && networkMode !== "local") {
    throw new Error("challenge network_mode must be none or local");
  }
  const targetContext = payload.target_context;
  if (targetContext !== undefined && targetContext !== null) {
    if (typeof targetContext !== "string") {
      throw new TypeError("challenge target_context must be a string");
    }
  }

  const filesPath = join(challengeDir, "files");
  let sourceDir: string | undefined;
  try {
    const filesStat = lstatSync(filesPath);
    if (filesStat.isSymbolicLink() || !filesStat.isDirectory()) {
      throw new Error("challenge files must be a directory");
    }
    sourceDir = filesPath;
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
  }

  return {
    identity,
    description,
    expectedFlag,
    networkMode,
    targetContext: targetContext ?? undefined,
    sourceDir,
  };
}

export async function runCli(argv: string[]): Promise<void> {
  if (argv.length === 0 || argv[0] !== "run") {
    console.error(
      "usage: flagagent run --challenge DIR --protocol openai-chat|openai-responses|anthropic --model MODEL",
    );
    process.exit(2);
  }
  const args = argv.slice(1);
  const getArg = (flag: string) => {
    const idx = args.indexOf(flag);
    return idx >= 0 ? args[idx + 1] : undefined;
  };
  const challenge = getArg("--challenge");
  const protocolValue = getArg("--protocol");
  const model = getArg("--model");
  const apiBase = getArg("--api-base");
  const apiKeyEnv = getArg("--api-key-env");
  const runsRoot = getArg("--runs-root") ?? "runs";
  const maxModelTurnsRaw = getArg("--max-model-turns");
  const wallTimeoutRaw = getArg("--wall-timeout-seconds");
  const commandTimeoutRaw = getArg("--command-timeout-seconds");
  if (!challenge || !protocolValue || !model) {
    console.error(
      "usage: flagagent run --challenge DIR --protocol openai-chat|openai-responses|anthropic --model MODEL",
    );
    process.exit(2);
  }
  if (!PROTOCOL_NAMES.includes(protocolValue as Protocol)) {
    throw new Error(`unsupported protocol: ${protocolValue}`);
  }
  const protocol = protocolValue as Protocol;
  const ch = loadChallenge(challenge);
  const defaultKeyEnv =
    protocol === "anthropic" ? "ANTHROPIC_API_KEY" : "OPENAI_API_KEY";
  const keyEnv = apiKeyEnv ?? defaultKeyEnv;
  if (!isIdentifier(keyEnv)) {
    throw new Error("api-key-env must be a valid environment variable name");
  }
  const apiKey = process.env[keyEnv] ?? "";
  if (!apiKey) {
    console.error(`missing API key environment variable: ${keyEnv}`);
    process.exit(2);
  }
  let modelInst: import("./model.js").Model;
  if (protocol === "openai-chat")
    modelInst = new ChatCompletionsModel({
      model,
      apiKey,
      baseURL: apiBase,
    }) as unknown as import("./model.js").Model;
  else if (protocol === "openai-responses")
    modelInst = new ResponsesModel({
      model,
      apiKey,
      baseURL: apiBase,
    }) as unknown as import("./model.js").Model;
  else if (protocol === "anthropic")
    modelInst = new AnthropicMessagesModel({
      model,
      apiKey,
      baseURL: apiBase,
    }) as unknown as import("./model.js").Model;
  else throw new Error(`unsupported protocol: ${protocol}`);
  const limitOverrides: Record<string, number> = {};
  if (maxModelTurnsRaw != null) {
    const v = Number(maxModelTurnsRaw);
    if (!Number.isInteger(v) || v <= 0)
      throw new Error("--max-model-turns must be a positive integer");
    limitOverrides.maxModelTurns = v;
  }
  if (wallTimeoutRaw != null) {
    const v = Number(wallTimeoutRaw);
    if (!Number.isFinite(v) || v <= 0)
      throw new Error("--wall-timeout-seconds must be a positive number");
    limitOverrides.wallTimeoutSeconds = v;
  }
  if (commandTimeoutRaw != null) {
    const v = Number(commandTimeoutRaw);
    if (!Number.isFinite(v) || v <= 0)
      throw new Error("--command-timeout-seconds must be a positive number");
    limitOverrides.commandTimeoutSeconds = v;
  }
  const executor = new DockerExecutor({ networkMode: ch.networkMode });
  const loop = new AgentLoop({
    model: modelInst,
    executor: executor as unknown as import("./tools.js").Executor,
    verifier: new ExactStringVerifier(ch.expectedFlag),
    challenge: {
      identity: ch.identity,
      description: ch.description,
      sourceDir: ch.sourceDir,
      targetContext: ch.targetContext,
      networkMode: ch.networkMode,
    },
    limits: new Limits(limitOverrides),
    runsRoot,
    monotonic: () => Number(process.hrtime.bigint()) / 1_000_000_000,
    utcNow: () => new Date(),
    systemPrompt: SOLVER_PROMPT,
    promptVersion: SOLVER_PROMPT_VERSION,
    promptSha256: SOLVER_PROMPT_SHA256,
    modelIdentity: model,
    protocol,
    apiBase: apiBase ?? null,
  });
  const result = await loop.run();
  const dir = (loop as unknown as { artifacts: { directory: string } }).artifacts
    .directory;
  console.log(`run=${dir}`);
  console.log(String(result["status:reason"]));
  let exitCode = 0;
  if (result.status === "error") exitCode = 1;
  try {
    writeWriteup(dir);
  } catch (e) {
    console.error(`writeup failed: ${(e as Error).message}`);
    if (exitCode === 0) exitCode = 1;
  }
  if (exitCode !== 0) process.exit(exitCode);
}

{
  const doRun = async (): Promise<void> => {
    try {
      const { realpathSync, existsSync } = await import("node:fs");
      const { fileURLToPath } = await import("node:url");
      const selfPath = fileURLToPath(import.meta.url);
      const entry = process.argv[1];
      if (!entry) return;
      let shouldRun = false;
      if (existsSync(entry)) {
        try {
          shouldRun = realpathSync(entry) === realpathSync(selfPath);
        } catch {
          shouldRun = entry === selfPath;
        }
      } else {
        shouldRun = selfPath.endsWith(entry.slice(-32));
      }
      if (shouldRun) await runCli(process.argv.slice(2));
    } catch {
      // ignore
    }
  };
  await doRun();
}
