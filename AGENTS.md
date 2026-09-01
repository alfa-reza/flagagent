# FlagAgent Repository Instructions

FlagAgent is a small, model-independent LLM agent harness for authorized CTFs, security labs, benchmarks, and sandboxed experiments.

## Engineering Principles

Prefer the smallest correct design.

- **KISS:** choose the simplest implementation that satisfies the concrete requirement.
- **YAGNI:** do not add features, abstractions, dependencies, configuration, or extensibility for hypothetical future needs.
- **DRY with restraint:** remove meaningful duplication, but do not abstract small incidental repetition.
- Prefer explicit control flow, clear ownership, and deterministic verification.
- Read relevant code and tests before changing behavior.
- Avoid unrelated refactors while implementing focused work.

## Architecture

- **Runtime:** Node.js 24 LTS, TypeScript `strict`, ESM, npm.
- **Providers:** official `openai` and `@anthropic-ai/sdk` (Chat Completions, Responses, Anthropic Messages).
- **Sandbox:** Docker CLI, run-scoped Agent container (plus optional internal network/target for `local` mode).
- **Core modules:** `src/flagagent/loop.ts` (orchestration, deadlines), `src/flagagent/docker.ts` (sandbox lifecycle), `src/flagagent/staging.ts` (source snapshot/staging), `src/flagagent/providers/*` (adapters), `src/flagagent/artifacts.ts` / `writeup.ts` (persistence), `src/flagagent/cli.ts` (challenge loading + CLI).

## Verification

```bash
npm ci
npm run typecheck
npm test
npm run build
npm run lint
npm run format:check
npm pack --dry-run
git diff --check
```

Docker-backed integration requires a reachable Docker Engine.

## Security & Invariants

- Only `shell` and `submit_flag` are exposed; unknown/malformed tool calls never execute.
- Only the verifier establishes `solved`; provider credentials and expected flag stay control-side.
- One absolute Run wall deadline is authoritative; no tool executes after it wins.
- Source staging uses procfd-anchored `O_NOFOLLOW` techniques; do not replace with naive path-based copy.
- Docker containers run non-root, `--cap-drop ALL`, `no-new-privileges`, bounded resources, no host network/socket unless explicitly required by `local` mode's internal network.

## Dependencies & Git

- Add dependencies only for demonstrated need; prefer Node built-ins and official SDK capabilities.
- Do not add agent/DI/Docker-SDK/state-machine/plugin frameworks without explicit justification.
- Keep `package-lock.json` committed; use Conventional Commits; do not rewrite history or force-push.
- Do not touch `.github/workflows/opencode.yml`.
