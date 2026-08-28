# FlagAgent Repository Instructions

FlagAgent is a small, model-independent LLM agent harness for authorized CTFs, security labs, benchmarks, and sandboxed experiments.

These are durable repository instructions for coding agents. During the v0.2.0 rewrite, `MIGRATION.md` is the single temporary migration plan/tracker.

## Engineering Principles

Prefer the smallest correct design.

- **KISS:** choose the simplest implementation that satisfies the concrete requirement.
- **YAGNI:** do not add features, abstractions, dependencies, configuration, or extensibility for hypothetical future needs.
- **DRY with restraint:** remove meaningful duplication, but do not abstract small incidental repetition.
- Prefer explicit control flow, clear ownership, and deterministic verification.
- Read relevant code and tests before changing behavior.
- Avoid unrelated refactors while implementing focused work.
- Abstract proven variation, not anticipated variation.

Do not optimize for minimum LOC. Optimize for fewer concepts, states, synchronization mechanisms, dependencies, and failure paths.

## v0.2.0 TypeScript Migration

The v0.2.0 work is a **behavior-preserving TypeScript rewrite with selective implementation simplification**. It is not a line-by-line translation and not a feature release.

During migration:

- Python v0.1.1 source and tests are the temporary behavioral/regression oracle.
- Keep the Python implementation and tests runnable and unchanged until final parity, unless an explicit task authorizes a baseline fix.
- Preserve behavior, reliability, security invariants, artifact semantics, and trust boundaries; do not preserve Python-specific mechanisms merely for symmetry.
- Do not preserve file boundaries mechanically. Consolidate modules when responsibilities naturally belong together and the result is simpler.
- Do not add unrelated v0.2 features such as multi-agent orchestration, new provider routing, new sandbox backends, resume/checkpoint, databases, web UI, generic plugins, or MCP integration as a FlagAgent product feature.
- Port/rewrite tests that encode behavior, regressions, or security. Retire only tests that are strictly Python-implementation-specific and whose required behavior is covered elsewhere.
- Never weaken tests merely to make the TypeScript rewrite pass.
- Follow the approved milestone in `MIGRATION.md`, verify it, update the tracker, then **stop**. Do not begin the next milestone unless explicitly instructed.
- When explicitly asked for the planning phase, research and produce/update the concise migration plan only; do not start implementation.

If a target-language design cannot preserve an important invariant cleanly, stop and report the conflict and options instead of silently weakening it.

For current behavior, inspect `src/flagagent/`, legacy tests, `README.md`, and `docs/design/architecture-v0.1.0.md`. If they disagree, investigate rather than guessing or silently rewriting the oracle.

## Target Direction

Unless an approved `MIGRATION.md` decision states otherwise:

- target Node.js 24 LTS;
- use TypeScript with `strict` enabled;
- use npm with a committed lockfile;
- use the official `openai` and `@anthropic-ai/sdk` packages;
- keep a small custom FlagAgent runtime rather than adopting an agent framework.

Use official SDK types/helpers instead of recreating provider API models by hand when the SDK behavior is suitable.

SDKs may own HTTP transport, provider types, parsing helpers, streaming primitives, and cooperative cancellation. FlagAgent must continue to own:

- agent-loop orchestration;
- normalized provider behavior;
- tool allowlisting and execution order;
- `shell` and `submit_flag` semantics;
- verifier authority;
- absolute Run deadline semantics;
- run artifacts and terminal status;
- Docker sandbox lifecycle.

Do not delegate core orchestration to provider convenience loops that automatically execute tools when that would bypass FlagAgent's verifier, deadline, audit, or Docker boundaries.

TypeScript types are not runtime validation. Validate untrusted challenge input, model/tool payloads, configuration, and persisted external data at runtime where required.

### Module Design

Do not port one Python file into one TypeScript file by default.

- Consolidate provider code when one cohesive module is clearer; OpenAI Chat and Responses may share an OpenAI provider module.
- Artifact persistence and derived write-up rendering may share a module if cohesion remains clear.
- Keep materially different responsibilities separate. Agent orchestration, provider supervision, Docker execution, and secure source staging must not be merged solely to reduce file count.
- Avoid dependency-injection frameworks, event buses, generic registries, middleware/plugin frameworks, and speculative factory layers.
- Prefer Node built-ins over new dependencies when they solve the requirement safely and clearly.

## Research Before Assumption

Do not implement version-sensitive external behavior from memory when it can be verified.

### Context7 MCP — Required

**Use Context7 MCP before implementing or changing code that depends on a third-party library, SDK, API, CLI, framework, or configuration format.** Use it even when the API appears familiar.

Typical migration examples: OpenAI SDK, Anthropic SDK, TypeScript configuration, Vitest/test tooling, runtime schema validation, and package/build tooling.

When using Context7:

1. identify the actual package and relevant version/version range;
2. query the specific API or behavior being changed;
3. prefer current retrieved documentation over remembered examples;
4. do not invent APIs absent from the retrieved documentation.

If Context7 is unavailable or insufficient, use official documentation/source and state that limitation in the task report.

Do not use Context7 mechanically for pure internal refactors, renames, business logic, or code review with no external API dependency.

### Exa MCP — Optional but Useful

Use Exa when broader research materially reduces uncertainty, especially for upstream issues/PRs, known runtime/SDK bugs, Node filesystem/subprocess/stream/cancellation behavior, Docker/security constraints, or migration precedents.

Prefer primary sources found through Exa: official docs, official repositories/source, release notes, and upstream issue/PR discussions. Treat community content as supporting evidence.

Do not use Exa merely to increase activity.

## RTK (Rust Token Killer)

RTK is developer tooling for reducing noisy shell output reaching the coding agent. It is **not** a FlagAgent runtime dependency and must not be added to project dependencies.

When no automatic RTK integration is available in the coding client, follow these prompt-level rules when RTK is installed.

Prefer RTK for supported high-output commands when compact output is sufficient, for example:

```bash
rtk git status
rtk git diff
rtk git log
rtk tsc
rtk vitest
rtk lint
rtk npm <args>
rtk test "uv run pytest"
rtk docker ps
rtk docker logs <container>
```

For chained supported commands, apply RTK to each command when practical.

Use raw commands when RTK is unavailable, unsupported for the required command shape, exact output is required, filtered output hides necessary diagnostics, or the command is already trivial.

If RTK changes or obscures behavior, rerun the original command. Correctness takes priority over token savings. Do not install or reconfigure RTK unless explicitly requested.

## Verification During Migration

Never claim a command, test, build, security property, Docker behavior, or parity check passed unless it was actually observed.

Run the smallest relevant checks first, then broader milestone checks.

Legacy Python checks currently include:

```bash
uv sync
uv lock --check
uv run pytest
uv run pytest -m docker
uv run ruff check .
uv run ruff format --check .
uv build
git diff --check
```

Once TypeScript scripts exist, keep the target verification surface simple and conventional: typecheck, tests, lint/format check, build/package check, and Docker-backed integration tests where applicable.

At each milestone boundary:

1. run relevant target checks and migrated regressions;
2. confirm the legacy oracle still runs when applicable;
3. inspect the diff for unnecessary abstractions, dependencies, dead code, and weakened assertions;
4. update only material status/decisions/blockers in `MIGRATION.md`;
5. stop before the next milestone.

At final parity, run the full target suite, typecheck, lint, build/package smoke checks, Docker integration tests, and the approved representative Python-vs-TypeScript parity scenarios.

Do not delete Python source/tests until final parity and security/reliability gates pass.

## Critical Invariants

Preserve the behavioral identity captured by the current architecture and regression suite.

### Tools, Verifier, and Secrets

- Unknown or malformed model tool requests must never execute.
- Model-generated challenge commands must not intentionally execute directly on the host.
- Only the authoritative verifier may establish `solved`.
- Incorrect flag submissions and non-zero shell exits are execution evidence, not automatically harness errors.
- Provider credentials, verifier secrets, and the expected flag remain outside Agent/Target containers.
- Do not silently broaden networking, mounts, capabilities, devices, seccomp posture, Docker socket access, or host access.

Docker is a practical containment baseline, not a VM/microVM-equivalent boundary.

### Run Deadline and Provider Supervision

Preserve the regression semantics represented by Issues #47, #54, and #56:

- one absolute Run wall deadline is authoritative;
- blocking provider/execution work must not reset or steal that deadline;
- no model-requested tool executes after the deadline wins;
- provider evidence demonstrably completed before the deadline is not lost merely because the parent/event loop observes it later;
- completion after the deadline is not treated as pre-deadline success;
- large provider responses must not create transport deadlock/circular backpressure.

Do not assume `AbortSignal`, `Promise.race()`, or SDK timeouts alone are equivalent to these guarantees. Use them where appropriate, but prove semantics with deterministic tests.

Do not port Python multiprocessing/pipes/threads/commit-marker mechanics literally unless needed. Preserve the invariant, not the Python mechanism.

### Source Staging

The current Python implementation uses descriptor-relative/no-follow filesystem techniques to reduce symlink/TOCTOU risk. Do not replace them with a simpler path-based `lstat -> copy` flow unless equivalent security has been demonstrated.

When Node lacks an exact primitive, research the runtime/OS behavior and choose the smallest design that preserves the security property. Preserve executable-bit behavior covered by tests.

### Docker Lifecycle

Preserve run-scoped ownership, bounded operations, cleanup/recovery semantics, resource limits, supported networking, and fail-closed handling for unsupported/uncertain Docker topology.

Do not simplify Docker lifecycle code solely to reduce LOC; much of its complexity represents previously discovered failure modes.

### Artifacts

Preserve the semantic roles of `run.json`, `events.jsonl`, `result.json`, workspace artifacts, and derived `writeup.md` where retained. `result.json` remains the authoritative committed terminal outcome.

## Dependencies and Documentation

Add dependencies only for concrete requirements. Before adding one, verify current usage through Context7, check maintenance/license/runtime cost, and confirm it does not duplicate Node built-ins or an official SDK capability.

Do not add an agent framework, provider framework, Docker SDK, generic state-machine framework, or plugin system during v0.2 unless explicitly approved for a demonstrated need.

Keep implementation, tests, architecture docs, and user-facing docs consistent.

`MIGRATION.md` is the **single temporary migration tracker** for this small repository. Keep it concise: goal/non-goals, approved decisions, critical invariants, milestone status, material deviations/blockers, and final parity status. Do not turn it into a session diary, prompt transcript, exhaustive test matrix, token log, or enterprise migration database.

After the migration, move durable information to `AGENTS.md`, `README.md`, release notes, or architecture docs as appropriate, then remove the temporary `MIGRATION.md` before release unless it has become a genuine user-facing upgrade guide.

## Third-Party Code

Treat external repositories as references unless source reuse is explicitly justified. Before copying/adapting source, verify provenance, license compatibility, attribution requirements, and whether a smaller original implementation would be clearer.

Do not copy code merely because another AI migration used it.

## Git and Commits

Keep changes focused and reviewable.

- Use Conventional Commits when creating commits.
- Prefer one coherent, green semantic commit per migration milestone by default; avoid micro-commits for every generated file/edit.
- Keep final parity/cleanup as a separate coherent commit when practical.
- Do not push, merge, tag releases, change remotes/Git identity/settings, or rewrite history unless explicitly requested.
- Do not run destructive operations such as `git reset --hard`, `git clean -fd`, or force-pushes without explicit authorization.

## Definition of Done

A migration milestone is complete only when the approved scope is implemented, relevant deterministic tests/checks pass, required behavior/security invariants remain covered, the legacy oracle remains runnable when applicable, dependencies/lockfiles are consistent, `MIGRATION.md` records material state, and the diff contains no unrelated feature work or unjustified abstractions.

The agent must stop before starting the next milestone unless explicitly instructed.

The v0.2 migration is complete only after representative cross-language parity, Docker/security integration verification, final documentation updates, and deliberate removal of obsolete Python/migration-only material.

When uncertain: research first, preserve the invariant, and choose the smallest reversible implementation.
