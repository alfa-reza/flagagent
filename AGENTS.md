# FlagAgent Repository Instructions

FlagAgent v0.1.0 is a small, model-independent CTF agent harness for legal and authorized CTFs, security labs, benchmarks, and sandboxed experiments.

This file is the operational guide for coding agents. It is not the architecture source of truth or a milestone PRD.

## Authority

Instruction precedence:

1. latest explicit human instruction;
2. `plans/Flagagent-v0.1.0.md`;
3. active approved milestone PRD under `plans/`;
4. this `AGENTS.md`;
5. implementation details.

Before substantial implementation, read the relevant Concept sections, active PRD, current code/tests, and Git state.

Do not silently change the frozen Concept or reinterpret an approved PRD. Report contradictions with evidence, impact, and the smallest proposed correction.

Do not invent milestone requirements. The human orchestrator is the final authority for architecture, scope, milestone acceptance, and releases.

## Workspace

```text
project-flagagent/
├── FlagAgent/              # writable product Git repository
└── donors/                 # read-only references
    ├── ctf-agent/
    ├── mini-swe-agent/
    ├── nyuctf_agents/
    └── pi/
```

Only `FlagAgent/` is writable by default.

Never edit, reformat, generate files in, commit in, reset, clean, or change branches inside `../donors/`. Never treat the parent workspace as one Git repository.

Inspect donors only for concrete questions. Prefer understanding a mechanism and implementing the smallest FlagAgent version over copying source.

`opencode.jsonc` and `.opencode/` are development-control configuration. Change them only when explicitly requested.

## v0.1.0 Contract

Frozen baseline:

- one Run = one attempt;
- one active model and one `AgentLoop`;
- one linear conversation;
- one Agent container per real Run;
- one authoritative verifier;
- product tools only `shell` and `submit_flag`;
- fresh non-interactive process per real `shell` call;
- JSON/JSONL run artifacts;
- no resume/checkpoint, product planner/executor, product multi-agent, or PTY/session manager;
- one real provider/model path is sufficient for M2.

OpenCode subagents, plugins, and Skills are development aids, not FlagAgent product-runtime architecture.

Do not add provider routers, generic plugin systems, RAG, databases, event buses, autonomous retry frameworks, browser agents, or generalized target frameworks unless required by the active PRD.

## Current Phase — M2

M0 is the deterministic semantic baseline and M1 is the completed containment baseline. Preserve their tested behavior unless an approved requirement explicitly changes it.

M2 implementation begins only with the approved `PRD-M2`.

M2 adds only the real provider/model, minimal CLI, frozen smoke fixtures, prompt provenance, deterministic write-up, and release usability required by `PRD-M2`.

Do not add Docker Compose or the Docker Python SDK unless the approved PRD and evidence require them.

Do not proceed to post-v0.1.0 architecture during M2.

## Critical Invariants

Preserve these invariants:

- unknown/hallucinated product tools never execute;
- real model-generated commands and challenge workloads never intentionally execute directly on the host;
- provider/verifier/control secrets and Docker credentials stay outside Agent/Target containers;
- only the authoritative verifier establishes `solved`;
- provider-specific objects stay behind the model boundary;
- model-visible execution is reconstructable from persisted evidence;
- persist the exact normalized/truncated tool result shown to the model;
- bound stdout/stderr while collecting real command output;
- non-zero command exit is normal evidence, not automatically `tool_error`;
- verifier `incorrect` is distinct from verifier infrastructure failure;
- preserve `solved`, `unsolved`, `error`, and no committed result;
- local targets and challenge-provided provisioning are untrusted by default;
- security relaxations are explicit, Run-scoped, and recorded;
- never silently broaden networking or fall back to Internet access.

Docker is a containment baseline, not perfect isolation.

## M1 Docker Guardrails

Do not weaken containment for convenience.

By default Agent/Target execution must not receive:

- privileged mode;
- Docker socket access;
- host networking;
- unrelated writable host mounts;
- framework/provider/verifier secrets;
- extra Linux capabilities;
- seccomp disablement.

Use explicit CPU, memory, and PID limits as required by the PRD; do not assume Docker supplies useful resource limits by default.

Retain Docker's default security posture where the PRD does not explicitly require a relaxation. Any relaxation must be explicit, Run-scoped, and recorded.

Do not automatically execute arbitrary challenge Dockerfiles, Compose files, Makefiles, scripts, or provisioning artifacts with host-level privileges.

Do not claim stronger network or sandbox isolation than tests actually prove.

Cleanup must target only resources belonging to the relevant Run. Never use broad cleanup such as `docker system prune` in normal development/tests.

## Engineering

Prefer the smallest explicit Python implementation satisfying the current PRD and tests.

- abstract proven variation, not hypothetical variation;
- avoid speculative classes, registries, services, layers, and directories;
- avoid unrelated refactors;
- preserve deterministic ordering and established M0 semantics;
- update deterministic tests with behavior changes;
- prefer executable verification over LLM judgement;
- keep Docker-specific mechanics behind the smallest boundary needed to preserve `AgentLoop`;
- do not create a generic sandbox/backend framework for one backend.

Baseline:

```text
Python >= 3.12
uv + committed uv.lock
Hatchling
pytest + pytest-cov
Ruff targeting Python 3.12
Docker CLI / Docker Engine for M1
```

Keep runtime dependencies empty unless the active milestone has a concrete need.

When adding a dependency, justify it, update `pyproject.toml` and `uv.lock` through `uv`, and verify. Never manually edit `uv.lock`.

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

Run the smallest relevant check first, then broader applicable checks. Coverage/build are required only when relevant.

Never claim a command, test, Docker property, or containment behavior passed unless its result was actually observed.

## OpenCode Delegation

The primary Plan/Build agent owns orchestration, integration, final diff inspection, final verification, Git commits/pushes, and handoff.

For FlagAgent, **`general` is the preferred implementation subagent for substantive bounded coding slices**.

Each `general` task should:

- cover one coherent responsibility;
- name relevant PRD acceptance criteria when available;
- point to only likely relevant files/context;
- implement and run focused validation;
- never commit or push;
- return changed files, validation, assumptions, and remaining risks.

Prefer several coherent bounded tasks over giving one subagent an entire milestone. Do not delegate trivial edits merely to create activity.

Other subagents:

- `locator` — exact files/symbols/references/tests;
- `explore` — architecture, execution flow, dependencies;
- `scout` — official external docs/upstreams for concrete version-sensitive questions;
- `diagnostician` — reproduce non-obvious failures and establish root cause;
- `fixer` — narrow corrective implementation after diagnosis/scope is clear;
- `tester` — independent verification without editing;
- `reviewer` — correctness/regression/security/maintainability review;
- `critic` — adversarial plan/design review;
- `flagagent-gate` — final read-only Concept/PRD/milestone compliance.

Use subagents for real specialization, independent review, or context isolation—not ceremony.

Subagents do not own Git history.

## Skills and Plugins

**Superpowers:** use relevant skills when they materially help with non-trivial planning, TDD, debugging, verification, review, or worktree isolation. Do not run the full methodology mechanically for trivial changes.

**Graphify:** use only when difficult cross-file architecture/dependency tracing is not efficiently answered by normal search plus `locator`/`explore`.

Do not let plugins rewrite project configuration or commit generated artifacts unless explicitly requested.

Skills/plugins never override the human, Concept, approved PRD, or security boundaries.

## Donors and Licensing

No donor is copied wholesale.

```text
research
→ understand
→ compare with exact FlagAgent need
→ reuse concept when sufficient
→ selectively adapt source only when justified
→ verify provenance/license
→ test FlagAgent behavior
```

For M1, `ctf-agent` is useful for CTF container lifecycle/tooling lessons, but do not inherit permissive competition-oriented sandbox defaults.

Before adapting donor source, establish exact upstream repository, commit SHA, source path/component, license/provenance, reuse mode, and FlagAgent destination.

v0.1.0 source reuse is MIT-only unless the human explicitly changes the policy.

For actual source adaptation, update `THIRD_PARTY_NOTICES.md` and commit provenance before adaptation:

```text
chore(license): record <donor> provenance
```

Do not create provenance commits for concept-only learning or independent rewrites.

AGPL/incompatible projects such as BoxPwnr are research/concept references only.

## Git and GitHub

Git history is an engineering artifact. Keep it incremental and recoverable.

The primary agent may commit and push verified FlagAgent checkpoints without a separate instruction for every checkpoint. Subagents remain non-committing.

Before work/checkpoints:

```bash
git status
git branch --show-current
git diff
git log --oneline -n 10
```

Do not alter Git identity/signing, remotes, repository visibility, branch protection, or GitHub settings unless explicitly requested.

Commit after a coherent, reviewable unit is complete and relevant verification passes.

Use Conventional Commits 1.0.0:

```text
<type>[optional scope]: <description>
```

Preferred types: `feat`, `fix`, `test`, `refactor`, `docs`, `build`, `ci`, `perf`, `chore`.

Examples:

```text
feat(docker): add run-scoped agent container
test(docker): prove fresh exec semantics
fix(docker): bound command output collection
docs: record M1 containment evidence
chore(license): record ctf-agent provenance
```

Before committing:

```bash
git diff
git diff --staged
git status
git diff --check
```

Stage only files belonging to the checkpoint.

Push after meaningful verified checkpoints. Do not intentionally push broken/half-implemented state merely as a save point.

Do not push donor repositories. Do not create tags/releases, merge PRs, or publish packages unless explicitly requested.

Never run `git reset --hard`, `git clean -fd`, force-push, or rewrite pushed history unless explicitly requested after explaining the impact.

After a successful push, report the branch and latest commit SHA.

## M1 Workflow

```text
read Concept + approved PRD-M1
→ inspect M0 implementation/tests/Git
→ research only concrete unknowns
→ bounded plan
→ delegate substantive coding slices to general
→ primary integrates
→ targeted deterministic/Docker verification
→ tester
→ reviewer
→ flagagent-gate
→ fix material findings
→ rerun affected checks
→ commit/push coherent checkpoints
→ report evidence
→ STOP at M1 gate
```

`flagagent-gate` supplements deterministic evidence; it does not replace pytest, Docker inspection, resource observations, network probes, verifier results, or other executable checks.

Do not proceed to M2 merely because M1 appears complete.

## Definition of Done

A task is done when applicable requirements are satisfied, relevant deterministic/Docker checks pass, M0 semantics remain intact unless explicitly changed, containment claims have observed evidence, dependency state is consistent, no donor/unrelated files changed, material review findings are resolved/reported, coherent work is committed and pushed as required, and the handoff reports verification, Docker evidence, commit SHA(s), branch, and remaining uncertainty.

When uncertain, prefer evidence and the smallest reversible change over speculative architecture.
