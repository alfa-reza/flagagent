# FlagAgent Repository Instructions

FlagAgent v0.1.0 is a small, model-independent CTF agent harness for legal and authorized CTFs, security labs, benchmarks, and sandboxed experiments.

This is an operational guide for coding agents, not the architecture source of truth.

## Authority

Instruction precedence:

1. latest explicit human instruction;
2. `plans/Flagagent-v0.1.0.md`;
3. active approved milestone PRD under `plans/`;
4. this `AGENTS.md`;
5. implementation details.

Before substantial implementation, read the relevant Concept sections, active PRD, current code/tests, and Git state.

Do not silently change or reinterpret the frozen Concept/PRD. Report contradictions with evidence, impact, and the smallest proposed correction. Do not invent missing milestone requirements.

The human orchestrator is the final authority for architecture, scope, and releases.

## Workspace

```text
project-flagagent/
├── FlagAgent/              # writable product Git repository
└── donors/                 # read-only references
    ├── mini-swe-agent/
    ├── pi/
    ├── ctf-agent/
    └── nyuctf_agents/
```

Only `FlagAgent/` is writable by default.

Never edit, reformat, generate files in, commit in, reset, clean, or change branches inside `../donors/`. Never treat the parent workspace as one Git repository.

Inspect donors only for concrete questions. Prefer understanding a mechanism and implementing the smallest FlagAgent version over copying code.

`opencode.jsonc` and `.opencode/` are development-control configuration. Change them only when explicitly requested.

## v0.1.0 Scope

Work only on the assigned milestone and approved PRD.

Frozen baseline:

- one Run = one attempt;
- one active model and one `AgentLoop`;
- one linear conversation;
- one Agent container per real Run;
- one authoritative verifier;
- product tools only `shell` and `submit_flag`;
- fresh non-interactive process per `shell` call;
- JSON/JSONL run artifacts;
- no resume/checkpoint, planner/executor, product multi-agent, or PTY/session manager;
- one real provider/model path is sufficient for M2.

Do not add provider routers, generic plugin systems, RAG, databases, event buses, autonomous retry frameworks, browser agents, or generalized target frameworks unless required by the current PRD.

OpenCode subagents, plugins, and Skills are development aids, not FlagAgent product-runtime architecture.

## Critical Invariants

Preserve the Concept invariants:

- unknown/hallucinated product tools never execute;
- real model-generated commands and challenge workloads never intentionally execute directly on the host;
- control/provider/verifier secrets and Docker credentials stay outside Agent/Target containers;
- only the authoritative verifier establishes `solved`;
- provider-specific response objects stay behind the model boundary;
- model-visible execution is reconstructable from persisted evidence;
- persist the exact normalized/truncated tool result shown to the model;
- bound stdout/stderr while collecting it;
- non-zero command exit is normal evidence, not automatically `tool_error`;
- verifier `incorrect` is distinct from verifier infrastructure failure;
- preserve `solved`, `unsolved`, `error`, and no committed result;
- local targets and challenge-provided provisioning are untrusted by default;
- security relaxations are explicit, Run-scoped, and recorded;
- never silently broaden networking or fall back to Internet access.

Docker is the v0.1.0 containment baseline, not perfect isolation.

## Milestones

**M0 — Prove the Loop:** deterministic/fake model, executor, and verifier. Prove runtime semantics, ordering/correlation, verifier authority, limits, persistence, and terminal outcomes. Prefer standard-library runtime code where practical.

**M1 — Prove Containment:** replace fake execution with Docker CLI / Docker Engine without redesigning `AgentLoop`. Prove execution placement, process semantics, bounds, networking, security defaults, provenance, and cleanup. Do not add Compose or Docker SDK without PRD/evidence.

**M2 — Prove Usefulness:** add only one real provider/model path, one versioned/hashed solver prompt, and one frozen smoke set. Generalize provider variation only after a second provider proves the need.

Do not skip milestone gates.

## Engineering

Prefer the smallest explicit Python implementation that satisfies the current PRD and tests.

- abstract proven variation, not hypothetical variation;
- avoid speculative classes, registries, services, layers, and directories;
- avoid unrelated refactors;
- preserve deterministic ordering;
- update deterministic tests with behavior changes;
- prefer executable verification over LLM judgement.

Baseline:

```text
Python >= 3.12
uv + committed uv.lock
Hatchling
pytest + pytest-cov
Ruff targeting Python 3.12
```

Keep runtime dependencies empty until a current milestone needs one. When adding one, justify it, update `pyproject.toml`, update `uv.lock` through `uv`, and verify. Never manually edit `uv.lock`.

Common commands:

```bash
uv sync
uv lock --check
uv run pytest
uv run pytest --cov=flagagent --cov-report=term-missing
uv run ruff check .
uv run ruff format --check .
uv build
```

Run the smallest relevant check first; coverage/build are required only when relevant. Never claim a check passed unless its successful result was observed.

## OpenCode Subagents

Use the narrowest specialist that adds real value:

- `locator` — exact files/symbols/references/tests;
- `explore` — architecture/control/data flow;
- `scout` — official external docs/upstreams/APIs/changelogs;
- `diagnostician` — reproduce failures and establish root cause;
- `fixer` — bounded implementation after outcome/diagnosis is clear;
- `tester` — verification without editing;
- `reviewer` — correctness/regression/security/maintainability review;
- `critic` — adversarial plan/design/architecture review;
- `general` — only when no narrower specialist fits;
- `flagagent-gate` — final read-only Concept/PRD/milestone compliance.

Use subagents for specialization, independent review, parallel research, or context isolation—not ceremony.

Subagents do not own Git history. The primary agent integrates, verifies, commits, and pushes.

## Installed Skills and Plugins

**Superpowers:** use relevant skills for non-trivial design, planning, TDD, debugging, verification, review, or worktree isolation. Do not mechanically run the full workflow for trivial changes. Skills never override the human, Concept, PRD, or repository boundaries.

Use Git worktrees only when isolation/parallelism provides real value.

**Graphify:** use for difficult cross-file architecture/dependency tracing when normal search plus `locator`/`explore` is insufficient. Do not use it by default for simple lookups. Do not run Graphify install/uninstall, rewrite agent configuration, or intentionally commit generated Graphify output unless explicitly requested or later adopted by the repository.

## Donors and Licensing

No donor is copied wholesale.

```text
research
→ understand
→ compare with exact FlagAgent need
→ reuse concept when sufficient
→ selectively adapt only when justified
→ verify provenance/license
→ test FlagAgent behavior
```

Before source adaptation, establish exact upstream repository, commit SHA, source path/component, license/provenance, reuse mode, and FlagAgent destination.

v0.1.0 source reuse is MIT-only unless the human changes the policy. When donor source first enters FlagAgent, add/update `THIRD_PARTY_NOTICES.md`.

AGPL/incompatible projects such as BoxPwnr are research/concept references only under the current policy.

## Docker and Security

Do not weaken containment for convenience.

By default Agent/Target execution must not receive privileged mode, Docker socket access, host networking, unrelated writable host mounts, framework/provider/verifier secrets, extra Linux capabilities, or seccomp disablement.

Challenge-specific relaxations must be explicit, Run-scoped, and recorded.

Do not automatically run arbitrary challenge Dockerfiles, Compose files, Makefiles, scripts, or provisioning artifacts with host-level privileges.

Cleanup must target only resources belonging to the relevant Run. Never use broad cleanup such as `docker system prune` in normal development/tests.

## Git and GitHub

Git history is an engineering artifact. Keep it incremental, understandable, and recoverable.

The **primary agent may commit and push verified FlagAgent checkpoints without a separate instruction for every checkpoint**. Subagents remain non-committing.

Before work/checkpoints, inspect as appropriate:

```bash
git status
git branch --show-current
git diff
git log --oneline -n 10
```

Inspect `git remote -v` before the first push in a session when the remote is not already known.

Do not alter Git identity/signing, remotes, visibility, branch protection, or GitHub settings unless explicitly requested.

### Commits

Commit after a coherent, reviewable unit of work is complete and relevant verification passes.

A normal checkpoint should:

- represent one logical change;
- leave the repository usable;
- include related tests/docs;
- exclude unrelated changes;
- make sense independently from later commits.

Do not commit every edit or intentionally push broken/half-implemented work merely as a save point. Use WIP checkpoints only when explicitly requested.

Before committing:

```bash
git diff
git diff --staged
git status
git diff --check
```

Stage only files belonging to the checkpoint.

### Conventional Commits

Use Conventional Commits 1.0.0:

```text
<type>[optional scope]: <description>
```

Preferred types: `feat`, `fix`, `test`, `refactor`, `docs`, `build`, `ci`, `perf`, `chore`.

Examples:

```text
feat(loop): add ordered tool execution
test(verifier): cover incorrect flag handling
fix(docker): enforce command timeout
docs: document M1 containment evidence
build: update pytest development dependency
```

Use `!` or `BREAKING CHANGE:` only for a real breaking change. Do not invent issue numbers, authors, co-authors, signatures, or trailers.

### Verification and Push

Before a normal code checkpoint, run the relevant subset:

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
git diff --check
```

Also use `uv lock --check` for dependency changes and `uv build` for build/package changes.

Push after a meaningful verified checkpoint: a coherent task slice, milestone/PRD phase, validated risky change, session focus change, or explicit human request.

Push the current development branch to its configured remote; set upstream when needed.

Do not push donor repositories. Do not create tags/releases, merge PRs, or publish packages unless explicitly requested.

Never force-push or rewrite pushed history by default. Never run `git reset --hard`, `git clean -fd`, `git push --force`, or `git push --force-with-lease` unless explicitly requested after explaining the impact.

Prefer small meaningful commits over giant commits or noisy micro-commits. Keep secrets, local environments, generated noise, and machine-specific artifacts out of Git.

After a successful push, report the branch and latest commit SHA.

## Workflow

```text
read Concept + active PRD
→ inspect code/tests/Git
→ research concrete unknowns
→ bounded plan when needed
→ implement current scope
→ deterministic verification
→ inspect diff
→ reviewer/critic when useful
→ flagagent-gate
→ fix material findings
→ rerun affected checks
→ commit coherent checkpoint
→ push checkpoint
→ report evidence
```

`flagagent-gate` is additional evidence, not a replacement for executable tests, verifier results, or Docker observations.

Stop at the milestone gate.

## Definition of Done

A task is done when applicable requirements are satisfied, relevant deterministic checks pass, dependency state is consistent, security/verifier invariants remain intact, no donor/unrelated files changed, material review findings are resolved/reported, coherent work is committed with Conventional Commits, required checkpoints are pushed, and the handoff reports verification, commit SHA(s), pushed branch, and remaining uncertainty.

When uncertain, prefer evidence and the smallest reversible change over speculative architecture.
