import { readFileSync, statSync } from "node:fs";
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

function loadChallenge(challengeDir: string): {
  identity: string;
  description: string;
  expectedFlag: string;
  networkMode: string;
  targetContext?: string;
  sourceDir?: string;
} {
  const stat = statSync(challengeDir);
  if (!stat.isDirectory()) throw new Error("challenge must be a directory");
  const raw = readFileSync(join(challengeDir, "challenge.json"), "utf8");
  const payload = JSON.parse(raw);
  const filesDir = join(challengeDir, "files");
  let sourceDir: string | undefined;
  try {
    if (statSync(filesDir).isDirectory()) sourceDir = filesDir;
  } catch {
    /* none */
  }
  return {
    identity: payload.identity,
    description: payload.description,
    expectedFlag: payload.expected_flag,
    networkMode: payload.network_mode ?? "none",
    targetContext: payload.target_context,
    sourceDir,
  };
}

export async function runCli(argv: string[]): Promise<void> {
  const get = (flag: string) => {
    const idx = argv.indexOf(flag);
    return idx >= 0 ? argv[idx + 1] : undefined;
  };
  const challenge = get("--challenge");
  const protocol = get("--protocol");
  const model = get("--model");
  const apiBase = get("--api-base");
  const runsRoot = get("--runs-root") ?? "runs";
  if (!challenge || !protocol || !model) {
    console.error(
      "usage: flagagent run --challenge DIR --protocol openai-chat|openai-responses|anthropic --model MODEL",
    );
    process.exit(2);
  }
  const ch = loadChallenge(challenge);
  const apiKey =
    process.env[protocol === "anthropic" ? "ANTHROPIC_API_KEY" : "OPENAI_API_KEY"] ??
    "";
  if (!apiKey) {
    console.error("missing API key");
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
  else
    modelInst = new AnthropicMessagesModel({
      model,
      apiKey,
      baseURL: apiBase,
    }) as unknown as import("./model.js").Model;
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
    limits: new Limits(),
    runsRoot,
    monotonic: () => Date.now() / 1000,
    utcNow: () => new Date(),
    systemPrompt: SOLVER_PROMPT,
    promptVersion: SOLVER_PROMPT_VERSION,
    promptSha256: SOLVER_PROMPT_SHA256,
    modelIdentity: model,
    protocol,
    apiBase: apiBase ?? null,
  });
  const result = await loop.run();
  console.log(JSON.stringify(result, null, 2));
}

if (import.meta.url === `file://${process.argv[1]}`) {
  await runCli(process.argv.slice(2));
}
