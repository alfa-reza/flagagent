import { createHash } from "node:crypto";
import { RunArtifacts } from "./artifacts.js";
import { Limits } from "./limits.js";
import { Model, ModelResponse } from "./model.js";
import {
  Executor,
  SandboxError,
  Verifier,
  TOOL_DEFINITIONS,
  normalizeShellResult,
  validateToolArguments,
  UnknownToolError,
} from "./tools.js";
import { FLAGAGENT_VERSION, CONCEPT_VERSION } from "./version.js";
import {
  InvalidChallengeSourceError,
  snapshotSourceFiles,
  stageSourceFiles,
} from "./staging.js";

export interface ChallengeInput {
  identity: string;
  description: string;
  sourceDir?: string | null;
  targetContext?: string | null;
  networkMode?: string;
}

export interface AgentLoopOptions {
  model: Model;
  executor: Executor & {
    prepare?: (workspace: string, runId: string) => void | Promise<void>;
    cleanup?: (runId: string) => void | Promise<void>;
    setRemaining?: (remaining: number) => void;
    setExecutionDeadline?: (deadline: number, monotonic: () => number) => void;
    sandboxProvenance?: () =>
      Record<string, unknown> | Promise<Record<string, unknown>>;
    sandboxProvenanceAsync?: () =>
      Record<string, unknown> | Promise<Record<string, unknown>>;
    sandboxLifecycle?: () => Record<string, unknown>;
  };
  verifier?: Verifier | null;
  challenge: ChallengeInput;
  limits: Limits;
  runsRoot: string;
  monotonic: () => number;
  utcNow: () => Date;
  runId?: string | null;
  systemPrompt?: string | null;
  promptVersion?: string | null;
  promptSha256?: string | null;
  modelIdentity?: string | null;
  protocol?: string | null;
  apiBase?: string | null;
}

function sanitizeApiBase(value: string | null | undefined): string | null {
  if (value == null) return null;
  if (typeof value !== "string" || value.length === 0)
    throw new Error("api_base must be a non-empty string");
  const url = new URL(value);
  if (url.username || url.password || url.search || url.hash) {
    throw new Error("api_base must not contain credentials or query data");
  }
  return `${url.protocol}//${url.host}${url.pathname}`;
}

function utcTimestamp(value: Date): string {
  return value.toISOString().replace("+00:00", "Z");
}

export class AgentLoop {
  private model: Model;
  private executor: AgentLoopOptions["executor"];
  private verifier: Verifier | null;
  private challenge: ChallengeInput;
  private limits: Limits;
  private runsRoot: string;
  private monotonic: () => number;
  private utcNow: () => Date;
  private runIdOpt: string | null | undefined;
  private systemPrompt: string | null | undefined;
  private promptVersion: string | null | undefined;
  private promptSha256: string | null | undefined;
  private modelIdentity: string | null | undefined;
  private protocol: string | null | undefined;
  private apiBase: string | null | undefined;

  private messages: Record<string, unknown>[] = [];
  private artifacts!: RunArtifacts;
  private deadline!: number;
  private started!: number;
  private modelCalls = 0;
  private toolCalls = 0;
  private flagSubmissions = 0;
  private seenCallIds = new Set<string>();
  private inputTokens: number | undefined;
  private outputTokens: number | undefined;

  // Provider supervision: for M1, use cooperative AbortSignal for real providers,
  // and direct await for ScriptedModel/fakes. The key invariants are enforced
  // by deadline checks + deadline-gated commit, not by transport mechanism.
  private abortController: AbortController | null = null;

  constructor(options: AgentLoopOptions) {
    this.model = options.model;
    this.executor = options.executor;
    this.verifier = options.verifier ?? null;
    this.challenge = options.challenge;
    this.limits = options.limits;
    this.runsRoot = options.runsRoot;
    this.monotonic = options.monotonic;
    this.utcNow = options.utcNow;
    this.runIdOpt = options.runId;
    this.systemPrompt = options.systemPrompt;
    this.promptVersion = options.promptVersion;
    this.promptSha256 = options.promptSha256;
    this.modelIdentity = options.modelIdentity;
    this.protocol = options.protocol;
    this.apiBase = options.apiBase;

    if (this.systemPrompt == null) {
      if (this.promptVersion != null || this.promptSha256 != null) {
        throw new Error("prompt metadata requires a system prompt");
      }
    } else {
      if (typeof this.systemPrompt !== "string" || this.systemPrompt.length === 0) {
        throw new Error("system_prompt must be a non-empty string");
      }
      if (typeof this.promptVersion !== "string" || this.promptVersion.length === 0) {
        throw new Error("prompt_version is required with a system prompt");
      }
      if (typeof this.promptSha256 !== "string" || this.promptSha256.length === 0) {
        throw new Error("prompt_sha256 is required with a system prompt");
      }
      const expected = createHash("sha256")
        .update(this.systemPrompt, "utf8")
        .digest("hex");
      if (this.promptSha256 !== expected)
        throw new Error("prompt_sha256 does not match system_prompt");
    }
  }

  private remaining(): number {
    return Math.max(0, this.deadline - this.monotonic());
  }

  private expired(): boolean {
    return this.monotonic() >= this.deadline;
  }

  private error(
    reason: string,
    operation: string,
    callId?: string,
    detail?: string,
  ): { status: string; reason: string; unprocessed: string[] } {
    const payload: Record<string, unknown> = { reason, operation };
    if (callId != null) payload.call_id = callId;
    if (detail != null) payload.detail = detail;
    this.artifacts.appendEvent("error", payload as Record<string, unknown>);
    return { status: "error", reason, unprocessed: [] };
  }

  private toolResult(
    callId: string,
    name: string,
    result: Record<string, unknown>,
    executed: boolean,
    loggedResult?: Record<string, unknown>,
  ): void {
    const payload: Record<string, unknown> = {
      call_id: callId,
      name,
      executed,
      result,
    };
    if (loggedResult != null) payload.logged_result = loggedResult;
    this.artifacts.appendEvent("tool_result", payload as Record<string, unknown>);
    this.messages.push({ role: "tool", call_id: callId, name, result });
  }

  private buildResult(status: string, reason: string): Record<string, unknown> {
    const result: Record<string, unknown> = {
      schema_version: 1,
      run_id: this.artifacts.runId,
      status,
      reason,
      "status:reason": `${status}:${reason}`,
      finished_at: utcTimestamp(this.utcNow()),
      duration_seconds: Math.max(0, this.monotonic() - this.started),
      model_calls: this.modelCalls,
      tool_calls: this.toolCalls,
      flag_submissions: this.flagSubmissions,
    };
    if (this.inputTokens != null) result.input_tokens = this.inputTokens;
    if (this.outputTokens != null) result.output_tokens = this.outputTokens;
    return result;
  }

  private terminal(
    status: string,
    reason: string,
    unprocessed: string[],
    candidateFlag?: string,
  ): Record<string, unknown> {
    this.artifacts.appendEvent("terminal_decision", {
      status,
      reason,
      committed: false,
      unprocessed_call_ids: unprocessed,
    } as Record<string, unknown>);
    const result = this.buildResult(status, reason);
    if (candidateFlag != null) result.candidate_flag = candidateFlag;
    this.artifacts.commitResult(result as Record<string, unknown>);
    return result;
  }

  private isProviderModel(): boolean {
    const name =
      (this.model as unknown as Record<string, unknown>)._clientInjected !== undefined
        ? (this.model.constructor.name as string)
        : this.model.constructor.name;
    const providerNames = [
      "ChatCompletionsModel",
      "ResponsesModel",
      "AnthropicMessagesModel",
    ];
    if (!providerNames.includes(name)) return false;
    const injected = (this.model as unknown as Record<string, unknown>)
      ._clientInjected as boolean | undefined;
    if (injected) return false;
    return true;
  }

  async run(): Promise<Record<string, unknown>> {
    const apiBase = sanitizeApiBase(this.apiBase ?? null);
    const selectedId = this.runIdOpt ?? RunArtifacts.generateRunId(() => this.utcNow());

    let preSnapshot: {
      files: Array<[string, string]>;
      digest: string | null;
      tmpDir: string | null;
      cleanup: () => void;
    } | null = null;
    let preSnapshotError: unknown = null;
    try {
      preSnapshot = snapshotSourceFiles(this.challenge.sourceDir ?? null, this.limits);
    } catch (e) {
      preSnapshotError = e;
    }

    const challengeMeta: Record<string, unknown> = {
      identity: this.challenge.identity,
      description: this.challenge.description,
    };
    if (this.challenge.targetContext != null)
      challengeMeta.target_context = this.challenge.targetContext;
    if (preSnapshot?.digest != null) challengeMeta.source_sha256 = preSnapshot.digest;
    const metadata: Record<string, unknown> = {
      schema_version: 1,
      run_id: selectedId,
      flagagent_version: FLAGAGENT_VERSION,
      concept_version: CONCEPT_VERSION,
      challenge: challengeMeta,
      started_at: utcTimestamp(this.utcNow()),
      limits: this.limits.toObject(),
    };
    if (this.systemPrompt != null) {
      metadata.prompt = { version: this.promptVersion, sha256: this.promptSha256 };
    }
    if (this.modelIdentity != null || this.protocol != null || apiBase != null) {
      metadata.model = {
        name: this.modelIdentity,
        protocol: this.protocol,
        base_url: apiBase,
      };
    }
    const trySync = (this.executor as unknown as Record<string, unknown>)
      .sandboxProvenance as
      (() => Record<string, unknown> | Promise<Record<string, unknown>>) | undefined;
    const tryAsync = (this.executor as unknown as Record<string, unknown>)
      .sandboxProvenanceAsync as
      (() => Record<string, unknown> | Promise<Record<string, unknown>>) | undefined;
    let syncProvenance: Record<string, unknown> | null = null;
    let maybeAsync: Promise<Record<string, unknown>> | null = null;
    if (typeof trySync === "function") {
      try {
        const maybe = trySync.call(this.executor) as unknown;
        if (maybe != null && typeof (maybe as Promise<unknown>).then === "function") {
          maybeAsync = maybe as Promise<Record<string, unknown>>;
        } else if (maybe != null) {
          syncProvenance = maybe as Record<string, unknown>;
        }
      } catch {
        // ignore sync failure
      }
    }
    if (syncProvenance) {
      metadata.sandbox = syncProvenance;
    }
    let asyncProvenancePromise: Promise<Record<string, unknown>> | null = null;
    if (typeof tryAsync === "function") {
      try {
        const p = tryAsync.call(this.executor) as unknown;
        if (p != null && typeof (p as Promise<unknown>).then === "function") {
          asyncProvenancePromise = p as Promise<Record<string, unknown>>;
        } else if (p != null) {
          metadata.sandbox = p as Record<string, unknown>;
        }
      } catch {
        // ignore
      }
    } else if (maybeAsync) {
      asyncProvenancePromise = maybeAsync;
    }
    let bestEffortSandbox: Record<string, unknown> | null = null;
    if (asyncProvenancePromise) {
      const raced = await Promise.race([
        asyncProvenancePromise.then(
          (v) => ({ ok: true as const, v }),
          () => ({ ok: false as const }),
        ),
        new Promise<{ ok: false }>((resolve) =>
          setTimeout(() => resolve({ ok: false }), 2500),
        ),
      ]);
      if (raced.ok) bestEffortSandbox = raced.v;
    }
    if (bestEffortSandbox) metadata.sandbox = bestEffortSandbox;
    try {
      this.artifacts = RunArtifacts.create(
        this.runsRoot,
        metadata as Record<string, unknown>,
        {
          runId: selectedId,
          now: () => this.utcNow(),
        },
      );
    } catch (e) {
      try {
        preSnapshot?.cleanup();
      } catch {
        // ignore
      }
      throw e;
    }

    // Build messages
    let userContent = this.challenge.description;
    if (this.challenge.targetContext)
      userContent = `${userContent}\n\nTarget context:\n${this.challenge.targetContext}`;
    this.messages = [];
    if (this.systemPrompt != null)
      this.messages.push({ role: "system", content: this.systemPrompt });
    this.messages.push({
      role: "user",
      content: userContent,
      challenge_identity: this.challenge.identity,
    });

    this.started = this.monotonic();
    this.deadline = this.started + this.limits.wallTimeoutSeconds;
    this.abortController = new AbortController();

    const wallMs = Math.ceil(Math.max(0, this.deadline - this.started) * 1000);
    const deadlineTimer = setTimeout(() => {
      this.abortController?.abort(new Error("wall_limit"));
    }, wallMs);

    const snapshot: typeof preSnapshot = preSnapshot;
    const snapshotError: unknown = preSnapshotError;
    if (snapshot && !snapshotError) {
      try {
        this.artifacts.appendEvent("source_snapshot", {
          sha256: snapshot.digest,
          files: snapshot.files.length,
        } as unknown as Record<string, unknown>);
      } catch {
        /* ignore */
      }
    }
    if (snapshotError != null) {
      if (snapshotError instanceof InvalidChallengeSourceError) {
        const result = this.buildResult("error", "invalid_challenge_source");
        try {
          this.artifacts.commitResult(result as Record<string, unknown>);
        } catch {
          /* ignore */
        }
        try {
          snapshot?.cleanup();
        } catch {
          /* ignore */
        }
        try {
          this.artifacts.close();
        } catch {
          /* ignore */
        }
        return result;
      }
      const result = this.buildResult("error", "serialization_error");
      try {
        this.artifacts.commitResult(result as Record<string, unknown>);
      } catch {
        /* ignore */
      }
      try {
        snapshot?.cleanup();
      } catch {
        /* ignore */
      }
      try {
        this.artifacts.close();
      } catch {
        /* ignore */
      }
      return result;
    }

    let terminalWritten = false;
    try {
      if (this.expired()) {
        const active = {
          status: "unsolved",
          reason: "wall_limit",
          unprocessed: [] as string[],
        };
        const result = this.terminal(active.status, active.reason, active.unprocessed);
        terminalWritten = true;
        return result;
      }

      if (snapshot && snapshot.files.length > 0) {
        try {
          stageSourceFiles(this.artifacts.workspace, snapshot.files, () =>
            this.expired(),
          );
        } catch (e) {
          if (e instanceof InvalidChallengeSourceError) {
            const err = this.error("sandbox_error", "sandbox");
            const r = this.terminal(err.status, err.reason, err.unprocessed);
            terminalWritten = true;
            return r;
          }
          throw e;
        }
        if (this.expired()) {
          const r = this.terminal("unsolved", "wall_limit", []);
          terminalWritten = true;
          return r;
        }
      }

      const prepare = (this.executor as unknown as Record<string, unknown>).prepare as
        ((ws: string, runId: string) => unknown) | undefined;
      if (typeof prepare === "function") {
        const setRemaining = (this.executor as unknown as Record<string, unknown>)
          .setRemaining as ((remaining: number) => void) | undefined;
        if (typeof setRemaining === "function") {
          try {
            setRemaining(this.remaining());
          } catch {
            // ignore
          }
        }
        if (this.expired()) {
          const r = this.terminal("unsolved", "wall_limit", []);
          terminalWritten = true;
          return r;
        }
        const prepareMs = Math.ceil(Math.max(0, this.remaining()) * 1000);
        if (prepareMs <= 0) {
          const r = this.terminal("unsolved", "wall_limit", []);
          terminalWritten = true;
          return r;
        }
        let prepareTimer: ReturnType<typeof setTimeout> | null = null;
        const prepareDeadlinePromise = new Promise<never>((_, reject) => {
          prepareTimer = setTimeout(
            () => reject(new Error("__wall_limit__")),
            prepareMs,
          );
        });
        const preparePromise = (async () => {
          return await prepare.call(
            this.executor,
            this.artifacts.workspace,
            this.artifacts.runId,
          );
        })();
        let prepareWon = false;
        try {
          await Promise.race([preparePromise, prepareDeadlinePromise]);
          prepareWon = true;
        } catch (e) {
          if ((e as Error).message === "__wall_limit__") {
            const r = this.terminal("unsolved", "wall_limit", []);
            terminalWritten = true;
            return r;
          }
          if (this.expired()) {
            const r = this.terminal("unsolved", "wall_limit", []);
            terminalWritten = true;
            return r;
          }
          if (e instanceof SandboxError) {
            const err = this.error("sandbox_error", "sandbox", undefined, e.message);
            const r = this.terminal(err.status, err.reason, err.unprocessed);
            terminalWritten = true;
            return r;
          }
          const err = this.error("sandbox_error", "sandbox");
          const r = this.terminal(err.status, err.reason, err.unprocessed);
          terminalWritten = true;
          return r;
        } finally {
          if (prepareTimer) clearTimeout(prepareTimer);
        }
        if (!prepareWon) {
          const r = this.terminal("unsolved", "wall_limit", []);
          terminalWritten = true;
          return r;
        }
        const lifecycle = (this.executor as unknown as Record<string, unknown>)
          .sandboxLifecycle as (() => Record<string, unknown>) | undefined;
        if (typeof lifecycle === "function") {
          try {
            this.artifacts.appendEvent(
              "sandbox_lifecycle",
              lifecycle.call(this.executor) as Record<string, unknown>,
            );
          } catch {
            // ignore
          }
        }
        if (this.expired()) {
          const r = this.terminal("unsolved", "wall_limit", []);
          terminalWritten = true;
          return r;
        }
      }

      const active = await this.runActive();
      // If runActive returned wall_limit directly, terminal handles it
      try {
        const result = this.terminal(
          active.status,
          active.reason,
          active.unprocessed,
          active.candidateFlag,
        );
        terminalWritten = true;
        return result;
      } catch (e) {
        // commit failures that are OSError/TypeError/ValueError map to serialization_error if not already terminal
        if (
          e instanceof Error &&
          (e.name === "TypeError" || e.message.includes("strict JSON"))
        ) {
          terminalWritten = true;
          throw e;
        }
        terminalWritten = true;
        throw e;
      }
    } catch (e) {
      if (terminalWritten) throw e;
      // Serialization errors
      if (
        e instanceof TypeError ||
        e instanceof SyntaxError ||
        (e instanceof Error && e.message.includes("strict JSON"))
      ) {
        const result = this.buildResult("error", "serialization_error");
        try {
          this.artifacts.commitResult(result as Record<string, unknown>);
        } catch {
          // if commit fails, propagate
          throw e;
        }
        return result;
      }
      throw e;
    } finally {
      clearTimeout(deadlineTimer);
      this.abortController?.abort(new Error("run finished"));
      try {
        snapshot?.cleanup();
      } catch {
        /* ignore */
      }
      const cleanup = (this.executor as unknown as Record<string, unknown>).cleanup as
        ((runId: string) => unknown) | undefined;
      if (typeof cleanup === "function") {
        try {
          await cleanup.call(this.executor, this.artifacts.runId);
        } catch (err) {
          try {
            this.artifacts.appendEvent("sandbox_cleanup_failed", {
              error_type: (err as Error).name ?? "Error",
            } as Record<string, unknown>);
          } catch {
            // ignore
          }
        }
      }
      try {
        this.artifacts.close();
      } catch {
        // ignore
      }
    }
  }

  private async runActive(): Promise<{
    status: string;
    reason: string;
    unprocessed: string[];
    candidateFlag?: string;
  }> {
    while (true) {
      if (this.expired())
        return { status: "unsolved", reason: "wall_limit", unprocessed: [] };
      if (this.modelCalls >= this.limits.maxModelTurns) {
        return { status: "unsolved", reason: "model_turn_limit", unprocessed: [] };
      }
      this.modelCalls += 1;
      const remaining = this.remaining();
      if (remaining <= 0)
        return { status: "unsolved", reason: "wall_limit", unprocessed: [] };

      // Pass remaining budget to provider (defense in depth, ms conversion happens in provider)
      const setter = (this.model as unknown as Record<string, unknown>).setRemaining as
        ((r: number) => void) | undefined;
      const setterSnake = (this.model as unknown as Record<string, unknown>)
        .set_remaining as ((r: number) => void) | undefined;
      const fn = setter ?? setterSnake;
      if (typeof fn === "function") {
        try {
          fn.call(this.model, remaining);
        } catch {
          // ignore invalid remaining
        }
      }

      if (this.abortController) {
        (this.model as unknown as Record<string, unknown>)._signal =
          this.abortController.signal;
      }
      const setMono = (
        this.model as unknown as { setMonotonic?: (m: () => number) => void }
      ).setMonotonic;
      if (typeof setMono === "function") {
        try {
          setMono.call(this.model, this.monotonic);
        } catch {
          /* ignore */
        }
      }
      type CommitSuccess = {
        kind: "success";
        response: ModelResponse;
        committedAt: number;
      };
      type CommitFailure = { kind: "failure"; error: unknown; committedAt: number };
      type Commit = CommitSuccess | CommitFailure;

      let commit: Commit | null = null;

      const readCommittedAt = (): number => {
        const v = (this.model as unknown as { lastCommittedAt?: number })
          .lastCommittedAt;
        return typeof v === "number" && Number.isFinite(v) ? v : this.monotonic();
      };

      try {
        if (this.expired() || this.remaining() <= 0) {
          return { status: "unsolved", reason: "wall_limit", unprocessed: [] };
        }
        const deadlineMs = Math.ceil(
          Math.max(0, this.deadline - this.monotonic()) * 1000,
        );
        if (deadlineMs <= 0) {
          return { status: "unsolved", reason: "wall_limit", unprocessed: [] };
        }
        const commitPromise: Promise<Commit> = this.invokeModel().then(
          (response) => ({
            kind: "success" as const,
            response,
            committedAt: readCommittedAt(),
          }),
          (error) => ({
            kind: "failure" as const,
            error,
            committedAt: readCommittedAt(),
          }),
        );

        let deadlineTimer: ReturnType<typeof setTimeout> | null = null;
        const deadlinePromise = new Promise<never>((_, reject) => {
          deadlineTimer = setTimeout(() => {
            this.abortController?.abort(new Error("wall_limit"));
            reject(new Error("__wall_limit__"));
          }, deadlineMs);
        });

        try {
          commit = await Promise.race([commitPromise, deadlinePromise]);
        } catch (e) {
          if ((e as Error).message === "__wall_limit__") {
            const settled = await Promise.race([
              commitPromise.then(
                (c) => c,
                () => null,
              ),
              new Promise<null>((resolve) => setTimeout(() => resolve(null), 150)),
            ]);
            if (settled != null && settled.committedAt < this.deadline) {
              commit = settled;
            } else {
              if (deadlineTimer) clearTimeout(deadlineTimer);
              return { status: "unsolved", reason: "wall_limit", unprocessed: [] };
            }
          } else {
            throw e;
          }
        } finally {
          if (deadlineTimer) clearTimeout(deadlineTimer);
        }
      } catch (e) {
        if ((e as Error).message === "__wall_limit__") {
          return { status: "unsolved", reason: "wall_limit", unprocessed: [] };
        }
        const isAbort =
          (e as Error).name === "AbortError" ||
          (e as Error).message.includes("aborted") ||
          (e as Error).message.includes("AbortError");
        if (isAbort && this.expired()) {
          return { status: "unsolved", reason: "wall_limit", unprocessed: [] };
        }
        if (this.expired()) {
          return { status: "unsolved", reason: "wall_limit", unprocessed: [] };
        }
        const err = this.error("provider_error", "model");
        return { status: err.status, reason: err.reason, unprocessed: err.unprocessed };
      }

      if (commit == null) {
        return { status: "unsolved", reason: "wall_limit", unprocessed: [] };
      }
      if (commit.committedAt >= this.deadline) {
        return { status: "unsolved", reason: "wall_limit", unprocessed: [] };
      }
      if (commit.kind === "failure") {
        const logged = this.error("provider_error", "model");
        return {
          status: logged.status,
          reason: logged.reason,
          unprocessed: logged.unprocessed,
        };
      }
      const response: ModelResponse = commit.response;

      // Provider error mapping: ProviderError thrown above already handled; successful response continues
      this.addUsage((response as ModelResponse).usage);
      const duplicate = this.duplicateId(response as ModelResponse);
      const accepted = duplicate == null;
      this.artifacts.appendEvent("model_response", {
        model_call: this.modelCalls,
        accepted,
        ...(response as ModelResponse).toObject(),
      } as Record<string, unknown>);
      if (duplicate != null) {
        if (this.expired()) {
          return {
            status: "unsolved",
            reason: "wall_limit",
            unprocessed: (response as ModelResponse).toolCalls.map((c) => c.callId),
          };
        }
        const err = this.error("provider_error", "model");
        return { status: err.status, reason: err.reason, unprocessed: err.unprocessed };
      }
      for (const c of (response as ModelResponse).toolCalls)
        this.seenCallIds.add(c.callId);
      this.messages.push({
        role: "assistant",
        content: (response as ModelResponse).content,
        tool_calls: (response as ModelResponse).toolCalls.map((c) => c.toObject()),
      });

      if (this.expired()) {
        return {
          status: "unsolved",
          reason: "wall_limit",
          unprocessed: (response as ModelResponse).toolCalls.map((c) => c.callId),
        };
      }
      if ((response as ModelResponse).truncated) {
        return {
          status: "unsolved",
          reason: "model_output_limit",
          unprocessed: (response as ModelResponse).toolCalls.map((c) => c.callId),
        };
      }
      if ((response as ModelResponse).toolCalls.length === 0) {
        return { status: "unsolved", reason: "model_stop", unprocessed: [] };
      }
      const terminal = await this.dispatch(response as ModelResponse);
      if (terminal != null) return terminal;
      if (this.modelCalls >= this.limits.maxModelTurns) {
        return { status: "unsolved", reason: "model_turn_limit", unprocessed: [] };
      }
    }
  }

  private async invokeModel(): Promise<ModelResponse> {
    // If model is a real provider, try to pass AbortSignal if it supports it.
    // Providers accept signal via second arg opts? Our providers currently take only (messages, tools) but we can inject signal via a wrapper.
    // Simplest: call generate directly; the provider's internal fetch will use its own timeout (remaining budget) as defense.
    // For deadline supervision, the AbortController abort will cause fetch to throw AbortError which we catch.
    // To make signal reach SDK, we set it on the client via withOptions if available — already done via setRemaining's timeout.
    // For determinism with fakes, just await.
    const result = this.model.generate(
      this.messages as unknown as Record<string, unknown>[],
      TOOL_DEFINITIONS as unknown as Record<string, unknown>[],
    );
    // Support both sync and async return
    return await result;
  }

  private addUsage(usage: unknown): void {
    if (usage == null || typeof usage !== "object" || Array.isArray(usage)) return;
    const rec = usage as Record<string, unknown>;
    for (const [key, attr] of [
      ["input_tokens", "inputTokens"],
      ["output_tokens", "outputTokens"],
    ] as const) {
      const v = rec[key];
      if (typeof v !== "number" || !Number.isInteger(v)) continue;
      const cur = (this as unknown as Record<string, unknown>)[attr] as
        number | undefined;
      (this as unknown as Record<string, unknown>)[attr] = cur == null ? v : cur + v;
    }
  }

  private duplicateId(response: ModelResponse): string | null {
    const current = new Set<string>();
    for (const call of response.toolCalls) {
      if (current.has(call.callId) || this.seenCallIds.has(call.callId))
        return call.callId;
      current.add(call.callId);
    }
    return null;
  }

  private async dispatch(response: ModelResponse): Promise<{
    status: string;
    reason: string;
    unprocessed: string[];
    candidateFlag?: string;
  } | null> {
    const calls = response.toolCalls;
    for (let index = 0; index < calls.length; index++) {
      const call = calls[index]!;
      if (this.expired()) {
        return {
          status: "unsolved",
          reason: "wall_limit",
          unprocessed: calls.slice(index).map((c) => c.callId),
        };
      }
      this.artifacts.appendEvent("tool_call", {
        call_id: call.callId,
        source_index: index,
        name: call.name,
        arguments: call.arguments,
      } as Record<string, unknown>);
      this.toolCalls += 1;
      if (call.name !== "shell" && call.name !== "submit_flag") {
        this.toolResult(
          call.callId,
          call.name,
          { ok: false, error: { type: "unknown_tool" } },
          false,
        );
        continue;
      }
      let args: Record<string, string>;
      try {
        args = validateToolArguments(call.name, call.arguments);
      } catch (e) {
        const isUnknown = e instanceof UnknownToolError;
        if (isUnknown) {
          this.toolResult(
            call.callId,
            call.name,
            { ok: false, error: { type: "unknown_tool" } },
            false,
          );
        } else {
          this.toolResult(
            call.callId,
            call.name,
            { ok: false, error: { type: "invalid_arguments" } },
            false,
          );
        }
        continue;
      }
      let terminal: {
        status: string;
        reason: string;
        unprocessed: string[];
        candidateFlag?: string;
      } | null = null;
      if (call.name === "shell") {
        terminal = await this.shell(call.callId, args.command);
      } else {
        terminal = await this.submit(call.callId, args.candidate);
      }
      if (terminal != null) {
        if (
          (terminal.status === "solved" && terminal.reason === "verified_flag") ||
          (terminal.status === "submitted" && terminal.reason === "unverified_flag")
        ) {
          const remaining = calls.slice(index + 1).map((c) => c.callId);
          return {
            status: terminal.status,
            reason: terminal.reason,
            unprocessed: remaining,
            ...(terminal.candidateFlag != null
              ? { candidateFlag: terminal.candidateFlag }
              : {}),
          };
        }
        return terminal;
      }
    }
    return null;
  }

  private async shell(
    callId: string,
    command: string,
  ): Promise<{
    status: string;
    reason: string;
    unprocessed: string[];
    candidateFlag?: string;
  } | null> {
    if (this.expired() || this.remaining() <= 0) {
      return { status: "unsolved", reason: "wall_limit", unprocessed: [callId] };
    }
    const timeout = Math.min(this.limits.commandTimeoutSeconds, this.remaining());
    if (timeout <= 0) {
      return { status: "unsolved", reason: "wall_limit", unprocessed: [callId] };
    }
    const setter = (this.executor as unknown as Record<string, unknown>)
      .setExecutionDeadline as
      ((deadline: number, monotonic: () => number) => void) | undefined;
    if (typeof setter === "function") {
      setter.call(this.executor, this.deadline, this.monotonic);
    }
    if (this.expired() || this.remaining() <= 0) {
      return { status: "unsolved", reason: "wall_limit", unprocessed: [callId] };
    }
    try {
      const raw = await (
        this.executor.execute as (
          c: string,
          t: number,
        ) =>
          Promise<import("./tools.js").ShellResult> | import("./tools.js").ShellResult
      )(command, timeout);
      const [modelResult, loggedResult] = normalizeShellResult(
        raw,
        this.limits.maxModelToolOutputBytes,
        this.limits.maxLoggedToolOutputBytes,
      );
      this.toolResult(
        callId,
        "shell",
        modelResult.toObject() as unknown as Record<string, unknown>,
        true,
        loggedResult.toObject() as unknown as Record<string, unknown>,
      );
      if (this.expired())
        return { status: "unsolved", reason: "wall_limit", unprocessed: [] };
      return null;
    } catch (e) {
      if (this.expired())
        return { status: "unsolved", reason: "wall_limit", unprocessed: [] };
      if (e instanceof SandboxError) {
        const err = this.error("sandbox_error", "sandbox", callId, e.message);
        return { status: err.status, reason: err.reason, unprocessed: err.unprocessed };
      }
      const err = this.error("tool_error", "executor", callId);
      return { status: err.status, reason: err.reason, unprocessed: err.unprocessed };
    }
  }

  private async submit(
    callId: string,
    candidate: string,
  ): Promise<{
    status: string;
    reason: string;
    unprocessed: string[];
    candidateFlag?: string;
  } | null> {
    if (this.expired()) {
      return { status: "unsolved", reason: "wall_limit", unprocessed: [callId] };
    }
    const stripped = candidate.trim();
    if (this.expired()) {
      return { status: "unsolved", reason: "wall_limit", unprocessed: [callId] };
    }
    this.artifacts.appendEvent("flag_submission", {
      call_id: callId,
      candidate: stripped,
    } as Record<string, unknown>);
    this.flagSubmissions += 1;
    if (this.verifier == null) {
      this.toolResult(callId, "submit_flag", { outcome: "submitted" }, true);
      if (this.expired())
        return { status: "unsolved", reason: "wall_limit", unprocessed: [] };
      return {
        status: "submitted",
        reason: "unverified_flag",
        unprocessed: [],
        candidateFlag: stripped,
      };
    }
    let outcome: string;
    try {
      if (this.expired()) {
        return { status: "unsolved", reason: "wall_limit", unprocessed: [callId] };
      }
      outcome = this.verifier.check(stripped);
      if (outcome !== "correct" && outcome !== "incorrect")
        throw new Error("unsupported verifier outcome");
    } catch {
      if (this.expired())
        return { status: "unsolved", reason: "wall_limit", unprocessed: [callId] };
      const err = this.error("verifier_error", "verifier", callId);
      return { status: err.status, reason: err.reason, unprocessed: err.unprocessed };
    }
    this.artifacts.appendEvent("verifier_result", {
      call_id: callId,
      outcome,
    } as Record<string, unknown>);
    this.toolResult(callId, "submit_flag", { outcome }, true);
    if (this.expired())
      return { status: "unsolved", reason: "wall_limit", unprocessed: [] };
    if (outcome === "correct")
      return { status: "solved", reason: "verified_flag", unprocessed: [] };
    return null;
  }
}
