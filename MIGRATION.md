# FlagAgent v0.2.0 TypeScript Migration

> Temporary execution plan for the Python v0.1.1 → TypeScript v0.2.0 rewrite.
> `AGENTS.md` is authoritative for durable engineering, research, security, Git, and verification rules.

## Status
- **Source:** `v0.1.1`
- **Target:** `v0.2.0`
- **Strategy:** greenfield parallel rewrite
- **Current milestone:** not started
- **Blockers:** none
- [ ] M0 — Foundation, core, artifacts, providers
- [ ] M1 — AgentLoop and provider supervision
- [ ] M2 — Source staging, Docker, CLI, integration
- [ ] Final — deterministic parity/release gate and cleanup

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

### Baseline gate
Record observed results before TypeScript implementation:
- [ ] `uv sync`
- [ ] `uv lock --check`
- [ ] `uv run pytest`
- [ ] `uv run ruff check .`
- [ ] `uv run ruff format --check .`
- [ ] `uv build`
- [ ] `git diff --check`
- [ ] Docker-backed tests when Docker is available

**Inherited baseline failures:** none recorded.

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
- **D011:** runtime validation — manual validators or one schema library
- **D012:** minimum test/lint/format toolchain

## Gate
- [ ] strict typecheck passes
- [ ] M0 tests pass
- [ ] build passes
- [ ] lint/format check passes
- [ ] minimal CI is green
- [ ] SDK usage verified against current documentation
- [ ] no unnecessary runtime dependency/framework added
- [ ] Python oracle remains runnable
- [ ] diff reviewed for KISS/YAGNI

**M0 status:** pending

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

## Gate
- [ ] AgentLoop tests pass
- [ ] #47 regressions pass
- [ ] #54 regressions pass
- [ ] #56 / large-response regressions pass
- [ ] deadline/completion semantics are deterministic
- [ ] typecheck/build/lint pass
- [ ] CI remains green
- [ ] Python oracle remains runnable
- [ ] synchronization/state reviewed for unnecessary complexity

**M1 status:** pending

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
- **D014:** secure source staging — choose the smallest Node/OS design preserving the current security property; stop and present options if no clean equivalent exists

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

## Gate
- [ ] source/executable-bit regressions pass
- [ ] Docker executor/network/lifecycle/recovery regressions pass
- [ ] CLI tests pass
- [ ] end-to-end smoke passes
- [ ] typecheck/build/lint pass
- [ ] CI remains green
- [ ] Python oracle remains runnable
- [ ] no containment/security boundary weakened
- [ ] dead/unneeded compatibility code reviewed

**M2 status:** pending

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
- **D011:** pending M0 — runtime validation approach
- **D012:** pending M0 — test/lint/format tooling
- **D013:** pending M1 — provider-supervision mechanism
- **D014:** pending M2 — secure source-staging mechanism

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
