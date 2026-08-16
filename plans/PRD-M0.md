# FlagAgent v0.1.0 — PRD M0: Prove the Loop

> **Status:** DONE  
> **Release:** v0.1.0 
> **Milestone:** M0 — Prove the Loop  
> **Document role:** Product / behavioral requirements for M0 implementation  
> **Architecture source of truth:** `plans/Flagagent-v0.1.0.md`  
> **Operational agent guidance:** `AGENTS.md`  
> **Primary implementation mode:** deterministic, local, no real provider, no Docker  
> **Human authority:** final product, architecture, scope, and approval decisions remain with the human orchestrator

---

## 0. Document Contract

This PRD translates the frozen FlagAgent v0.1.0 Concept into exact, testable requirements for **M0 only**.

It defines:

- the problem M0 must solve;
- the observable behavior M0 must provide;
- the scope and non-scope;
- the minimum internal contracts that later milestones depend on;
- deterministic acceptance criteria;
- donor-research and source-reuse rules;
- the evidence required to declare M0 complete.

This PRD intentionally does **not** prescribe a detailed file layout, class hierarchy, design pattern, or implementation plan unless a behavioral requirement depends on it.

Instruction precedence remains:

```text
latest explicit human instruction
        ↓
plans/Flagagent-v0.1.0.md
        ↓
this approved PRD-M0
        ↓
AGENTS.md
        ↓
implementation details
```

If this PRD appears to conflict with the frozen Concept, implementation MUST stop and report the conflict rather than silently choosing one interpretation.

---

# 1. Product Lineage and Donor Intent

FlagAgent is a **from-scratch reimplementation in the lineage of `verialabs/ctf-agent`**.

It is not a Git fork and MUST NOT copy the `ctf-agent` source tree wholesale.

The intended relationship is:

```text
ctf-agent
    │
    │ CTF product lineage, operational lessons,
    │ selected mechanisms/components where justified
    ▼
FlagAgent
    │
    │ clean architecture rebuilt from first principles
    │ around the frozen v0.1.0 Concept
    ▼
small, observable single-agent baseline
```

`ctf-agent` is the **primary CTF product donor/reference**.

For M0 specifically:

- `ctf-agent` is inspected for solver lifecycle, tracing/output concepts, CTF-oriented behavior, and reusable small mechanisms;
- `mini-swe-agent` is the strongest reference for a minimal linear agent loop and independent command execution;
- `pi` is the strongest reference for model/tool boundaries, assistant-before-result ordering, call/result correlation, and sequential tool execution;
- `nyuctf_agents` is inspected for its CTF baseline and for lessons separating a simple baseline from planner/executor complexity.

The existence of an upstream mechanism does not automatically justify adapting its source.

Default decision order:

```text
understand
→ compare to exact M0 requirement
→ reuse concept if sufficient
→ write the smallest FlagAgent implementation
→ selectively adapt source only when materially better
```

---

# 2. Problem Statement

Before FlagAgent can prove Docker containment or real-model usefulness, it needs a deterministic core whose behavior is unambiguous.

Without M0, failures observed later could be caused by model/provider behavior, tool ordering mistakes, verifier-authority mistakes, persistence bugs, timeout semantics, malformed model calls, framework failures, or Docker behavior.

The problem M0 solves is:

> **Can FlagAgent deterministically execute a small single-agent conversation loop, process only allowed tool calls in defined order, distinguish model/tool/verifier/framework outcomes correctly, persist an auditable trajectory, and declare success only after verifier confirmation?**

---

# 3. Outcome

M0 is successful when the same scripted inputs always produce the same observable runtime behavior and terminal result.

The milestone MUST establish a trustworthy baseline for M1 without requiring:

- a real LLM;
- provider credentials;
- Docker;
- network access;
- a real CTF target;
- a generalized challenge framework.

After M0 passes, replacing the fake command executor with Docker in M1 MUST NOT require redesigning the AgentLoop semantics.

---

# 4. User and Usage Context

The primary v0.1.0 user is the project maintainer.

The repository SHOULD also remain clonable and understandable by other developers.

For M0:

- source/development installation is sufficient;
- `uv` remains the project environment/package workflow;
- publishing to PyPI, npm, or another package registry is not an M0 requirement;
- a polished public CLI is not an M0 requirement;
- the future executable name is reserved as `flagagent`, but CLI syntax is deferred.

M0 behavior MAY be exercised entirely through Python APIs and `pytest`.

---

# 5. M0 Scope

M0 MUST implement only the runtime pieces necessary to prove deterministic loop semantics.

Required conceptual components:

```text
Run
AgentLoop
Model boundary
Scripted/Fake Model
Tool call normalization
Fake shell executor
Verifier boundary
Fake/exact verifier
Persistence
Terminal result handling
Limits
Deterministic tests
```

Product tool names are frozen:

```text
shell
submit_flag
```

---

# 6. Explicit M0 Non-Goals

M0 MUST NOT introduce the following merely for completeness:

- Docker execution;
- Docker SDK;
- Docker Compose;
- real provider SDK;
- real LLM calls;
- provider router/registry/fallback;
- generic retry framework;
- multi-agent orchestration;
- planner/executor;
- model racing;
- PTY or persistent shell/session manager;
- interactive stdin or process handles;
- background job API;
- browser tooling;
- CTFd integration;
- MCP as FlagAgent runtime architecture;
- persistent memory or RAG;
- database or event bus;
- resume/checkpoint;
- generalized target framework;
- generalized plugin framework;
- CLI framework solely for appearance;
- JSON/YAML DSL for scripted-model fixtures;
- coverage-percentage gate.

---

# 7. Run Identity and Directory Contract

## 7.1 Run ID

A Run ID MUST be generated automatically.

Format:

```text
FA-<UTC_BASIC_TIMESTAMP>-<8 lowercase hex characters>
```

Example:

```text
FA-20260814T161530Z-a13f4c2d
```

Requirements:

- UTC timestamp;
- random suffix generated using Python standard-library functionality;
- no external ID dependency;
- user-selected Run IDs are not required in M0;
- an existing Run directory with the generated ID MUST NOT be overwritten.

## 7.2 Default Run root

Default artifact root:

```text
runs/
```

Per-Run structure:

```text
runs/
└── <run-id>/
    ├── run.json
    ├── events.jsonl
    ├── result.json
    └── workspace/
```

`result.json` exists only after a terminal result is successfully committed.

## 7.3 Workspace retention

The M0 workspace remains after terminal completion.

M0 does not implement automatic workspace deletion.

---

# 8. Challenge Input Contract

M0 uses a passive challenge input only.

Minimum logical inputs:

```text
challenge path or identity
challenge description
```

M0 MUST NOT create lifecycle-heavy abstractions such as `ChallengeProvider`, `TargetProvider`, `TargetRegistry`, `provision()`, `lease()`, or `release()`.

The Run creates its own workspace directory.

Real challenge copy/mount semantics, symlink policy, target provisioning, recursive challenge hashing, and container exposure are deferred to M1 unless an M0 persistence test needs a minimal deterministic fixture.

---

# 9. Normalized Model Contract

Core runtime MUST depend on a small normalized model boundary, not provider-specific response objects.

Conceptual operation:

```text
Model.generate(messages, tools) -> ModelResponse
```

Minimum normalized response information:

```text
content
tool_calls
usage
```

Each normalized tool call contains:

```text
call_id
name
arguments
```

Requirements:

- `content` MAY be empty when tool calls are present;
- `content` and tool calls MAY coexist;
- `usage` MAY be absent/`None`;
- core runtime MUST NOT require a real provider-specific type.

---

# 10. Scripted Model

M0 uses an in-memory scripted model.

The scripted model SHOULD be representable as a Python sequence of normalized responses.

Do not introduce a separate JSON/YAML fixture language.

It MUST be possible to script:

- a content-only response;
- one tool call;
- multiple ordered tool calls in one response;
- wrong then correct flag submissions;
- an unknown tool;
- invalid arguments;
- a model/provider failure;
- duplicate `call_id` input for negative testing.

A model turn is exactly one invocation of `Model.generate(...)`.

---

# 11. Tool Surface and Validation

The only product-level tools exposed by M0 are `shell` and `submit_flag`.

Tool dispatch MUST use an allowlist.

Unknown tool names MUST NOT execute.

## 11.1 Unknown tool

An unknown tool request is recoverable model behavior:

```text
unknown tool request
→ do not execute anything
→ create correlated model-visible error result
→ persist evidence
→ continue the loop if limits allow
```

Recommended normalized error type:

```text
unknown_tool
```

Exact human-readable wording is not a compatibility contract.

## 11.2 Invalid arguments

A known tool with invalid arguments MUST NOT execute.

It is also recoverable model behavior:

```text
invalid tool arguments
→ do not execute
→ correlated model-visible invalid_arguments result
→ persist evidence
→ continue if limits allow
```

Recommended normalized error type:

```text
invalid_arguments
```

## 11.3 Duplicate call IDs

`call_id` values within the active normalized conversation MUST be unambiguous.

A duplicate `call_id` in a model response is a malformed model-boundary response.

Required terminal classification:

```text
status = error
reason = provider_error
```

For M0 the scripted model represents the provider/model boundary, so this classification is used even though no external provider is involved.

---

# 12. AgentLoop Behavioral Contract

## 12.1 Core order

```text
check remaining Run wall time
        ↓
Model.generate(messages, tools)
        ↓
persist/append assistant response to conversation
        ↓
tool calls?
   ┌────┴────┐
   no        yes
   │          │
model_stop    execute sequentially
              │
              ├── shell
              └── submit_flag
              │
       append correlated result
              │
     correct flag submitted?
        ┌─────┴─────┐
       yes          no
        │            │
      solved       continue
```

## 12.2 Assistant-before-result invariant

The assistant/model response that requested a tool MUST enter conversation state before any result for that tool enters conversation state.

Tests MUST verify this ordering.

## 12.3 Multiple tool calls

When one model response requests multiple tools:

- execute in normalized model-declared order;
- preserve each `call_id`;
- append results in the same logical order;
- count the entire model response as one model turn.

M0 baseline execution is sequential, not parallel.

## 12.4 Verified solve short-circuit

If `submit_flag` returns `correct`:

```text
status = solved
reason = verified_flag
```

The Run terminates immediately.

Any later tool calls from the same model response MUST NOT execute.

## 12.5 Wrong flag behavior

If `submit_flag` returns `incorrect`:

- the Run remains active;
- later calls from the same response continue in order;
- the next model turn remains possible if limits allow.

## 12.6 Model stop

A normal model response with no tool calls terminates:

```text
status = unsolved
reason = model_stop
```

M0 MUST NOT automatically reprompt a model that has normally stopped.

---

# 13. `shell` Contract

M0 freezes the product-facing shell contract even though execution is fake.

Minimum call:

```text
shell(command: str)
```

No M0 arguments for `cwd`, `env`, `stdin`, session, PTY, background execution, or process handles.

Whitespace-only/empty command is invalid tool arguments and MUST NOT execute.

The real M1 shell baseline will use a fresh non-interactive process for each invocation. M0 MUST NOT build behavior that assumes a persistent shell session.

Minimum normalized successful/failing command result:

```json
{
  "stdout": "...",
  "stderr": "...",
  "exit_code": 0,
  "timed_out": false,
  "truncated": false
}
```

A timed-out command uses:

```json
{
  "exit_code": null,
  "timed_out": true
}
```

A non-zero `exit_code` is normal tool evidence and MUST NOT automatically become framework `tool_error`.

A failure of the executor itself to fulfill a valid call terminates:

```text
status = error
reason = tool_error
```

M0 uses a deterministic fake executor capable of returning each relevant outcome.

---

# 14. Tool Output Normalization

Initial defaults:

```text
max_model_tool_output per stdout  = 16 KiB
max_model_tool_output per stderr  = 16 KiB
max_logged_tool_output per stdout = 64 KiB
max_logged_tool_output per stderr = 64 KiB
```

Requirement:

```text
max_logged_tool_output >= max_model_tool_output
```

## 14.1 Truncation behavior

When output exceeds the model-visible limit, normalization MUST preserve useful beginning and ending context.

Baseline strategy:

```text
head
+ explicit truncation marker
+ tail
```

The result MUST clearly indicate `truncated = true`.

The exact normalized/truncated result returned to the model MUST be the result persisted as model-visible evidence.

M0 MUST test normalization/truncation deterministically.

**M0 does not claim bounded subprocess collection**, because no real subprocess is used. M1 is responsible for proving that stdout/stderr collection itself is bounded before large output can exhaust host memory.

---

# 15. `submit_flag` and Verifier Contract

Minimum tool:

```text
submit_flag(candidate: str)
```

The verifier is authoritative.

M0 uses a simple exact-string verifier.

Normalization:

```text
candidate = candidate.strip()
```

Comparison is case-sensitive.

Example:

```text
expected:  Flag{example_123}
candidate: "  Flag{example_123}\n"
result:    correct
```

But `flag{example_123}` is not equal to `Flag{example_123}`.

Required verifier outcomes:

```text
correct
incorrect
```

`incorrect` is a normal candidate result.

Verifier infrastructure failure terminates:

```text
status = error
reason = verifier_error
```

M0 MUST NOT implement regex flag discovery, flag-prefix heuristics, confidence scoring, scraping final model text for flags, or automatic success from stdout.

Only a successful `submit_flag` verifier result establishes `solved`.

There is no separate wrong-submission budget in M0.

Submitted candidates MAY be persisted as trajectory evidence.

The expected flag/verifier secret MUST NOT be placed into model-visible messages, workspace content, or tool results.

---

# 16. Error Taxonomy

M0 distinguishes recoverable model mistakes from framework failures.

## 16.1 Recoverable model-visible mistakes

These MUST NOT terminate the Run by themselves:

```text
unknown_tool
invalid_arguments
incorrect flag
non-zero shell exit
shell timed_out result
```

## 16.2 Terminal framework/model-boundary failures

Initial terminal error reasons:

```text
provider_error
tool_error
verifier_error
serialization_error
```

Examples:

- scripted/model boundary raises or returns malformed normalized response → `provider_error`;
- valid tool cannot be fulfilled by executor → `tool_error`;
- verifier cannot perform its check → `verifier_error`;
- a serializable terminal error can be committed after another serialization failure → `serialization_error`.

No generic FlagAgent retry framework exists in M0.

Provider-SDK retry policy is explicitly deferred to M2.

---

# 17. Limits

Initial defaults:

```text
max_model_turns         = 100
wall_timeout_seconds    = 1800
command_timeout_seconds = 60
```

All configured limits MUST be positive.

M0 tests SHOULD override defaults with small deterministic values.

## 17.1 Model-turn semantics

One model turn = one `Model.generate(...)` invocation.

Before each model invocation, the runtime checks whether another model call is permitted.

Exactly `max_model_turns` calls MAY occur.

If the final allowed model response requests tools:

- those tool calls MAY complete normally;
- if they do not solve/terminate the Run, no additional model call is permitted;
- terminal result becomes `unsolved/model_turn_limit`.

## 17.2 Wall time

Elapsed-time enforcement MUST use a monotonic clock abstraction.

M0 SHOULD make time injectable/fakeable for deterministic tests.

The solving wall budget covers model calls, tool execution, verifier calls, and active loop orchestration.

If wall time is exhausted before a new operation, do not begin that operation.

If wall time becomes exhausted during an operation, classify the Run as `unsolved/wall_limit` once control returns and terminal handling can occur.

Minimal terminal bookkeeping MAY run after the wall limit is detected.

## 17.3 Effective command timeout

The effective command budget MUST NOT exceed the remaining Run wall budget.

M0 tests the calculation/semantics using the fake executor.

---

# 18. Persistence Contract

M0 freezes the semantic roles of three artifacts.

## 18.1 `run.json`

`run.json` is an immutable Run configuration/provenance snapshot.

Minimum fields:

```text
schema_version
run_id
flagagent_version
concept_version
challenge
started_at
limits
```

Minimum versions:

```text
schema_version = 1
flagagent_version = "0.1.0"
concept_version = "0.1.0"
```

`started_at` uses UTC ISO-8601.

The exact nested JSON shape MAY be chosen for clarity, but tests MUST freeze the adopted schema once implemented.

No secrets may be serialized.

## 18.2 `events.jsonl`

`events.jsonl` is the ordered observable trajectory.

Each committed event MUST include:

```text
schema_version
seq
timestamp
type
payload
```

`seq` is an integer, monotonically increasing, and SHOULD start at `1`.

Timestamps use UTC ISO-8601.

Minimum event categories:

```text
model_response
tool_call
tool_result
flag_submission
verifier_result
error
```

Events MUST preserve normalized model response content, requested tool calls, executed/non-executed result evidence, call/result correlation, submitted flag candidates, verifier decisions, and framework errors.

Provider-private hidden reasoning and raw provider SDK payloads are out of scope.

## 18.3 `result.json`

`result.json` is the committed terminal outcome.

Minimum fields:

```text
schema_version
run_id
status
reason
finished_at
duration_seconds
model_calls
tool_calls
flag_submissions
```

Committed statuses:

```text
solved
unsolved
error
```

Initial reasons:

```text
verified_flag
model_stop
model_turn_limit
wall_limit
provider_error
tool_error
verifier_error
serialization_error
```

`result.json` MUST NOT be treated as committed until its atomic replacement succeeds.

---

# 19. Atomic Write and Crash Semantics

## 19.1 `run.json`

Required pattern:

```text
serialize
→ temporary file in same directory/filesystem
→ flush and close
→ os.replace(...)
```

## 19.2 `result.json`

Required pattern:

```text
serialize
→ result temporary file in same directory/filesystem
→ flush and close
→ os.replace(...)
```

M0 does not require an `fsync` durability guarantee.

Do not document stronger crash durability than is actually implemented.

## 19.3 `events.jsonl`

Each event is one complete JSON object on one line.

After writing an event, flush the Python file buffer. No fsync requirement.

A reader after interruption:

- accepts all complete lines;
- may ignore/reject at most one trailing incomplete line;
- MUST NOT interpret a partial line as a committed event.

M0 MUST contain a deterministic test for this behavior.

## 19.4 No committed result

Process interruption before terminal commit or `result.json` serialization/write/replace failure produces no valid committed terminal result.

```text
no valid result.json
= no committed terminal result
```

No recovery subsystem is required.

---

# 20. Determinism Requirements

Given the same scripted model sequence, fake executor, fake verifier, injected clock values, and explicit Run ID/time fixtures where needed, M0 tests MUST observe the same:

- model/tool ordering;
- terminal status and reason;
- call correlations;
- normalized tool results;
- event sequence semantics;
- result counters.

Wall-clock-generated IDs/timestamps MAY differ in ordinary runtime and SHOULD be injected/fixed in tests where needed.

Tests SHOULD compare parsed semantic JSON values rather than relying on JSON object key ordering.

---

# 21. Donor Reconnaissance Requirement

Before implementing M0, the coding agent MUST inspect the local configured donor checkouts:

```text
../donors/ctf-agent
../donors/mini-swe-agent
../donors/nyuctf_agents
../donors/pi
```

The local checkout is authoritative for source adaptation decisions.

For each donor, inspect enough source to answer:

```text
what mechanism is relevant to M0?
does FlagAgent need it?
reuse decision:
  concept-only
  selective-adaptation candidate
  reject/defer
```

### `ctf-agent`

Inspect CTF-specific solver/runtime and tracing concepts, especially local equivalents of solver lifecycle, tool/execution interface, tracing/output, error handling, and CTF submission behavior.

Do not inherit coordinator/swarm/message-bus architecture into M0.

### `mini-swe-agent`

Inspect the minimal loop, linear trajectory/history, independent action execution, limits, and output truncation.

### `pi`

Inspect assistant-message-before-tool-result ordering, call/result correlation, tool argument validation, sequential execution behavior, and tool-result ordering.

### `nyuctf_agents`

Inspect the baseline agent, CTF execution assumptions, and the separation between baseline and planner/executor architecture.

Donor reconnaissance MUST happen before invasive implementation.

The final implementation report MUST state:

- donor repositories inspected;
- local donor commit SHA(s);
- relevant paths/symbols inspected;
- reuse decision for each donor.

---

# 22. Source Reuse, License, and Provenance

The repository already has its project license.

FlagAgent v0.1.0 source adaptation remains MIT-only unless the human explicitly changes the policy.

`ctf-agent` being a primary product lineage donor does **not** waive provenance requirements.

Before adapting any donor source:

```text
identify exact local/upstream repository
→ record exact commit SHA
→ identify exact source path/component
→ verify component license/provenance
→ decide reuse mode
→ record FlagAgent destination
```

If exact provenance is unclear, do not adapt the code.

Conceptual learning is still allowed.

## 22.1 Required provenance record

When donor source is first adapted, add or update:

```text
THIRD_PARTY_NOTICES.md
```

Minimum record:

```text
upstream_repository
upstream_commit
upstream_path
license
copyright/provenance
reuse_mode
flagagent_destination
modifications
review_date
```

## 22.2 License commit rule

For actual source adaptation, provenance MUST be committed before the adaptation commit.

Required sequence:

```text
chore(license): record <donor> provenance
        ↓
<adaptation commit using appropriate Conventional Commit type>
```

Example:

```text
chore(license): record ctf-agent provenance
feat(loop): adapt ctf-agent tracing mechanism
```

The adaptation commit MUST NOT be pushed unless its corresponding provenance commit is present in the same branch/history.

Do **not** create license/provenance commits for concept-only learning or an independent rewrite that does not adapt donor source.

AGPL/incompatible projects such as BoxPwnr are external research references only and MUST NOT be source-adapted under the current policy.

---

# 23. Implementation Constraints

M0 runtime SHOULD use Python standard library only where practical.

M0 MUST NOT add a runtime dependency without a concrete PRD requirement and human-approved justification.

Existing development tooling remains:

```text
Python >= 3.12
uv
Hatchling
pytest
pytest-cov
Ruff
```

M0 does not prescribe async vs sync as a permanent API guarantee, a specific module tree, or dataclass vs other small data structures.

Choose the smallest clear implementation.

Do not create directories/layers solely for architecture symmetry.

---

# 24. Required Deterministic Acceptance Tests

The implementation MUST provide deterministic tests demonstrating all applicable acceptance criteria.

## A. Run and persistence

**AC-M0-001 — Run creation**  
A Run creates a unique Run directory, `run.json`, `events.jsonl`, and `workspace/` without overwriting an existing Run.

**AC-M0-002 — Immutable metadata role**  
`run.json` records the adopted schema/version/configuration fields and is not rewritten as trajectory state.

**AC-M0-003 — Atomic terminal result**  
A successful terminal commit produces one valid `result.json` through the required temporary-file replacement path.

**AC-M0-004 — Trailing JSONL interruption**  
A complete `events.jsonl` prefix plus one incomplete trailing line is handled without treating the partial line as a committed event.

**AC-M0-005 — No-result state**  
A simulated result-commit failure produces no valid committed `result.json` and is distinguishable from `unsolved`.

## B. Conversation and tool ordering

**AC-M0-006 — Assistant-before-tool-result**  
The assistant response requesting tools is present in conversation state before corresponding results.

**AC-M0-007 — Sequential multiple calls**  
Multiple calls from one response execute in declared order.

**AC-M0-008 — Call correlation**  
Every requested tool result is correlated to the correct `call_id`.

**AC-M0-009 — Unknown tool recovery**  
An unknown tool never executes, produces a correlated model-visible error, is persisted, and the model may continue.

**AC-M0-010 — Invalid arguments recovery**  
Invalid known-tool arguments never execute, produce a correlated model-visible error, and the model may continue.

**AC-M0-011 — Duplicate call ID failure**  
Duplicate `call_id` input is rejected as `error/provider_error`.

## C. Verifier authority

**AC-M0-012 — Model text is not success**  
A response containing a flag-looking string without successful `submit_flag` verification does not solve the Run.

**AC-M0-013 — Wrong flag is normal**  
An incorrect candidate produces `incorrect`, does not solve, and later calls may execute.

**AC-M0-014 — Correct flag solves**  
A stripped, case-sensitive exact match verified by the authoritative verifier produces `solved/verified_flag`.

**AC-M0-015 — Solve short-circuits remaining calls**  
After a correct submission, later calls in the same model response do not execute.

**AC-M0-016 — Verifier failure distinction**  
Verifier infrastructure failure produces `error/verifier_error`, not `incorrect`.

## D. Shell/tool semantics

**AC-M0-017 — Non-zero exit is evidence**  
A fake shell result with non-zero exit code is returned to the model normally and does not become `tool_error`.

**AC-M0-018 — Timeout is evidence**  
A fake timed-out command has `timed_out=true` and `exit_code=null` without automatically becoming framework error.

**AC-M0-019 — Executor infrastructure failure**  
A fake executor failure on a valid call terminates as `error/tool_error`.

**AC-M0-020 — Output truncation**  
Oversized stdout/stderr are deterministically head+tail truncated with an explicit marker and `truncated=true`.

**AC-M0-021 — Exact model-visible persistence**  
The normalized/truncated tool result returned to the model matches the persisted model-visible tool-result evidence.

## E. Termination and limits

**AC-M0-022 — Model stop**  
A normal response with no tool calls ends `unsolved/model_stop`.

**AC-M0-023 — Exact model-turn limit**  
Exactly `max_model_turns` model calls are allowed; no additional call occurs.

**AC-M0-024 — Final allowed turn tools**  
Tool calls produced by the final permitted model turn may complete before `model_turn_limit` is committed if no earlier terminal result occurs.

**AC-M0-025 — Wall limit**  
A deterministic fake clock proves wall-budget exhaustion produces `unsolved/wall_limit`.

**AC-M0-026 — Remaining wall budget bounds command budget**  
The effective command budget does not exceed remaining Run wall time.

**AC-M0-027 — Provider/model failure**  
A scripted model-boundary failure terminates `error/provider_error`.

## F. Result and audit evidence

**AC-M0-028 — Terminal states remain distinct**  
Tests independently demonstrate `solved`, `unsolved`, `error`, and no committed result.

**AC-M0-029 — Counters are mechanically correct**  
`model_calls`, `tool_calls`, and `flag_submissions` in the committed result match observed execution.

**AC-M0-030 — Trajectory reconstruction**  
Persisted normalized events are sufficient to determine what model response occurred, which calls were requested and executed, what result the model received, which flags were submitted, verifier outcomes, and why the Run stopped.

---

# 25. Verification Gate

Before M0 can be considered PASS, the implementation MUST pass all applicable project checks:

```bash
uv lock --check
uv run pytest
uv run ruff check .
uv run ruff format --check .
git diff --check
```

`pytest-cov` MAY be run for diagnostic visibility, but M0 has no numeric coverage threshold.

A check is evidence only if it was actually executed and its successful result observed.

The read-only `flagagent-gate` SHOULD perform the final Concept/PRD compliance review.

The gate supplements deterministic evidence; it does not replace tests.

---

# 26. M0 Definition of Done

M0 is complete only when all of the following are true.

### Runtime

- normalized Model boundary exists;
- scripted model drives deterministic trajectories;
- `shell` and `submit_flag` are the only product tools;
- tool calls execute sequentially and correlate correctly;
- verifier authority is mechanically enforced;
- terminal semantics are implemented exactly.

### Persistence

- `run.json`, `events.jsonl`, `result.json`, and workspace roles are implemented;
- event ordering is auditable;
- exact model-visible tool results are persisted;
- atomic result semantics and no-result behavior are tested.

### Limits

- exact model-turn semantics are tested;
- wall timeout is deterministically testable;
- command budget respects remaining wall time;
- model/log output normalization limits are tested.

### Quality

- all required deterministic acceptance tests pass;
- Ruff lint/format checks pass;
- `uv.lock` is consistent;
- no unnecessary runtime dependency was introduced;
- no donor repository was modified;
- no M1/M2 architecture was implemented opportunistically.

### Donors and provenance

- all four local donors were inspected before implementation;
- final report records donor SHAs and inspected paths;
- any actual source adaptation has exact license/provenance evidence;
- `THIRD_PARTY_NOTICES.md` is updated when required;
- required provenance commit precedes any source-adaptation commit.

### Git

- completed work is divided into coherent Conventional Commits;
- verified checkpoints are pushed according to `AGENTS.md`;
- destructive/force Git operations were not used;
- final handoff records relevant commit SHA(s) and pushed branch.

### Compliance

- no unresolved material Concept or PRD violation remains;
- `flagagent-gate` returns PASS, or any INCONCLUSIVE finding is resolved by the human before M1 begins.

---

# 27. M0 PASS / FAIL Decision

## PASS

M0 passes when:

```text
all required deterministic acceptance criteria pass
+
project verification passes
+
donor/provenance requirements are satisfied
+
no Fundamental Invariant is violated
+
human accepts the milestone evidence
```

## FAIL

M0 fails if any required behavior is missing or contradicted, including:

- model text can establish solved without verifier;
- unknown tools execute;
- results cannot be correlated to calls;
- assistant/tool-result ordering is wrong;
- framework failure is silently treated as normal command evidence;
- terminal statuses collapse into one ambiguous outcome;
- persisted model-visible result differs from what the model actually received;
- M0 requires Docker/real provider to pass;
- source is adapted without verified provenance;
- donor repositories are modified.

## INCONCLUSIVE

M0 is inconclusive when required evidence cannot be produced even though no definite violation is proven.

An inconclusive gate does not authorize M1.

---

# 28. Deferred Decisions for M1/M2

This PRD intentionally does not freeze:

```text
real Docker process implementation
Agent image/tooling
resource limits for Agent/Target
real networking implementation
target lifecycle
real challenge copy/mount semantics
provider/model selection
provider SDK retry policy
solver system prompt
smoke challenge set
real token/cost metrics
PyPI/npm publishing
final CLI command syntax
```

Those decisions belong to later PRDs and should use evidence learned from M0.

---

# 29. Research Basis — Informative, Not Normative

This PRD structure was informed by:

- OpenAI Codex guidance emphasizing goal, context, constraints, evidence, and success criteria for coding agents;
- GitHub Spec Kit / spec-driven development guidance that treats the specification as the shared source of truth before planning, tasks, implementation, and verification;
- Productboard PRD guidance covering problem/outcome, scope, assumptions, constraints, risk, and measurable acceptance/success criteria;
- Linear guidance favoring short, focused specifications that communicate key product and technical decisions without unnecessary breadth;
- Martin Fowler community writing and experiments on spec-driven/agentic development, including explicit assumptions and independent verification of AI-generated work;
- the frozen FlagAgent v0.1.0 Concept;
- local donor inspection requirements for `ctf-agent`, `mini-swe-agent`, `pi`, and `nyuctf_agents`.

These sources guide the **shape of the PRD**. The normative FlagAgent behavior remains defined by the human-approved Concept and this PRD.

---

# 30. Approval

This document remains **DRAFT** until explicitly approved by the human orchestrator.

After approval:

```text
PRD-M0 approved
        ↓
bounded implementation plan
        ↓
M0 implementation
        ↓
deterministic verification
        ↓
review + flagagent-gate
        ↓
human M0 PASS decision
        ↓
PRD-M1
```

No implementation should treat an unapproved draft as authorization to change architecture beyond explicit human instructions.
