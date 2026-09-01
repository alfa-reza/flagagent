# FlagAgent v0.2.0 Architecture

> **Release:** v0.2.0
> **Scope:** authorized CTFs, security labs, benchmarks, and sandboxed experiments
> **Reference platform:** Node 24 LTS + TypeScript ESM + Linux Docker Engine

This document describes the **as-built architecture of FlagAgent v0.2.0**.

## 1. Overview

v0.2.0 is a TypeScript rewrite of the v0.1 Python harness. It preserves the v0.1 trust model and invariants while moving the runtime to Node 24, TypeScript `strict`, ESM, and npm. The agent exposes two tools (`shell`, `submit_flag`), runs commands in a run-scoped Docker container, and considers a run solved only when the trusted verifier accepts a flag.

## 2. System boundaries

```
Challenge directory ──► CLI / Challenge loader ──► AgentLoop ◄──► Model adapter
                           │                       │  ├── shell ──► DockerExecutor ──► Agent (/workspace)
                           │                       │  └── submit_flag ──► Verifier
                           │                       └──► RunArtifacts (run.json, events.jsonl, result.json, workspace) ──► writeup.md
                           └── expected_flag ─────► Verifier only
```

- **CLI / challenge loader** validates `challenge.json` (64 KiB, UTF-8, allowlist) and optional `files/`, constructs `Limits`, `DockerExecutor`, provider adapter, and `AgentLoop`.
- **Model adapters** normalize OpenAI Chat, OpenAI Responses, and Anthropic Messages to `ModelResponse { content, tool_calls, usage, truncated }`.
- **AgentLoop** owns conversation, wall deadline, turn budgets, tool dispatch, and terminal decision.
- **DockerExecutor** owns the Agent container and, for `local`, the run-scoped internal bridge network and Target fixture (`target:9999`).
- **RunArtifacts / writeup** persist `run.json`, `events.jsonl`, `result.json`, `workspace/`, and derived `writeup.md`.

## 3. Runtime

- **Node 24 LTS**, **TypeScript `strict`**, **ESM**, **npm** (committed `package-lock.json`), **Vitest**, **ESLint + Prettier**.
- **Providers:** `openai` and `@anthropic-ai/sdk` with budgeted `seconds→ms` timeout + `maxRetries:0` vs unbudgeted SDK defaults.
- **Sandbox:** Docker CLI via async `spawn` with bounded output, wall-deadline budgets, and reaped stdio.
- **Staging:** Linux `procfd`/`O_NOFOLLOW` snapshot into temp, then copy to `workspace/` preserving exec bits; deterministic `FLAGAGENT-SOURCE-V1` digest.

## 4. D013 semantic completion

A provider invocation becomes semantically committed after the adapter has obtained and normalized the complete result/error and updated provider-owned replay/continuation state required by subsequent turns, then records the current-invocation completion witness before returning or throwing that completed outcome to AgentLoop.

AgentLoop arbitrates the witness against the absolute wall deadline: valid pre-deadline completed evidence is not discarded solely because parent observation occurs later; late/incomplete completion is not treated as pre-deadline; model-requested tools do not begin after the deadline.

## 5. Executor cooperation

`Executor.prepare()` is cooperative: custom implementations must be non-blocking with respect to the Node event loop, bound their async work, and respect `setRemaining` / `setExecutionDeadline` budgets where applicable. v0.2.0 does not promise hard preemption for arbitrary synchronous custom executors. `DockerExecutor` is the supported production implementation.

## 6. Security & invariants

Only `shell`/`submit_flag` are model-visible; verifier alone establishes `solved`; wall deadline is authoritative; staging uses `procfd`/`O_NOFOLLOW`; containers run non-root, `--cap-drop ALL`, `no-new-privileges`, bounded resources, no host network unless `local` internal network.

## 7. Decisions

**D011 — Manual strict-JSON validation:** FlagAgent uses explicit `isRecord`/`snapshotJson` with null-prototype snapshots for untrusted model/tool/artifact JSON. This is intentional `__proto__`-pollution hardening (see `src/flagagent/model.ts`). Do not introduce a schema dependency without demonstrated need.

## 8. Artifacts

`result.json` is authoritative; `run.json` records static sandbox provenance (`docker_engine`, `rootless`, `flagagent_version` best-effort, image reference, network mode); dynamic lifecycle identifiers (container, network, image IDs) are recorded in `sandbox_lifecycle` events in `events.jsonl`; `events.jsonl` is the trajectory; `workspace/` is the agent’s writable bind.
