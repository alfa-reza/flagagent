# FlagAgent v0.1.0 — Concept and Architecture Baseline

> **Status:** FROZEN CONCEPT — ready to be translated into milestone PRDs  
> **Project:** FlagAgent  
> **Release:** v0.1.0  
> **Document role:** Concept / architecture baseline, not a PRD or implementation plan  
> **Primary scope:** legal and authorized CTFs, cybersecurity benchmarks, security labs, and sandboxed experiments  
> **Primary platform:** Linux containers on Linux Docker Engine  
> **Development philosophy:** KISS, evidence before abstraction, deterministic verification before model claims  
> **Human authority:** the human orchestrator makes final product and architecture decisions  
> **Research snapshot:** 2026-08-14

---

## 0. Document Contract

This document is the **conceptual source of truth for FlagAgent v0.1.0**.

It exists to make the product intent, architecture boundaries, security assumptions, runtime semantics, milestone outcomes, and donor policy unambiguous before implementation requirements are split into PRDs.

This document is intentionally **not**:

- a PRD;
- `AGENTS.md`;
- an OpenCode prompt;
- an implementation plan;
- a solver system prompt;
- an API reference;
- a roadmap beyond v0.1.0.

The intended documentation hierarchy is:

```text
plans/Flagagent-v0.1.0.md
        ↓
milestone PRD(s)
        ↓
AGENTS.md
        ↓
bounded implementation plan
        ↓
implementation + tests + evidence
```

If two artifacts conflict, use this precedence:

```text
latest explicit human decision
        ↓
this concept
        ↓
approved PRD
        ↓
AGENTS.md / execution instructions
        ↓
implementation details
```

The concept should change only when evidence shows that a frozen assumption is wrong, unsafe, contradictory, or impractical.

### Normative language

`MUST`, `MUST NOT`, `SHOULD`, `SHOULD NOT`, and `MAY` are used only when a boundary needs to be explicit.

They describe FlagAgent design intent, not a claim of standards compliance.

---

# 1. Product Thesis

> **FlagAgent is a small, model-independent CTF agent harness that lets an LLM act through an isolated execution environment, records an inspectable trajectory, and declares success only after an authoritative verifier confirms the submitted flag.**

The first release is deliberately narrow.

FlagAgent v0.1.0 should answer one practical question:

> **Can a small single-agent loop solve representative CTF tasks inside a controlled environment while producing reproducible evidence and never confusing a model claim with a verified solve?**

The baseline mental model is:

```text
                     ┌─────────────┐
                     │    Model    │
                     └──────┬──────┘
                            │
                     normalized I/O
                            │
                     ┌──────▼──────┐
                     │  AgentLoop  │
                     └───┬─────┬───┘
                         │     │
                    shell│     │submit_flag
                         │     │
              ┌──────────▼┐   ┌▼──────────┐
              │ Agent     │   │ Verifier  │
              │ Container │   │ authority │
              └─────┬─────┘   └───────────┘
                    │
                 workspace
```

If a local target service is required:

```text
Agent Container
      │
      │ dedicated internal network
      ▼
Target Container
```

The target is treated as untrusted challenge workload, not trusted infrastructure.

---

# 2. Why This Baseline

FlagAgent is not trying to win an architecture contest.

The first release exists to establish a trustworthy baseline that is:

- small enough for one maintainer to understand;
- observable enough to debug;
- deterministic where deterministic verification is possible;
- sandboxed enough to run untrusted commands honestly;
- modular enough to change model/provider later;
- measurable enough to compare future strategies;
- simple enough that architecture does not hide model behavior.

The governing rule is:

```text
build the smallest useful harness
→ verify it
→ run it
→ measure failures
→ add only the smallest justified capability
```

This follows a recurring pattern across current coding-agent and software-engineering guidance:

- OpenAI recommends durable repository guidance, task planning when complexity warrants it, and validation through executable feedback.
- Anthropic recommends starting with the simplest sufficient agentic pattern and warns that frameworks can obscure prompts, responses, and failure causes.
- Google engineering guidance favors small, focused changes and tests that travel with behavior.
- Microsoft reliability guidance explicitly treats unnecessary architecture complexity as a reliability cost.
- Z.AI coding-agent guidance emphasizes structured context, constraints, a clear definition of done, planning for complex work, and using MCP/Skills only when they solve repeated needs.
- OpenCode separates concise persistent `AGENTS.md` guidance from on-demand files, permissions, agents, references, and skills.

The implication for FlagAgent is simple:

> **Single-agent v0.1.0 is the baseline to evaluate, not an incomplete multi-agent system.**

---

# 3. Goals and Non-Goals

## 3.1 Goals

FlagAgent v0.1.0 aims to establish:

1. a minimal LLM-driven solver loop;
2. explicit model/tool boundaries;
3. isolated command execution;
4. authoritative flag verification;
5. durable, inspectable run artifacts;
6. bounded runtime and resource usage;
7. a small provider boundary;
8. deterministic tests for runtime semantics;
9. a real-model smoke evaluation;
10. a baseline against which future complexity can be measured.

## 3.2 Explicit non-goals

v0.1.0 does **not** require:

- multi-agent orchestration;
- planner/executor architecture;
- model racing or voting;
- swarm coordination;
- specialist agents;
- autonomous retries;
- persistent memory;
- RAG or vector databases;
- PTY or interactive shell sessions;
- debugger orchestration;
- browser automation;
- CTFd/live-competition integration;
- MCP as a product-runtime foundation;
- provider routing/fallback;
- a generic plugin system;
- an event bus;
- event sourcing;
- a relational database;
- resume/checkpoint support;
- distributed workers;
- Kubernetes;
- microVM isolation;
- a web UI;
- a generalized evaluation framework.

A non-goal is not rejected forever.

It means:

> **There is not yet enough measured evidence to pay its complexity cost.**

---

# 4. Fundamental Invariants

These invariants define the identity and trust boundaries of v0.1.0.

A PRD or implementation MAY refine them but MUST NOT silently weaken them.

## I-1 — The model acts only through explicitly exposed tools

For v0.1.0 the product-level tool surface is:

```text
shell
submit_flag
```

The tool set is an allowlist.

The model MUST NOT gain an undocumented path to:

- host shell execution;
- arbitrary host filesystem access;
- Docker daemon access;
- provider credentials;
- verifier secrets;
- unrelated control-plane secrets.

Unknown or hallucinated tool names MUST NOT execute.

## I-2 — Untrusted execution does not run directly on the host

Model-generated commands and challenge workloads are untrusted.

For real execution milestones they MUST run inside an explicitly controlled isolation boundary.

v0.1.0 uses Docker as that containment baseline.

A deterministic fake executor is acceptable only for the milestone that proves AgentLoop semantics.

## I-3 — Control secrets remain outside solver and target trust boundaries

Agent and Target containers MUST NOT receive secrets they do not need.

Examples include:

- expected flags;
- verifier secrets;
- provider API keys;
- Docker socket or daemon credentials;
- unrelated host credentials;
- unrelated CTF control-plane credentials.

A credential intentionally supplied as part of a challenge is challenge data, not a framework secret.

## I-4 — Only the verifier establishes `solved`

A flag-shaped string in model output is not success.

```text
model text
stdout
regex match
model confidence
        ≠ solved
```

Only this transition is authoritative:

```text
submit_flag(candidate)
        ↓
Verifier.check(candidate)
        ↓
correct
        ↓
solved
```

## I-5 — Model-visible execution is auditable

The runtime MUST preserve enough evidence to reconstruct:

- what the model returned;
- which tool calls it requested;
- which calls actually executed;
- what normalized result each tool returned to the model;
- which flag candidates were submitted;
- how the verifier responded;
- why the run terminated.

FlagAgent does not require provider-private chain-of-thought or hidden reasoning tokens.

## I-6 — Local targets are untrusted workloads

A local challenge target MAY be malicious or vulnerable by design.

It MUST NOT automatically receive:

- privileged mode;
- Docker socket access;
- arbitrary host mounts;
- framework secrets;
- unrestricted resources;
- broader networking than the challenge requires.

## I-7 — Challenge-provided orchestration is not trusted by default

The presence of a challenge `Dockerfile`, Compose file, Makefile, script, or provisioning artifact does not make it safe for host-level execution.

v0.1.0 MUST NOT automatically run arbitrary challenge provisioning with Docker/host privileges.

Controlled smoke targets may use project-owned or explicitly audited configuration.

---

# 5. Frozen v0.1.0 Baseline

The following choices are frozen so the first release can finish:

```text
one Run
one attempt
one active model
one AgentLoop
one linear conversation
one Agent container per real Run
one verifier
two product tools: shell + submit_flag
JSON / JSONL run artifacts
Docker execution for containment milestone
fresh non-interactive process for each shell call
no resume/checkpoint
no planner/executor
no multi-agent
no PTY/session manager
one real provider path is sufficient for the first usefulness gate
```

These are **release decisions**, not permanent FlagAgent identity.

After v0.1.0, a decision can change when representative evaluation demonstrates a material benefit while the Fundamental Invariants remain intact.

---

# 6. Run Model

## 6.1 Run lifecycle

Conceptually:

```text
create Run directory
      ↓
persist immutable run metadata
      ↓
prepare verifier
      ↓
prepare Agent environment
      ↓
prepare optional audited Target
      ↓
construct initial conversation
      ↓
execute AgentLoop
      ↓
known terminal outcome?
   ┌───────┴────────┐
  yes               no / process loss
   │                 │
commit result     no committed result
   │
best-effort cleanup
```

A state-machine framework is not required.

## 6.2 Terminal semantics

Committed run status is limited to:

```text
solved
unsolved
error
```

`solved` means the authoritative verifier returned correct.

`unsolved` means the harness terminated normally without a verified flag.

Initial normal unsolved reasons are expected to include:

```text
model_stop
model_turn_limit
wall_limit
```

`error` represents known framework/infrastructure failure such as:

```text
provider_error
sandbox_error
verifier_error
tool_error
serialization_error
```

If the process is interrupted or the terminal artifact cannot be committed:

```text
no committed result
```

This distinction is important:

```text
failed to solve
≠ known framework failure
≠ unknown/catastrophic interruption
```

---

# 7. AgentLoop Contract

Exact Python APIs are not frozen here.

The behavior is.

## 7.1 Core loop

```text
check remaining Run time
        ↓
model.generate(messages, tools)
        ↓
record assistant response
        ↓
tool calls?
   ┌────┴────┐
   no        yes
   │          │
unsolved      execute in declared order
model_stop       │
                 ├── shell
                 └── submit_flag
                      │
             append correlated results
                      │
            verifier says correct?
                 ┌────┴────┐
                yes        no
                 │          │
               solved      loop
```

## 7.2 Conversation ordering

The assistant message that requested a tool call MUST enter conversation state before the corresponding tool result.

The minimum ordering is:

```text
user/system
assistant + tool request(s)
tool result(s)
assistant
...
```

## 7.3 Model turn definition

One model turn is exactly one model-generation invocation.

It is not:

- one message;
- one tool call;
- one shell command;
- one reasoning step.

This gives `max_model_turns` a stable meaning.

## 7.4 Multiple tool calls

If one model response requests multiple tools:

- execute them in normalized model-declared order;
- preserve a correlation identifier for every call/result;
- count the response as one model turn.

If `submit_flag` returns `correct`, the Run becomes solved immediately and later calls from that same response MUST NOT execute.

Sequential execution is the v0.1.0 baseline because it is easier to reason about, record, reproduce, and stop safely.

## 7.5 Normal model stop

A normal response with no tool calls ends the Run as:

```text
status = unsolved
reason = model_stop
```

v0.1.0 does not automatically reprompt the model simply because it stopped.

## 7.6 Unknown tools

Unknown tool names:

```text
do not execute
→ produce structured model-visible unknown-tool result
→ record it
→ allow the model to recover
```

A hallucinated tool name alone is not a framework crash.

---

# 8. Model Boundary

FlagAgent keeps model access behind a deliberately small replaceable boundary.

Conceptually:

```python
Model.generate(messages, tools) -> ModelResponse
```

The normalized response needs, at minimum:

```text
content
tool_calls:
  - call_id
  - name
  - arguments
usage
```

A provider adapter MAY create a runtime-local `call_id` if the provider does not supply one.

Core runtime logic MUST NOT depend on provider-specific response objects.

Provider credentials remain on the trusted control side.

v0.1.0 does not need:

- provider registry;
- provider router;
- automatic fallback;
- model racing;
- generic capability matrix;
- pricing engine;
- framework-wide retry abstraction.

The correct initial claim is:

> **FlagAgent has a small replaceable model boundary.**

Do not claim proven provider independence until at least a second provider is actually integrated and evaluated.

---

# 9. Tool Contracts

## 9.1 `shell`

Conceptual interface:

```text
shell(command)
    →
{
  stdout,
  stderr,
  exit_code,
  timed_out,
  truncated
}
```

Additional fields MAY be introduced when a PRD demonstrates a concrete need.

### Process semantics

For real execution:

```text
one Run
= one Agent container

each shell call
= one fresh non-interactive process
  inside that same container
```

Persisted state:

- filesystem changes;
- workspace files;
- generated scripts;
- compiled binaries;
- Run-local installed artifacts, if allowed by the image/environment.

Not guaranteed to persist between calls:

- current directory;
- shell-local variables;
- exported environment changes;
- shell functions;
- job-control state;
- interactive terminal state.

The model can always issue compound commands:

```bash
cd /workspace/challenge && make && ./solve.py
```

### Command failure vs tool failure

A non-zero command exit is normal evidence:

```text
exit_code != 0
≠ framework tool_error
```

`tool_error` is reserved for failure of the executor/runtime to fulfill the valid tool contract.

## 9.2 `submit_flag`

Conceptually:

```text
submit_flag(candidate)
    →
correct | incorrect
```

The underlying authority is:

```text
Verifier.check(candidate)
```

`incorrect` is a normal candidate outcome.

Verifier infrastructure failure is an error, not an incorrect flag.

The expected flag or verifier secret MUST NOT appear in solver-visible trajectory data.

---

# 10. Bounded Execution and Output

An agent harness that can execute arbitrary commands must bound both time and data flow.

## 10.1 Canonical limits

The concept uses these names:

```text
max_model_turns
wall_timeout_seconds
command_timeout_seconds
```

The sandbox also requires explicit resource constraints for at least:

```text
memory
cpu
pids
```

Exact defaults belong in the PRD/configuration, not this concept.

## 10.2 Run wall deadline

The wall deadline includes active solving work such as:

- model/provider calls;
- tool execution;
- verifier calls;
- runtime orchestration.

A monotonic clock SHOULD be used for elapsed-time enforcement.

When the deadline is known to be exhausted:

```text
status = unsolved
reason = wall_limit
```

Minimal final bookkeeping and cleanup MAY occur after the solving deadline is detected.

## 10.3 Command timeout

A command timeout while Run time remains is a normal tool result:

```text
timed_out = true
```

If the overall Run deadline expires during a command, terminal handling should ultimately record `wall_limit` once control returns.

## 10.4 Output limits

At minimum, the design distinguishes:

```text
max_model_tool_output
max_logged_tool_output
```

with:

```text
max_logged_tool_output >= max_model_tool_output
```

The event trajectory MUST preserve the exact normalized/truncated result that the model received.

Raw pre-normalization output is not required.

### Collection itself must be bounded

The host MUST NOT accumulate unbounded stdout/stderr and truncate only after command completion.

Bounding must happen while output is collected, or through an equivalent mechanism that prevents unbounded host-memory exposure.

Large artifacts belong in the workspace; the model can inspect selected portions afterward.

---

# 11. Persistence and Reproducibility

The minimum per-Run structure is:

```text
runs/
└── <run-id>/
    ├── run.json
    ├── events.jsonl
    ├── result.json
    └── workspace/
```

No database or event-store service is required.

## 11.1 `run.json`

`run.json` is an immutable configuration/provenance snapshot.

The exact schema belongs in the PRD/tests, but it should be able to represent provenance such as:

- Run identifier;
- FlagAgent version;
- architecture/concept version;
- Git commit;
- challenge identity/hash;
- requested provider/model;
- prompt hash/version;
- sandbox image identity/digest;
- limits;
- network mode;
- explicit security relaxations;
- start time.

Use `0.1.0` in machine-readable version fields.

Use `v0.1.0` for human-facing release labels and Git tags.

## 11.2 `events.jsonl`

`events.jsonl` is an append-oriented observable trajectory.

It should capture normalized categories sufficient for model, tool, flag, and framework error events.

After a hard crash, a reader may accept complete lines and reject or ignore one trailing incomplete line.

## 11.3 `result.json`

`result.json` is the committed terminal outcome.

The preferred write pattern is:

```text
serialize
→ write temporary file
→ flush/close
→ atomic rename in the same filesystem
```

Do not promise stronger durability than the filesystem/runtime actually provides.

Interpretation:

```text
valid result.json
= known terminal result committed

missing/invalid result.json
= no committed terminal result
```

---

# 12. Docker Containment Baseline

Docker is a **containment baseline**, not a perfect security boundary.

The trusted computing base includes the configured host kernel, Docker daemon/runtime, FlagAgent control process, verifier, and approved images/configuration.

## 12.1 Reference platform

Release-gating containment for v0.1.0 targets:

```text
Linux host
Linux Docker Engine
Linux containers
```

Docker Desktop, rootless Docker, Podman, macOS, and Windows MAY work, but they are not part of the first containment claim unless separately tested.

The initial controlled image may use Ubuntu 24.04 LTS.

Evaluation images SHOULD be pinned by digest when reproducibility matters.

## 12.2 Agent container default posture

The baseline should preserve least privilege:

```text
privileged = false
host network = false
Docker socket = absent
container user = non-root
Docker default seccomp = retained
no-new-privileges = enabled
no additional Linux capabilities unless explicitly required
framework/provider/verifier secrets = absent
workspace = intentionally writable
unrelated host paths = absent
```

Docker documentation explicitly recommends retaining the default seccomp profile, supports `no-new-privileges`, and notes that containers otherwise have no default CPU/memory constraints.

Therefore resource/security settings are part of the product boundary, not optional polish.

## 12.3 Security relaxations

A challenge MAY require a relaxation such as:

- root inside the container;
- `SYS_PTRACE`;
- another Linux capability;
- seccomp override;
- device access;
- an additional mount;
- host-gateway exposure.

Every relaxation MUST be:

1. explicit;
2. scoped to one Run/challenge;
3. recorded in Run provenance;
4. absent from global defaults unless later evidence justifies a new baseline.

## 12.4 Target containment

A project-launched target container should default to the same principle:

- no privileged mode;
- no Docker socket;
- no control secrets;
- bounded resources;
- minimal mounts;
- only required networking.

## 12.5 Network modes

The concept recognizes three modes:

```text
none
local
external
```

### `none`

The Agent receives no intended non-loopback network connectivity.

### `local`

The Agent and optional Target communicate through a dedicated user-defined internal Docker network.

The intent is:

```text
Agent ↔ intended Target
no normal external egress
```

No automatic Internet fallback is permitted.

### `external`

External networking exists only when the challenge explicitly requires it.

It must be an explicit Run choice.

## 12.6 Filesystem boundary

Default philosophy:

```text
workspace
= writable

challenge source/input
= copied into workspace or mounted read-only

other host paths
= absent
```

Do not mount by default:

- Docker socket;
- the full user home directory;
- the FlagAgent source repository writable into solver;
- donor repositories writable into solver;
- provider credential directories;
- unrelated host paths.

## 12.7 Disk exhaustion

CPU/RAM/PID limits do not automatically solve disk growth.

v0.1.0 should:

- bound command/model-visible/logged output;
- observe writable storage growth where practical;
- use available storage quota mechanisms only where the host reliably supports them;
- avoid claiming a universal disk quota.

A custom storage quota subsystem is not required.

---

# 13. Challenge Input Boundary

v0.1.0 does not need a generalized challenge platform.

Conceptually, a Run needs only:

```text
challenge directory/input
challenge description
optional target endpoint
verifier/control data kept separately
```

A passive Challenge data structure MAY be introduced for clarity.

Do not begin with lifecycle-heavy abstractions such as:

```text
Challenge.provision()
TargetProvider
TargetRegistry
lease manager
```

unless a real requirement forces them.

---

# 14. Milestone Model

The concept uses three evidence gates.

They define **what must be proven**, not the exact implementation checklist.

Exact test cases, schemas, commands, numeric defaults, and chosen provider belong in milestone PRDs.

## M0 — Prove the Loop

**Purpose:** prove runtime semantics deterministically before Docker or a real model adds noise.

Use:

```text
scripted/fake model
fake executor
fake verifier
```

M0 should demonstrate that:

- conversation ordering is correct;
- tool calls correlate with their results;
- tool execution order is deterministic;
- unknown tools never execute;
- a wrong flag never solves the Run;
- only verifier success solves;
- execution stops immediately after a verified solve;
- command failure and executor failure are distinguishable;
- model turn limits have exact semantics;
- wall-limit behavior is deterministic-testable;
- model-visible tool results match persisted evidence;
- terminal outcomes and interrupted/no-result state are distinguishable.

M0 SHOULD require almost no donor code.

## M1 — Prove Containment

**Purpose:** replace the fake shell with real Docker execution without redesigning the loop.

The model can remain deterministic.

M1 should prove:

- model commands execute in the Agent container, not directly on host;
- one container belongs to one Run;
- each shell call starts a fresh process in that container;
- filesystem state persists while shell-local state is not assumed to;
- command timeout works;
- output collection is bounded;
- CPU, memory, and PID constraints are enforced on the reference host;
- security defaults are actually present;
- Docker socket/control secrets/unrelated host mounts are absent;
- `none` and `local` networking behave as specified;
- controlled Target containers are bounded;
- labels/identifiers permit orphan discovery and cleanup;
- every explicit security relaxation appears in Run provenance.

A real LLM is not required for M1.

## M2 — Prove Usefulness

**Purpose:** run the same harness with one real provider/model on a small frozen smoke set.

Add only what is needed for:

```text
one real provider path
one real model
one versioned/hashed solver prompt
one fixed representative smoke set
```

The smoke set should be:

- legal and authorized;
- repeatable;
- small enough for a solo maintainer;
- frozen before release evaluation;
- non-trivial enough to require multiple observe/act cycles.

Minimum useful metrics per attempt:

```text
solved
failure_reason
duration_seconds
model_calls
tool_calls
input_tokens
output_tokens
```

Cost and resource metrics MAY be added when provider/runtime data makes them reliable.

v0.1.0 does not require a high solve rate.

It requires proof that:

- the real provider path works end to end;
- credentials remain control-side;
- trajectories are analyzable;
- at least one representative challenge is verifiably solved;
- metrics and terminal outcomes are machine-extractable;
- failure reasons can be classified.

---

# 15. Evaluation Philosophy

FlagAgent is built and changed through evaluation, not reputation.

Prefer deterministic verification whenever a claim can be checked using:

- tests;
- exit codes;
- parsers;
- schemas;
- invariants;
- checksums;
- resource observations;
- verifier responses;
- flag submission results.

Use LLM-as-a-Judge only for qualities that genuinely require judgment, such as:

- architecture clarity;
- maintainability;
- relevance;
- completeness;
- reasoning quality.

When comparing models or strategies, record task fit and evidence such as:

- solve rate;
- time-to-flag;
- model/tool calls;
- token usage;
- monetary cost when known;
- failed attempts;
- reliability/variance;
- resource usage;
- human intervention.

Do not infer that a model or orchestration style is best from vendor reputation alone.

---

# 16. Donor Policy

## 16.1 Core rule

> **No donor repository is copied wholesale into FlagAgent v0.1.0.**

The default process is:

```text
research
→ understand mechanism
→ compare with exact FlagAgent need
→ reuse concept where sufficient
→ adapt only the smallest justified code
→ verify provenance/license/commit
→ test in FlagAgent
```

Reuse modes:

1. **Concept reference** — learn mechanism/behavior, implement a small FlagAgent version.
2. **Selective adaptation** — adapt a small component when that is materially better than rewriting.
3. **Dependency/integration** — prefer an upstream dependency when long-term ownership is healthier than vendoring.
4. **Reject/defer** — do not integrate when coupling, license, complexity, or fit is poor.

## 16.2 v0.1.0 donor set

The canonical donor set is limited to repositories already present in the project workspace.

| Donor | v0.1.0 role | Retain | Do not inherit |
|---|---|---|---|
| `SWE-agent/mini-swe-agent` | primary loop reference | linear history, minimal loop, fresh command process, benchmark-first simplicity | SWE-bench assumptions, full configuration/runtime |
| `earendil-works/pi` | primary boundary reference | clean model/agent/tool separation, tool-result ordering, explicit state | large multi-provider/runtime surface, coding-agent UX |
| `verialabs/ctf-agent` | CTF operations reference | CTF workflow lessons, container lifecycle, labels/orphan cleanup, tooling-image lessons | coordinator/swarm baseline, permissive sandbox defaults |
| `NYU-LLM-CTF/nyuctf_agents` | CTF strategy/evaluation reference | baseline vs D-CIPHER comparison, Docker-based CTF execution lessons | planner/executor/autoprompter in v0.1.0 core |

### mini-swe-agent

This is the strongest conceptual donor for the first core loop.

Current upstream emphasizes:

- a very small agent loop;
- completely linear history;
- independent command execution;
- simple replacement of local command execution with sandbox execution;
- evaluation as a first-class use case.

FlagAgent should copy the **simplicity lesson**, not its benchmark-specific product shape.

### Pi

Pi is the strongest reference for clean boundaries.

Useful lessons include:

- assistant response entering state before tool execution;
- clear tool-result correlation;
- explicit agent state;
- model/tool separation;
- configurable execution semantics.

FlagAgent intentionally needs a much smaller surface.

Pi itself documents that strong filesystem/process/network restrictions should come from an external sandbox/container boundary rather than assuming the agent runtime provides them.

### verialabs/ctf-agent

This project is valuable because it is CTF-native and operational.

Useful ideas include:

- one isolated solver environment;
- CTF tool images;
- container labeling and orphan cleanup;
- challenge workspace mounting;
- model diversity as a future evaluation hypothesis.

Its current sandbox defaults are **not** a security baseline for FlagAgent: upstream enables strong relaxations such as `SYS_ADMIN`, `SYS_PTRACE`, `seccomp=unconfined`, a host gateway, and a device mount for its competition needs.

FlagAgent should reuse mechanisms and lessons, not inherit those defaults.

### NYU `nyuctf_agents`

This donor provides both a baseline agent and D-CIPHER planner/executor architecture.

Its primary value to v0.1.0 is comparative:

> build and measure the smallest single-agent baseline before adopting planner/executor complexity.

## 16.3 Future references, not v0.1.0 donors

The following remain relevant but should not expand the canonical donor set now:

- **SWE-agent / EnIGMA** — interactive CTF tooling and debugger/PTTY lessons if non-interactive shell becomes a measured bottleneck.
- **SWE-ReX** — richer execution backend if FlagAgent's small Docker layer becomes a material maintenance or interactivity bottleneck.
- **CTFusion** — live CTF/CTFd evaluation, contamination-resistant event evaluation, and submission deduplication if live competition becomes a product requirement.
- **CHYing-agent** — lessons about context/tool visibility, progress handoff, orchestration, and stop-loss ideas if corresponding failure modes appear.

Potential libraries/frameworks such as Pydantic AI or evaluation systems such as Inspect are **ecosystem candidates**, not donor architecture for v0.1.0.

---

# 17. License and Provenance

FlagAgent's source-reuse policy for v0.1.0 is MIT-only unless the human maintainer explicitly changes it.

A repository-level license badge is not sufficient evidence for copying arbitrary code.

Before adapting donor code:

```text
identify exact upstream repository
→ record exact commit SHA
→ identify exact source path/component
→ verify the component's license/provenance
→ inspect bundled or derived content
→ choose reuse mode
→ preserve required notices
→ test adapted behavior
```

When the first donor code is actually incorporated, add a provenance record such as:

```text
THIRD_PARTY_NOTICES.md
```

Useful fields:

```text
upstream_repository
upstream_commit
upstream_path
license
copyright
reuse_mode
flagagent_destination
modifications
review_date
```

If exact provenance cannot be established:

```text
do not reuse the code
```

Conceptual learning is still permitted.

Dataset/benchmark licensing is a separate decision from source-code licensing.

---

# 18. Development Baseline

These are current project/toolchain decisions rather than permanent architecture identity:

```text
Python >= 3.12
uv-managed environment and lockfile
Hatchling build backend
pytest + pytest-cov
Ruff targeting Python 3.12
M0 runtime: Python standard library where practical
M1 runtime: Docker CLI / Docker Engine
initial controlled Linux image: Ubuntu 24.04 LTS
```

The package version is:

```text
0.1.0
```

Human-facing release/tag format is:

```text
v0.1.0
```

SemVer explicitly uses `0.y.z` for initial development and recommends starting at `0.1.0`.

Exact minimum Docker Engine version should be determined by M1 testing rather than guessed from the newest installed version.

Docker Compose is not required for the baseline.

---

# 19. Repository and Coding-Agent Contract

The project workspace currently separates the writable product repository from donor references.

Conceptually:

```text
project-flagagent/
├── FlagAgent/              ← product Git repository
└── donors/
    ├── mini-swe-agent/
    ├── pi/
    ├── ctf-agent/
    └── nyuctf_agents/
```

Default rule:

```text
FlagAgent/
= writable product repository

donors/
= read-only research/reference
```

Coding agents MAY:

- read/search donors;
- trace exact donor mechanisms;
- inspect upstream history and license;
- compare approaches.

Coding agents MUST NOT by default:

- modify donors;
- reformat donors;
- commit in donors;
- reset or clean donors;
- vendor a donor wholesale;
- treat the parent workspace as one Git repository.

Hard restrictions should be enforced with permissions/filesystem/runtime controls where practical rather than relying only on prose.

---

# 20. Documentation Strategy for Coding Agents

This concept is intentionally deeper than `AGENTS.md`.

Persistent agent instructions should remain short because major coding tools load them into context and adherence degrades as they become bloated or contradictory.

Recommended split:

```text
plans/Flagagent-v0.1.0.md
    deep product/architecture source of truth

plans/PRD-*.md
    exact milestone requirements and acceptance criteria

AGENTS.md
    concise operational repo guidance

tests / commands / gate agent
    executable verification
```

`AGENTS.md` should eventually contain only high-value durable information such as:

- repo/workspace map;
- writable and restricted paths;
- pointers to this concept and the current PRD;
- build/test/lint commands;
- current milestone;
- dependency/donor rules;
- security-critical do-not rules;
- definition of done.

Do **not** duplicate this entire concept into `AGENTS.md`.

OpenCode supports project `AGENTS.md`, custom instruction files, project references, per-agent permissions, and on-demand Skills. Use each for the problem it actually solves.

---

# 21. Preferred Implementation Workflow

For each milestone:

```text
read current concept clauses
        ↓
read current milestone PRD
        ↓
inspect current product code
        ↓
inspect a donor only for a concrete question
        ↓
make a bounded plan
        ↓
implement the current milestone only
        ↓
run deterministic verification
        ↓
review against concept + PRD
        ↓
fix material findings
        ↓
rerun verification
        ↓
record evidence
        ↓
stop at the milestone gate
```

The architecture does not authorize a coding agent to:

- redesign FlagAgent opportunistically;
- implement future roadmap features;
- add a framework because it looks useful;
- generalize a boundary before a second case exists;
- modify donor repositories.

When a frozen requirement appears contradictory, report:

```text
requirement
observed conflict
evidence
smallest possible change
alternatives
impact
```

The human orchestrator decides whether the concept changes.

---

# 22. Risks, Trade-offs, and Rejected Baselines

A useful concept document should record why obvious alternatives were not selected.

## 22.1 One loop vs planner/executor

**Chosen:** one direct loop.

**Trade-off:** less explicit task decomposition.

**Why:** simpler control flow, cheaper context, easier debugging, and a necessary baseline for measuring whether planner/executor actually helps.

## 22.2 Thin internal runtime vs agent framework

**Chosen:** small internal loop and boundaries.

**Trade-off:** FlagAgent owns some low-level code.

**Why:** the first release needs only a small subset of framework functionality, and external frameworks can hide the exact behavior FlagAgent wants to evaluate.

A framework becomes justified when repeated provider/runtime integration pain is measured.

## 22.3 Docker vs stronger isolation

**Chosen:** Linux Docker Engine containment baseline.

**Trade-off:** containers share the host kernel and are not equivalent to microVM isolation.

**Why:** Docker is widely available, easy to reproduce, integrates naturally with CTF tooling, and is sufficient to establish the first measurable containment baseline.

Do not market Docker as perfect sandboxing.

## 22.4 Non-interactive shell vs PTY

**Chosen:** fresh non-interactive process per call.

**Trade-off:** interactive debugger/program workflows are limited.

**Why:** this is much easier to isolate, time out, reproduce, and reason about.

If interactivity becomes a dominant measured failure, evaluate PTY/SWE-ReX-style execution after v0.1.0.

## 22.5 Local verifier vs live CTF submission

**Chosen:** authoritative local verifier boundary first.

**Trade-off:** live competition semantics are deferred.

**Why:** local verification is deterministic and free of scoreboard/network side effects.

CTFd/live submission should be a separate later integration.

## 22.6 JSON/JSONL vs database/event store

**Chosen:** filesystem artifacts.

**Trade-off:** less sophisticated query/concurrency support.

**Why:** one Run/one attempt does not justify a storage service.

---

# 23. Architecture Change Gate

The frozen concept may be reopened when concrete evidence shows one of the following:

1. a frozen semantic cannot be implemented reasonably;
2. deterministic tests expose a contradiction;
3. a security assumption proves false;
4. representative evaluation reveals a material repeated failure mode;
5. a second provider/backend/tool class proves a boundary too narrow;
6. licensing/provenance requires a design change.

A change proposal should contain:

```text
observed problem
evidence
impact
smallest proposed change
alternatives
complexity cost
expected measurable benefit
acceptance test
rollback/reject criterion
```

The following is not enough:

> "Another framework has this feature."

---

# 24. v0.1.0 Concept Definition of Done

This concept is ready for PRD work when the following are clear:

### Product

- product thesis and target use are unambiguous;
- v0.1.0 scope is intentionally small;
- success requires authoritative verification.

### Runtime

- one-loop baseline is defined;
- model-turn and tool-order semantics are defined;
- unknown-tool behavior is defined;
- shell persistence semantics are defined;
- terminal outcomes are distinguishable.

### Security

- trusted/untrusted boundaries are explicit;
- model commands never intentionally execute directly on host;
- Docker is described as containment, not perfect isolation;
- Agent and Target defaults follow least privilege;
- relaxations are explicit and auditable.

### Observability

- Run metadata, event trajectory, and terminal result have distinct roles;
- model-visible tool output is reproducible from persisted evidence;
- no-result/interruption is distinguishable from normal failure.

### Evaluation

- M0 proves semantics;
- M1 proves containment;
- M2 proves real-model usefulness;
- deterministic checks take priority over subjective judgement.

### Donors

- only the four workspace donors form the v0.1.0 donor set;
- donor mechanisms are learned selectively;
- code reuse requires exact provenance and MIT-compatible policy;
- future references do not become dependencies by accident.

### Coding-agent workflow

- concept, PRD, `AGENTS.md`, and implementation plan have separate responsibilities;
- donors are read-only by default;
- implementation is milestone-bounded;
- human remains the final architecture authority.

---

# 25. Final Decision

> **FlagAgent v0.1.0 is frozen as a small single-agent CTF harness baseline built around explicit tools, isolated Docker execution, authoritative verification, observable trajectories, and evidence-driven evolution.**

The first release should not attempt to predict the final FlagAgent architecture.

Its job is to create a baseline strong enough to answer future questions with evidence.

Final rule:

```text
simple first
secure enough to be honest
observable by default
verify deterministically
measure real failures
generalize only after variation exists
```

---

# Appendix A — Terminology

**AgentLoop**  
The product control loop that requests one model turn, processes its tool calls, records results, and decides whether to continue.

**Run**  
One complete FlagAgent attempt with immutable input/provenance, trajectory, workspace, and at most one committed terminal result.

**Agent container**  
The Docker environment in which untrusted solver commands execute.

**Target**  
An optional untrusted vulnerable service/application used by a challenge.

**Verifier**  
The authoritative component that decides whether a submitted candidate is correct.

**Model-visible result**  
The normalized/truncated tool output actually returned to the model.

**Control side**  
Trusted FlagAgent process/infrastructure outside the Agent/Target trust boundary.

**Security relaxation**  
An explicit deviation from the default containment posture required by a specific challenge.

---

# Appendix B — Research and Design Basis

Research was refreshed on **2026-08-14**.

The structure of this document intentionally borrows ideas from mature design/RFC formats rather than treating a concept file as a long prompt:

- **Go proposal template** — Abstract, Background, Proposal, Rationale.
- **Rust RFC template** — Motivation, detailed design, drawbacks, alternatives.
- **Kubernetes Enhancement Proposals** — explicit status, goals/non-goals, risks and test/evaluation thinking.
- **OpenAI Codex** — durable project guidance in `AGENTS.md`, context management, planning for complex work, executable validation, and repository-local planning artifacts.
- **Anthropic Claude Code** — concise structured project guidance and separation of persistent instructions from scoped/on-demand context.
- **Anthropic Building Effective Agents** — start with simple composable patterns; add agentic complexity only when outcomes justify it; ground agents in environment feedback.
- **Google Gemini Code Assist / Gemini CLI** — project context files and scoped context for agents.
- **Google Engineering Practices** — small focused changes and tests with behavioral changes.
- **Microsoft Well-Architected** — simplicity and clear justification for architecture complexity.
- **Z.AI Coding Agent Best Practice** — goal/context/constraints/done-when framing, plan-before-execution for complex work, project guidance, MCP/Skills for repeated needs.
- **OpenCode** — `AGENTS.md`, references, specialized agents, granular permissions, and on-demand Skills.
- **Docker Engine documentation** — default seccomp, `no-new-privileges`, explicit resource constraints, user-defined/internal networks, and non-root guidance.
- **Semantic Versioning 2.0.0** — `0.y.z` for initial development; `0.1.0` is the recommended starting point.

Primary project/donor references:

- `SWE-agent/mini-swe-agent`
- `earendil-works/pi`
- `verialabs/ctf-agent`
- `NYU-LLM-CTF/nyuctf_agents`

Future references retained for measured needs:

- `SWE-agent/SWE-agent` / EnIGMA
- `SWE-agent/SWE-ReX`
- `kaist-hacking/CTFusion`
- `yhy0/CHYing-agent`

Primary research locations:

```text
# Document/design formats
https://github.com/rust-lang/rfcs/blob/master/0000-template.md
https://github.com/golang/proposal/blob/master/design/TEMPLATE.md
https://github.com/kubernetes/enhancements/tree/master/keps/NNNN-kep-template

# OpenAI
https://learn.chatgpt.com/guides/best-practices
https://learn.chatgpt.com/docs/agent-configuration/agents-md

# Anthropic
https://code.claude.com/docs/en/memory
https://www.anthropic.com/engineering/building-effective-agents

# Google
https://docs.cloud.google.com/gemini/docs/codeassist/use-agentic-chat-pair-programmer
https://google.github.io/eng-practices/review/developer/small-cls.html

# Microsoft
https://learn.microsoft.com/en-us/azure/well-architected/reliability/simplify

# Z.AI
https://docs.z.ai/devpack/resources/best-practice

# OpenCode
https://opencode.ai/docs/rules/
https://opencode.ai/docs/agents/
https://opencode.ai/docs/permissions/
https://opencode.ai/docs/skills/
https://opencode.ai/docs/references/

# Docker
https://docs.docker.com/engine/security/seccomp/
https://docs.docker.com/engine/containers/resource_constraints/

# Versioning
https://semver.org/

# Active v0.1.0 donors
https://github.com/SWE-agent/mini-swe-agent
https://github.com/earendil-works/pi
https://github.com/verialabs/ctf-agent
https://github.com/NYU-LLM-CTF/nyuctf_agents

# Future references
https://github.com/SWE-agent/SWE-agent
https://github.com/SWE-agent/SWE-ReX
https://github.com/kaist-hacking/CTFusion
https://github.com/yhy0/CHYing-agent
```

---

**End of FlagAgent v0.1.0 Concept and Architecture Baseline.**
