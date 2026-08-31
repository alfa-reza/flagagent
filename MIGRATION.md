# FlagAgent v0.2.0 TypeScript Migration

> Temporary execution plan for the Python v0.1.1 → TypeScript v0.2.0 rewrite.
> `AGENTS.md` is authoritative for durable engineering, research, security, Git, and verification rules.

## Status
- **Source:** `v0.1.1`
- **Target:** `v0.2.0`
- **Strategy:** greenfield parallel rewrite
- **Current milestone:** RC-2 closure — all local gates PASS, remote CI pending
- **Blockers:** none (local)
- [x] M0 — Foundation, core, artifacts, providers (ae39b47, bf94602 + b08cfdf carry-forward)
- [x] M1 — AgentLoop and provider supervision (a4aa81d + closure `94ef11f` corrective)
- [x] M2 — Source staging, Docker, CLI, integration (partial `64fc4f9` + corrections `78d03f6` etc.)
- [x] RC — Provider/deadline semantic proof, Docker lifecycle/provenance, staging boundaries, CLI/package real-artifact, real-Docker packed CLI integration (local PASS)
- [x] RC-2 — Test partition, Actions v7, direct bin proof, local network, provenance parity, sentinel flag isolation (local PASS — see RC-2 gate below)

Complete one milestone, verify it, update this file, then stop.

## Source
FlagAgent v0.1.1 remains the executable oracle until final cutover.

Baseline:
- Python 3.12+ with `uv`
- 13 production Python files / 4,671 LOC
- 25 Python test files / 11,625 LOC
- 418 test functions
- tools: `shell`, `submit_flag`
- providers: OpenAI Chat, OpenAI Responses, Anthropic Messages
- sandbox: Docker CLI
- artifacts: `run.json`, `events.jsonl`, `result.json`, `writeup.md`, `workspace/`
- statuses: `solved`, `unsolved`, `error`

High-risk areas: `loop.py`, `docker_executor.py`, provider supervision/normalization, and regressions #47/#54/#56.

### Baseline gate (observed 2026-08-29, before M0)
- [x] `uv sync` — pass
- [x] `uv lock --check` — pass
- [x] `uv run pytest` — 334 passed, 1 failed (`test_run_metadata_is_not_rewritten_as_trajectory_state` expects `flagagent_version 0.1.0` but code reports `0.1.1`; inherited)
- [x] `uv run ruff check .` — 63 errors (unused-try-pass in issue47/56 tests, line length)
- [x] `uv run ruff format --check .` — 1 file needs reformatting
- [x] `uv build` — pass (sdist + wheel)
- [x] `git diff --check` — pass
- [x] Docker-backed tests — deferred (no Docker in this env; full suite needs Docker for executor/networking)
- [x] `test_limits.py::test_model_response_returned_after_wall_deadline_is_preserved` — failed (KeyError `input_tokens`) — inherited, not caused by M0

**Inherited baseline failures:** 2 pre-existing failures unrelated to M0 migration:
- `test_audit.py::test_run_metadata_is_not_rewritten_as_trajectory_state` — version mismatch `0.1.0` vs `0.1.1`.
- `test_limits.py::test_model_response_returned_after_wall_deadline_is_preserved` — deterministic under this Python env.

Do not edit legacy Python merely to make the rewrite easier. Record inherited failures or contradictory regressions before changing behavior.

## Target
Fixed direction:
- TypeScript
- Node.js 24 LTS
- TypeScript `strict`
- ESM
- npm + committed `package-lock.json`
- official `openai` TypeScript SDK
- official `@anthropic-ai/sdk`
- custom FlagAgent loop
- Docker remains the sandbox boundary

Validate during M0 rather than assume:
- test runner (Vitest is the default candidate)
- minimum lint/format toolchain
- manual runtime validators vs one schema library
- exact TypeScript module layout

Prefer Node built-ins and official SDK capabilities over extra dependencies. Do not adopt an agent framework as FlagAgent's core runtime.

## Strategy
Use a **greenfield parallel rewrite**:
1. keep Python v0.1.1 runnable in its current paths;
2. build TypeScript beside it;
3. migrate by subsystem/behavior, not file-for-file;
4. port/rewrite relevant tests with each subsystem;
5. compare selected observable behavior against Python;
6. cut over only after deterministic final verification;
7. remove Python and migration-only material at the end.

Do **not** create Python↔TypeScript FFI, HTTP, subprocess bridge, or compatibility service unless a real requirement appears.

Likely consolidation:
- `providers.py` + `responses.py` → OpenAI provider module
- `anthropic_messages.py` → Anthropic provider module
- `provider_process.py` → provider-supervision responsibility, not literal IPC translation
- `artifacts.py` + `writeup.py` → artifact module if cohesion remains clear
- source-staging logic in `loop.py` → separate source-staging module
- remaining orchestration in `loop.py` → AgentLoop
- `prompt.py` may be merged where ownership is clearest

Do not optimize for minimum LOC/file count. Optimize for fewer concepts, states, dependencies, synchronization mechanisms, and failure paths.

## Non-Goals
Do not add during migration unless separately approved:
- multi-agent orchestration
- MCP as a FlagAgent product feature
- new provider protocols or provider-routing framework
- new sandbox backends
- resume/checkpoint
- database/service state
- web UI
- plugin/DI/middleware framework
- speculative extension points
- unrelated behavior cleanup

If a new bug or hardening opportunity is discovered, record it as a follow-up or explicit deviation. Do not silently mix it into parity work.

## Compatibility Priorities
Preserve requirements and observable semantics, not Python mechanisms:
- verifier alone establishes `solved`
- `shell` and `submit_flag` behavior
- malformed/unknown tools never execute
- non-zero shell exits remain execution evidence
- incorrect flag submissions do not automatically become harness errors
- one absolute Run wall deadline
- zero post-deadline model-requested tool execution
- provider completion semantics covered by #47/#54/#56
- Docker isolation, ownership, cleanup, networking, resources, recovery
- secure source staging and executable-bit behavior
- normalized behavior across all three provider paths
- artifact semantics and `result.json` terminal authority
- current protocol names and CLI behavior where practical

Record any deliberate incompatibility under **Decisions / Deviations** before acceptance.

## Verification
### Test migration
Do not translate all 418 Python tests mechanically. For relevant tests:
- **PORT** — same behavior, direct TypeScript regression
- **REWRITE** — same invariant, target-appropriate test
- **RETIRE** — Python-only implementation detail with behavior covered elsewhere
- **PARITY** — portable scenario against both implementations

Port tests with the subsystem they protect.

### Cross-language parity
Use the smallest representative parity set needed for externally observable behavior not sufficiently proven by target tests. Candidate cases:
- model stops without tools
- `shell` → observation → next turn
- incorrect flag → continue
- correct flag → verifier establishes `solved`
- turn limit
- absolute wall deadline
- provider completes pre-deadline but is observed later
- canonical run/result/artifact trajectory

Normalize timestamps, durations, run IDs, and temporary paths. Validate the parity harness before trusting it.

### CI
The current repository has no normal test/typecheck/build CI. M0 must add a minimal target CI workflow. Final completion must be demonstrated by deterministic checks on the current commit/PR head, not only by an agent's local report.

# M0 — Foundation, Core, Artifacts, Providers
## Scope
Implement:
- package/TypeScript/ESM setup
- target test runner
- minimum lint/format setup
- minimal GitHub Actions CI
- canonical model/tool/verifier contracts
- limits/truncation
- artifacts/writeup
- OpenAI Chat + Responses adapter
- Anthropic adapter
- relevant TypeScript regressions

Do not implement AgentLoop, provider supervision, source staging, or Docker yet.

## Research
Follow `AGENTS.md`. Use Context7 for current third-party APIs used in M0, especially OpenAI, Anthropic, TypeScript/tooling, and any schema library under consideration.

## Decisions
- **D011:** manual validators — no schema library. Python oracle uses manual `isinstance`/`json` checks; M0 surface (ToolCall args, ToolDefinition, ShellResult, Limits, Artifact validation, writeup) small and security-sensitive (`__proto__` pollution). Chosen: explicit `isRecord`/`snapshotJson` with `Object.create(null)` + `Object.defineProperty` for `__proto__` safety, `Object.hasOwn` exact checks. Revisit only if schema count grows beyond trivial.
- **D012:** Vitest (`^4.1.4`) + ESLint flat (`^9.39.5`) + `typescript-eslint` (`^8.46.2`) + Prettier (`^3.6.2`) + TypeScript (`~6.0.3`). No coverage in M0. Scripts: `test`/`test:watch`/`typecheck`/`build`/`lint`/`format:check`.
- **D015:** TypeScript ESM compiler/module settings — `target ES2023`, `module NodeNext`, `moduleResolution NodeNext`, `strict true`, `types ["node"]`, `esModuleInterop`, `forceConsistentCasingInFileNames`, `isolatedModules`, `declaration`, `sourceMap`, `rootDir src`, `outDir dist`, `skipLibCheck`, `noEmitOnError`. Verified against TS 6.0.3 + Node 24.18.0 + `type: module` + `engines >=24`. Defer re-check only if TS major bumps.
- **D016:** Provider timeout/retry — budgeted `seconds → milliseconds` via `Math.ceil(seconds*1000)` + `maxRetries: 0`; unbudgeted leaves SDK defaults untouched. Verified against locked `openai@5.23.2` (`DEFAULT_TIMEOUT 600000`, `maxRetries 2`, `withOptions` clone) + `@anthropic-ai/sdk@0.67.1` (same). SDK default `600000 ms` is *not* a FlagAgent contract; budgeted vs unbudgeted split tested.

## Gate (observed 2026-08-29, M0 complete)
- [x] strict typecheck passes (`npm run typecheck` — `tsc --noEmit`)
- [x] M0 tests pass (`npm test` — 39 tests: 26 core + 13 providers)
- [x] build passes (`npm run build` + `npm pack` smoke)
- [x] lint/format check passes (`eslint .`, `prettier --check`)
- [ ] minimal CI is green — CI workflow added as `.github/workflows/ci.yml` (separate from forbidden `opencode.yml`); local `act` not run; needs first green run on push
- [x] SDK usage verified against locked versions and current docs (openai 5.23.2, anthropic 0.67.1; `withOptions` vs `RequestOptions` second arg both covered)
- [x] no unnecessary runtime dependency/framework added (only `openai`, `@anthropic-ai/sdk` as runtime deps; no zod/agent framework)
- [x] Python oracle remains runnable (same inherited 2 failures as baseline)
- [x] diff reviewed for KISS/YAGNI — consolidation `providers.py`+`responses.py`→`providers/chat|responses`, `anthropic_messages.py`→`providers/anthropic`, no DI/event-bus/framework

**M0 status:** complete (2 commits: `ae39b47 feat(core)` + providers slice pending push). Commits are independently green.

Observations:
- Python truncation is `data[:limit].decode("utf-8", errors="ignore")` — TS mirrors via `.replace(/\uFFFD/g,"")` after `toString("utf8")`; byte-budget shrinking loop preserved.
- `writeup` metadata fields now routed through `codeSpan` (challenge identity etc. previously raw backtick interpolation — hardening included in M0, noted as explicit change, not assumed parity).
- `Model.generate` stays synchronous in M0 contracts; async widening deferred to M1 when supervision needs it (reviewer noted — not blocking for M0 core).
- `readEvents` handles `\r\n` and trailing incomplete line; poisoned stream covers early JSON validation failures via catch-all try.
- `validateRunId` rejects `..`, separator, Docker delimiters, whitespace per Python `validate_run_id`.

Reviewer disposition: fixed blockers — `__proto__` pollution via `Object.create(null)` + `Object.hasOwn`, Prettier formatting, UTF-8 prefix byte-boundary, writeup escaping. Remaining: async `Model` deferred to M1, raw invalid-UTF-8 strict decoding considered minor (deferred).

**M0 artifacts:**
- `package.json`/`package-lock.json`, `tsconfig.json`, `vitest.config.ts`, `eslint.config.js`, `.prettierrc`, `.github/workflows/ci.yml`
- `src/flagagent/model.ts`, `tools.ts`, `limits.ts`, `artifacts.ts`, `writeup.ts`, `prompt.ts`, `version.ts`, `index.ts`
- `src/flagagent/providers/chat.ts`, `providers/responses.ts`, `providers/anthropic.ts`, `providers/index.ts`
- `tests/core.test.ts` (26), `tests/providers.test.ts` (13) — PORT/REWRITE for M0 scope; no AgentLoop/Docker/CLI migrated yet.

# M1 — AgentLoop and Provider Supervision
## Scope
Implement AgentLoop, scripted/fake model/executor paths, terminal status/reason behavior, verifier + event/artifact ordering, the smallest provider-supervision mechanism satisfying existing regressions, and loop/provider/deadline tests.

Do not start source-staging, Docker, or CLI migration.

## Required semantics
Issues #47, #54, #56 are hard gates:
- one absolute Run wall deadline
- no tool execution after the deadline wins
- provider evidence fully completed before the deadline is not lost only because parent/event-loop observation occurs later
- late/partial completion is not treated as pre-deadline success
- large responses do not create transport deadlock/backpressure cycles

`AbortSignal`, SDK timeout, `Promise.race()`, worker, or child process are implementation options, not correctness proofs.

## Decision
- **D013:** provider supervision — choose the smallest deterministic design; do not port Python multiprocessing/pipes literally unless evidence requires equivalent machinery

## Gate (observed 2026-08-30, M1 corrective closure `94ef11f`)
- [x] AgentLoop tests pass (`npm test` — loop.test.ts 8, deadline.test.ts 9, transport.test.ts 12, core 26, providers 13 = 74 incl. staging/e2e)
- [x] #47 regressions pass — real SDK adapters via local http stubs: header-stall and body-drip for Chat/Responses/Anthropic all lose to absolute wall, wall_limit before payload finishes, no tool_call, wallMs timers kept referenced, AbortSignal propagated per-request
- [x] #54 regressions pass — transport-stamped committedAt via provider `lastCommittedAt` (captured inside fetch settlement, before event-loop microtask), unconditional preservation: success with model_response/usage/unprocessed, provider_error preserved, post-deadline rejected, bounded 150ms drain
- [x] #56 / large-response regressions pass — 300 KiB via real SDK + direct ScriptedModel both succeed without deadlock; wall_limit large preserved test proves committedAt path
- [x] deadline/completion semantics deterministic — one absolute monotonic wall deadline, bounded prepare (Promise.race remaining), shell double-check after setExecutionDeadline, verifier flag_submission gated before event, deadline timers kept referenced, AbortController per Run, cleanup outside budget
- [x] typecheck/build/lint pass (`tsc --noEmit`, `tsc`, `eslint .`, `prettier --check`, `npm pack --dry-run`)
- [x] Python oracle sampled — `test_issue54_deadline.py` 3, `test_issue56_large_response.py` 3 passed
- [x] synchronization/state reviewed — AbortController + Promise.race + provider-stamped committedAt; no Worker/SharedArrayBuffer needed; per-Run provider state via instance fields

**M1 status:** corrective closure `94ef11f`. Verified: provider-stamped witness, admission double-checks, bounded prepare, 74 tests green.

Observations:
- Provider witness: `captureCommittedAt()` inside provider `generate()` settlement (after full parse + state mutation like `builtInput`/`thinkingHistory`), loop reads `lastCommittedAt` via `readCommittedAt()` — no longer sampling in parent .then microtask.
- Bounded prepare: `prepare` raced against remaining deadline; wall_limit if exceeds.
- Shell: re-check `expired()`/`remaining()` after `setExecutionDeadline` before `execute`.
- Verifier: `flag_submission` event now gated before record; double-check before `verifier.check`.
- Transport and replay/thinking unchanged and still green.

**M1 artifacts:**
- `src/flagagent/loop.ts` — provider-stamped witness + admission fixes
- `src/flagagent/providers/chat.ts` / `responses.ts` / `anthropic.ts` — `setMonotonic` + `lastCommittedAt` capture
- `src/flagagent/model.ts` — `ScriptedModel` monotonic witness for deterministic tests
- `src/flagagent/tools.ts` — Executor async (unchanged)
- `tests/loop.test.ts` (8), `tests/deadline.test.ts` (9), `tests/transport.test.ts` (12), `tests/staging.test.ts` (5), `tests/e2e.test.ts` (1) — 74 total

# M2 — Source Staging, Docker, CLI, Integration
## Scope
Implement secure source snapshot/staging, Docker executor/network/lifecycle/recovery, CLI + challenge loading, npm package/bin behavior, relevant regressions, and a deterministic end-to-end harness smoke.

## Required semantics
Preserve:
- source symlink/TOCTOU protections
- executable bits
- run-scoped Docker ownership
- cleanup/recovery
- resource/network constraints
- fail-closed unsupported Docker topology
- model shell execution remains inside intended sandbox containment

Do not replace descriptor-relative/no-follow staging with a simpler path-based flow unless equivalent security is demonstrated.

## Decision
- **D014:** secure source staging — Linux procfd-anchored staging (fd + /proc/self/fd/<fd>/<name> + O_NOFOLLOW) preserving descriptor-relative TOCTOU protection; streaming opendir via procfd path, O_NOFOLLOW file open + fstat authority, incremental limits, exec-bits-only, deterministic sha256

## Required smoke
Exercise the assembled deterministic path:

```text
CLI/challenge
  → AgentLoop
  → DockerExecutor
  → shell
  → submit_flag
  → verifier
  → terminal result/artifacts
```

Use a scripted/fake provider.

## Gate (observed 2026-08-31, RC closure — local)
- [x] typecheck / lint / format:check / build PASS
- [x] ordinary tests PASS (115 tests: core/providers/loop/deadline/transport/staging/security/boundaries/cli/docker/e2e/adversarial)
- [x] real-Docker packed CLI integration PASS (local, non-root uid 1000, images flagagent-sandbox:dev/target:dev built)
- [x] package artifact: npm pack → clean install → bin smoke PASS
- [x] Docker lifecycle: bounded reap, descendant stdio, execution-budget probe, provenance version labels PASS
- [x] provider/deadline: semantic completion (built Response + state committed before deadline, late observation) preserved; incomplete/late rejected PASS
- [x] staging: symlink/special-file/TOCTOU boundaries, exec bits, digest framing, boundaries PASS
- [x] CLI descriptor/package: allowlist, symlink, protocol, env handling, installed bin PASS
- [x] no containment/security boundary weakened (non-root, cap-drop, no-new-privileges, bounded output)
- [ ] remote CI verification (requires push — not done in this task)

**M2 status:** corrections applied `78d03f6` (M1 corrective `94ef11f`); RC closure `b852a2c` + `1e8f025` adds semantic adversarial proof, bounded Docker lifecycle, staging boundaries, real-Docker packed CLI gate. Local: PASS, remote: pending.

## RC-2 local pre-cutover gate (2026-09-01, this head — local only)
- `npm ci` — PASS
- `npm run typecheck` — PASS
- `npm run lint` — PASS
- `npm run format:check` — PASS
- `npm run build` — PASS
- `npm test` (ordinary, `vitest.config.ts` excludes `real-docker*`) — 116 passed (14 files)
- `npm run test:real-docker` (`vitest.real-docker.config.ts`, `fileParallelism:false`) — 3 passed (none/local packed CLI + direct)
- `npm pack --dry-run` / `npm pack` + clean install + direct `flagagent` bin smoke — PASS
- CI: `actions/checkout@v7` + `actions/setup-node@v7`, `docker build -t flagagent-*:dev images/*` — local syntax PASS
- Docker images `flagagent-sandbox:dev` / `flagagent-target:dev` present, non-root execution (uid 1000) — PASS
- Provenance: `docker_engine`/`rootless`/`flagagent_version` keys + `sandboxLifecycle` `image_id`/`network_name` when applicable
- Sentinel flag isolation: initial provider request + `run.json` + workspace + Docker argv absent sentinel
- `git diff --check` — PASS
- Verdict: **LOCAL PRE-CUTOVER PASS — REMOTE CI NOT YET VERIFIED**

# Final — Deterministic Completion Gate
Do not remove Python before this gate.

The current commit/PR head must reproducibly pass:
- [ ] full TypeScript tests
- [ ] strict typecheck
- [ ] lint/format check
- [ ] production build
- [ ] npm/package/bin smoke
- [ ] Docker integration tests
- [ ] deterministic end-to-end smoke
- [ ] selected Python ↔ TypeScript parity
- [ ] applicable Python oracle checks
- [ ] `git diff --check`
- [ ] no unresolved compatibility/security deviation
- [ ] no unnecessary migration dependency/abstraction

Local Docker verification may be deferred when Docker is unavailable, but **v0.2.0 release readiness requires Docker integration to pass in a reproducible environment**.

After the gate:
- update `README.md` for Node/npm
- update architecture docs
- convert `AGENTS.md` from migration mode to permanent TypeScript instructions
- record user-facing breaking/install changes in v0.2.0 release notes
- remove Python production source/tests
- remove `pyproject.toml`, `uv.lock`, and Python-only tooling
- remove migration-only compatibility code
- transfer durable decisions to permanent docs
- remove `MIGRATION.md` before release unless intentionally repurposed as a user-facing upgrade guide

## Decisions / Deviations
Initial:
- **D001:** greenfield parallel rewrite
- **D002:** Python v0.1.1 remains runnable until final parity
- **D003:** no Python↔TypeScript migration bridge
- **D004:** module consolidation allowed; file-for-file parity not required
- **D005:** Node 24 LTS + TypeScript strict + ESM + npm
- **D006:** official OpenAI and Anthropic TypeScript SDKs
- **D007:** FlagAgent retains loop, verifier, deadline authority, artifacts, Docker lifecycle
- **D008:** Docker CLI remains default unless evidence justifies changing it
- **D009:** three milestones + final deterministic gate
- **D010:** tests migrate by behavior/subsystem, not 1:1
- **D011:** resolved M0 — manual validators (isRecord/snapshotJson, __proto__ safe)
- **D012:** resolved M0 — Vitest + ESLint flat + typescript-eslint + Prettier + TS 6.0.3
  - **D013:** resolved M1 corrective `94ef11f` — provider-stamped `lastCommittedAt` (captured inside fetch settlement after full parse + builtInput/thinkingHistory), loop `readCommittedAt()` + bounded 150ms drain, `committedAt < deadline`; shell double-check after `setExecutionDeadline`, verifier `flag_submission` gated before event; bounded prepare; no Worker needed
  - **D014:** resolved M2 — Linux procfd-anchored staging via fd + /proc/self/fd/<fd>/<name> + O_NOFOLLOW/O_DIRECTORY, streaming opendir via procfd, fstat-pinned authority, incremental limits, exec-bits-only, sha256 FLAGAGENT-SOURCE-V1; Docker async corrected, CLI descriptor validation corrected
- **D015:** resolved M0 — TS ESM `ES2023`/`NodeNext`/`NodeNext`, `strict`, `type: module`, Node 24.18
- **D016:** resolved M0 — provider budget `seconds→ms` + `maxRetries:0` vs unbudgeted defaults (openai 5.23.2, anthropic 0.67.1; signal split in M1)

**Material deviations/blockers:** none.

## Commit Policy
A completed milestone should normally end in one coherent green semantic commit. Use an extra commit only for a genuine reviewable semantic boundary. Avoid file-by-file or AI-edit micro-commits.

## Stop Conditions
Stop and report instead of improvising if:
- a security/reliability invariant would need to be weakened
- source-staging security cannot be preserved cleanly
- provider deadline semantics cannot be proven deterministically
- an unplanned framework/dependency appears necessary
- an inherited regression is contradictory or invalid
- a breaking artifact/CLI behavior change becomes necessary
- work begins expanding into unrelated v0.2 features

When uncertain: verify Python behavior, research the target API/runtime, choose the smallest reversible solution, and record only the decision that matters.
