# FlagAgent v0.1.0 Architecture

> **Release:** v0.1.0  
> **Scope:** authorized CTFs, security labs, benchmarks, and sandboxed experiments  
> **Reference platform:** Linux containers on Linux Docker Engine

This document describes the **as-built architecture of FlagAgent v0.1.0**. It focuses on system boundaries, component responsibilities, runtime semantics, containment, persistence, and the design decisions that materially shape the release.

It is not a PRD, roadmap, implementation plan, contributor guide, or coding-agent instruction file. The milestone PRDs preserve release requirements and acceptance history; the code and deterministic tests define executable behavior. Documentation drift between these artifacts should be treated as a defect and reconciled.

---

## 1. Overview

FlagAgent is a small LLM agent harness for solving controlled security challenges. A model operates through an explicit two-tool interface, commands execute inside a run-scoped Docker container, and a run is considered solved only when a trusted verifier accepts a submitted flag.

The v0.1.0 design intentionally favors a small, observable baseline over general orchestration infrastructure.

### Design principles

- **Keep the runtime small.** Prefer direct control flow over frameworks that do not solve a demonstrated problem.
- **Keep boundaries explicit.** Model access, tool execution, verification, challenge data, and persistence have separate responsibilities.
- **Verify deterministically when possible.** Model claims do not establish success.
- **Contain untrusted execution.** Model-generated commands and challenge workloads do not execute directly on the host.
- **Preserve evidence.** A run should leave enough model-visible and runtime evidence to understand what happened.
- **Add complexity from evidence, not speculation.** v0.1.0 does not generalize around hypothetical future requirements.

### Scope boundaries

v0.1.0 is a single-agent, single-attempt harness. It does not provide multi-agent orchestration, planner/executor decomposition, resume/checkpoint support, persistent memory, PTY sessions, generic plugins, provider routing/fallback, distributed workers, a database, a web UI, or live CTF platform integration.

These are scope choices for this release, not permanent restrictions on future versions.

---

## 2. Architecture Overview

### 2.1 Component view

```text
                         Trusted control side

 Challenge directory ──► CLI / Challenge Loader
                              │
                              ├──── expected flag ──────────────┐
                              │                                 │
                              ▼                                 ▼
                        ┌───────────┐                      ┌──────────┐
 Model API ◄──────────► │  Model    │                      │ Verifier │
                        │  Adapter  │                      └────┬─────┘
                        └─────┬─────┘                           │
                              │ normalized ModelResponse        │
                              ▼                                 │
                        ┌───────────┐                            │
                        │ AgentLoop │                            │
                        └───┬───┬───┘                            │
                            │   │                                │
                       shell│   │submit_flag                     │
                            │   └────────────────────────────────┘
                            ▼
                     ┌──────────────┐
                     │DockerExecutor│
                     └──────┬───────┘
                            │
                  ┌─────────▼─────────┐
                  │  Agent container  │
                  │   /workspace      │
                  └─────────┬─────────┘
                            │ local mode only
                            ▼
                  ┌───────────────────┐
                  │  Target container │
                  │ project-owned v0.1│
                  └───────────────────┘

 AgentLoop / executor / verifier ─────► RunArtifacts
                                        │
                                        ├── run.json
                                        ├── events.jsonl
                                        ├── result.json
                                        └── workspace/

 committed run artifacts ─────────────► Write-up renderer
                                        └── writeup.md
```

### 2.2 Component responsibilities

| Component | Responsibility |
| --- | --- |
| **CLI / Challenge Loader** | Validate challenge input, select a protocol adapter, construct the verifier/executor/loop, and start one run. |
| **Model Adapter** | Translate a supported provider protocol to and from FlagAgent's normalized model boundary. |
| **AgentLoop** | Own conversation state, enforce run limits, dispatch tools sequentially, and decide the terminal run outcome. |
| **Tool boundary** | Expose only `shell` and `submit_flag` to the model and validate their arguments. |
| **DockerExecutor** | Own the run-scoped Agent container and, for local mode, the run-scoped internal network and Target fixture. |
| **Verifier** | Authoritatively decide whether a submitted candidate is correct. |
| **RunArtifacts** | Persist run metadata, event trajectory, terminal result, and workspace. |
| **Write-up renderer** | Produce a human-readable summary from committed run artifacts. |

The core loop is Docker-agnostic and provider-agnostic at its interface boundaries. Concrete protocol and execution behavior live behind those boundaries rather than inside the loop.

---

## 3. Core Invariants and Trust Boundaries

These invariants define the security and behavioral identity of v0.1.0.

### 3.1 The model acts only through an allowlisted tool surface

The product-level tool surface is exactly:

```text
shell
submit_flag
```

Unknown or hallucinated tool names do not execute. Invalid arguments also do not reach the underlying executor or verifier; they are returned to the model as structured tool errors so the model can recover.

The model has no product-level path to host shell execution, the Docker daemon, arbitrary host files, provider credentials, verifier secrets, or unrelated control-plane data.

### 3.2 Untrusted execution stays inside the containment boundary

Model-generated commands are untrusted and execute inside the run-scoped Agent container rather than directly on the host.

Challenge Targets are also treated as untrusted workloads. The presence of challenge-supplied scripts, Dockerfiles, Compose files, Makefiles, or similar provisioning artifacts does not make them trusted for automatic host-level execution.

Docker is the v0.1.0 containment baseline. It is **not** treated as equivalent to a hardened virtual machine or microVM.

### 3.3 Control-side secrets remain outside Agent and Target containers

Provider credentials and the authoritative expected flag remain on the trusted control side.

They are not copied into the Agent workspace or passed to the Agent or Target containers. A credential intentionally included as challenge data is part of the challenge, not a framework secret.

### 3.4 Only the verifier establishes `solved`

A flag-shaped string in model output, shell output, logs, or a regular-expression match does not establish success.

```text
candidate
   │
   ▼
submit_flag
   │
   ▼
Verifier.check
   │
   ├── incorrect ──► continue
   │
   └── correct ────► solved
```

The verifier is the sole authority for the `solved` transition.

### 3.5 Model-visible execution is auditable

The runtime records enough normalized evidence to reconstruct:

- model responses accepted by the loop;
- requested tool calls and correlation IDs;
- which tool calls executed;
- the result returned to the model;
- flag candidates submitted;
- verifier outcomes;
- terminal status and reason.

FlagAgent does not require provider-private chain-of-thought or hidden reasoning tokens for auditability.

### 3.6 A committed terminal result is authoritative

`result.json` is the authoritative terminal result for a completed run. Human-readable summaries are derived from run artifacts and do not override it.

A missing or invalid terminal result means there is no committed terminal outcome, rather than implying `unsolved` or `error` by inference.

---

## 4. Runtime Architecture

### 4.1 Run lifecycle

One CLI invocation creates one Run, one AgentLoop, one active model adapter, and one Agent container.

Conceptually:

```text
load and validate challenge
        │
        ▼
create run metadata/artifacts
        │
        ▼
stage challenge files into workspace
        │
        ▼
prepare Docker sandbox
        │
        ├── none: Agent only
        │
        └── local: network + Target + Agent
        │
        ▼
construct initial conversation
        │
        ▼
run AgentLoop
        │
        ▼
commit result.json
        │
        ▼
best-effort sandbox cleanup
        │
        ▼
derive writeup.md
```

The loop does not require a general state-machine framework. The run is linear and bounded.

### 4.2 AgentLoop

A model turn is one invocation of the normalized model interface:

```text
Model.generate(messages, tools) -> ModelResponse
```

The loop follows this sequence:

```text
check run deadline / turn budget
        │
        ▼
call model
        │
        ▼
record model response
        │
        ├── no tool calls ─────────────► unsolved:model_stop
        │
        ▼
append assistant message to conversation
        │
        ▼
execute requested tools in declared order
        │
        ├── shell
        └── submit_flag
                 │
                 └── verified correct ─► solved:verified_flag
        │
        ▼
append correlated tool results
        │
        └──────────────────────────────► next model turn
```

Important semantics:

- the assistant message requesting tools enters conversation state before its tool results;
- multiple calls from one model response execute sequentially in normalized provider-declared order;
- all calls in that response still count as one model turn;
- tool call IDs must remain unique within the run;
- if a flag is verified as correct, the run stops immediately and later calls from the same model response are not executed;
- a normal model response with no tool calls ends the run instead of triggering an automatic reprompt;
- an unknown tool or invalid tool arguments are model-visible recoverable results, not an arbitrary execution path.

### 4.3 Terminal semantics

A committed run has one of three statuses:

| Status | Meaning |
| --- | --- |
| `solved` | The verifier accepted a submitted candidate. |
| `unsolved` | The harness terminated normally without a verified flag. |
| `error` | A known framework, provider, sandbox, verifier, tool, or serialization failure terminated the run. |

v0.1.0 uses terminal reasons including:

```text
solved:verified_flag

unsolved:model_stop
unsolved:model_turn_limit
unsolved:wall_limit

error:provider_error
error:sandbox_error
error:verifier_error
error:tool_error
error:serialization_error
```

The distinction is intentional:

```text
failed to solve
    !=
known harness failure
    !=
no committed terminal result
```

### 4.4 Time and output bounds

The run enforces independent bounds for:

- model turns;
- total run wall time;
- individual shell command time;
- model-visible tool output;
- logged tool output;
- container CPU, memory, and PID usage.

A shell command timeout is normally returned as tool evidence. If the overall run deadline is exhausted, terminal handling resolves to `unsolved:wall_limit` once control returns. Execution-phase blocking Docker operations are bounded by that same Run wall deadline, so pre-execution time is never re-granted and an already-exhausted deadline does not start fresh long timeouts to restore the sandbox — only non-blocking host hygiene runs before control returns to terminal, leaving final containment to the best-effort cleanup outside the active Run budget.

Output collection is bounded while stdout and stderr are drained. The host does not intentionally buffer an unbounded command result and truncate only after process completion.

Large files belong in the run workspace rather than in model-visible tool output.

---

## 5. Model and Tool Boundaries

### 5.1 Normalized model interface

The core runtime depends on a small protocol rather than provider-specific response objects:

```python
Model.generate(messages, tools) -> ModelResponse
```

A normalized response carries:

```text
content
tool_calls:
  - call_id
  - name
  - arguments
usage
```

This keeps provider translation outside AgentLoop and allows the loop to be tested deterministically with a scripted model.

### 5.2 Supported protocol paths

v0.1.0 exposes three CLI protocol paths:

| CLI protocol | Boundary |
| --- | --- |
| `openai-chat` | OpenAI-compatible Chat Completions |
| `openai-responses` | OpenAI Responses |
| `anthropic` | Anthropic Messages |

An adapter may target a compatible custom base URL. Compatibility at the protocol boundary does **not** imply that every provider, gateway, or model implementing a similar API has been tested.

Provider credentials remain in the trusted control process and are not injected into the execution containers.

v0.1.0 does not add a provider registry, router, automatic fallback layer, model racing, or generic capability matrix around these adapters.

### 5.3 `shell`

Conceptually:

```text
shell(command)
    ->
stdout
stderr
exit_code
timed_out
truncated
```

One real Run owns one Agent container. Each `shell` invocation starts a **fresh non-interactive process** inside that same container.

Persisted between calls:

- workspace files;
- filesystem changes inside the container;
- generated scripts and binaries;
- other run-local filesystem state.

Not guaranteed to persist between calls:

- current working directory changes made by a prior shell process;
- shell-local variables and functions;
- exported environment changes made inside that prior process;
- jobs or interactive terminal state.

A non-zero command exit is normal command evidence and does not by itself mean the tool or harness failed.

### 5.4 `submit_flag`

Conceptually:

```text
submit_flag(candidate)
    ->
correct | incorrect
```

`incorrect` is a normal candidate outcome. A verifier infrastructure failure is an error, not an incorrect flag.

The expected flag remains control-side and is not placed in the model-visible challenge trajectory.

---

## 6. Challenge Boundary

A challenge is loaded from a directory containing a `challenge.json` descriptor and optional `files/` directory.

The v0.1.0 descriptor accepts:

```text
identity
description
expected_flag
network_mode
target_context   (optional)
```

The expected flag is separated from the `ChallengeInput` passed into AgentLoop and is used to construct the trusted verifier.

Optional challenge files are staged into the writable run workspace. The runtime records a source hash when available so the run can identify the staged challenge input without exposing control-side secrets.

### Network modes

v0.1.0 supports exactly two challenge network modes:

```text
none
local
```

`external`, host networking, `container:<id>`, and arbitrary Docker network names are outside the supported v0.1.0 boundary.

#### `none`

The Agent container uses Docker's no-network mode and has no intended non-loopback challenge connectivity.

#### `local`

FlagAgent creates a run-scoped user-defined **internal bridge network**, starts the project-owned Target fixture, waits for bounded readiness, and then starts the Agent on the same network.

```text
Agent ─────► Target
  │
  └── no normal external route
```

No Target port is published to the host.

`--internal` restricts external network routing, but it is not a host-isolation guarantee. Docker documents that containers on an internal network can still communicate with the network gateway and appropriately configured host services, and the host can communicate directly with container IPs. The `local` mode should therefore be understood as **run-scoped challenge networking with restricted external routing**, not as isolation from the Docker host.

---

## 7. Execution and Containment

### 7.1 Trust model

The trusted computing base for v0.1.0 includes:

- the host kernel;
- Docker Engine and its daemon/runtime;
- the FlagAgent control process;
- the verifier;
- approved Agent and Target images/configuration.

The Agent container, model-generated commands, challenge files, and Target workload are not trusted control-plane components.

### 7.2 Agent container posture

The Agent is created with a fixed least-privilege baseline:

```text
non-root user
privileged mode not enabled
all additional Linux capabilities dropped
no-new-privileges enabled
explicit memory limit
explicit CPU limit
explicit PID limit
host networking not used
Docker socket not mounted
provider/verifier secrets not injected
run workspace is the intentional writable host bind
```

Docker's default seccomp behavior is retained; v0.1.0 does not expose a generic security-relaxation mechanism for capabilities, privileged execution, arbitrary devices, seccomp overrides, or additional host mounts.

The persisted sandbox provenance may record `security_relaxations`, but the v0.1.0 executor reports this as an empty list rather than treating relaxations as a supported feature.

### 7.3 Target posture

The local Target is a project-owned fixture, not a generic challenge-provisioning system. It is also run with a non-root user, dropped capabilities, `no-new-privileges`, explicit resource limits, no Docker socket, and no host port publishing.

FlagAgent does not automatically execute arbitrary challenge-provided container orchestration with host/Docker privileges.

### 7.4 Filesystem boundary

The run workspace is intentionally writable and bind-mounted into the Agent container.

Unrelated host paths are not mounted by default. In particular, the execution boundary does not require the Docker socket, full user home, provider credential directories, or the FlagAgent repository to be exposed to the solver.

### 7.5 Supported Docker topology

FlagAgent v0.1 supports local Docker Engine with a daemon reachable via a local socket (`unix://`, `npipe://`, `fd://`, or a direct filesystem path such as `/var/run/docker.sock`). The control process and daemon must share the same host filesystem so the Run workspace bind mount (`--mount type=bind,source=<workspace>,target=/workspace`) refers to the same path.

Remote TCP (`tcp://`) and SSH (`ssh://`) daemon endpoints are unsupported and fail closed before any Run-scoped Docker resource is created. Endpoint validation resolves the effective Docker host using Docker's documented precedence — `DOCKER_CONTEXT` overrides `DOCKER_HOST`, which overrides the active context from `docker context show` / `docker context inspect` — and inspects the endpoint host string rather than assuming a name like `default` is always local. If the endpoint cannot be reliably determined, preparation also fails closed. The explicit `--mount type=bind,...` form converts a missing source on the daemon host into a deterministic failure rather than silently creating an empty directory, but remote topology validation remains the primary control.

### 7.6 Containment limitations

Docker is a practical containment baseline for v0.1.0, not a hardened isolation boundary:

- containers share the host kernel;
- `local` internal networking still permits host/gateway communication as described above;
- CPU, memory, PID, time, and tool-output growth are bounded, but v0.1.0 does not promise a portable universal disk quota;
- the security posture assumes a correctly configured Docker host and trusted images/configuration.

Workloads requiring stronger isolation should use a stronger external sandbox boundary rather than interpreting v0.1.0 Docker containment as a microVM-equivalent guarantee.

---

## 8. Run Artifacts and Observability

Each attempt owns a run directory:

```text
runs/<run-id>/
├── run.json
├── events.jsonl
├── result.json
├── writeup.md
└── workspace/
```

### `run.json`

`run.json` is the run configuration and provenance snapshot created before active solving. It records run/challenge identity, limits, version information, and model/prompt/sandbox metadata when available.

It is not intended to become a general database or event store.

### `events.jsonl`

`events.jsonl` is the append-oriented runtime trajectory. Events carry a sequence number and capture normalized model, tool, verifier, lifecycle, error, and terminal-decision information.

A reader may ignore one incomplete trailing line after an interrupted write; corruption in the interior of the stream is not treated the same way.

The trajectory preserves the normalized result shown to the model. A larger logged shell result may also be retained when configured, but raw unbounded pre-normalization output is not required.

### `result.json`

`result.json` is the authoritative committed terminal result.

The runtime writes JSON through a temporary file and replaces the destination in the same filesystem. It does not claim stronger durability than the underlying filesystem/runtime provides.

A valid `result.json` represents a known committed terminal state. Missing or invalid terminal output is not silently interpreted as another status.

### `workspace/`

`workspace/` contains the writable state used by the Agent during the run, including staged challenge files and files produced while solving.

### `writeup.md`

`writeup.md` is generated **after** the run from `run.json`, `events.jsonl`, and `result.json`.

It is a derived human-readable summary. It is useful for inspection, but it is not authoritative over the structured artifacts from which it was produced.

---

## 9. Key Architecture Decisions and Known Limitations

| Decision | v0.1.0 choice | Consequence |
| --- | --- | --- |
| Agent orchestration | One linear `AgentLoop` | Easy to inspect and test; no planner/multi-agent specialization. |
| Tool surface | `shell` + `submit_flag` | Small authority surface; richer tools must be justified by future requirements. |
| Success authority | Trusted verifier | Prevents model self-report or flag-shaped text from becoming success. |
| Command execution | Persistent Agent container, fresh process per shell call | Files persist while shell session state does not. |
| Tool scheduling | Sequential | Deterministic ordering and immediate stop after verified success; no parallel tool execution. |
| Model integration | Small normalized boundary + three concrete protocol adapters | Core loop avoids provider objects; protocol support is not a claim of universal provider compatibility. |
| Containment | Docker on Linux | Practical and widely available, but shares the host kernel and is weaker than VM/microVM isolation. |
| Challenge networking | `none` or run-scoped `local` | No supported external/VPN challenge networking in v0.1.0. |
| Local Target | Project-owned fixture | Exercises local network flow without creating a generic untrusted provisioning framework. |
| Persistence | JSON/JSONL + filesystem workspace | Inspectable and simple; no resume database or distributed state. |
| Terminal result | Atomic file replacement | Clear committed-result boundary without introducing a database. |
| Interactive execution | Fresh non-PTY shell process | Simple/reproducible; interactive debuggers and long-lived terminal sessions are outside v0.1.0. |
| Resource control | Time/output + Docker CPU/RAM/PID limits | Bounds primary runtime resources; portable general disk quota is not guaranteed. |

The included smoke challenges exercise the harness paths and are not evidence that v0.1.0 is a benchmark of general CTF-solving capability.

---

## 10. Glossary

**AgentLoop**  
The single control loop that calls the model, dispatches tools, tracks limits, records events, and produces a terminal outcome.

**Run**  
One run-scoped solver attempt with its own Agent container, artifacts, limits, and terminal result.

**Agent**  
The run-scoped Docker container in which model-generated shell commands execute.

**Target**  
The optional project-owned challenge service used by `network_mode: local`. It is treated as untrusted workload rather than trusted control infrastructure.

**Verifier**  
The trusted control-side component that decides whether a submitted flag candidate is correct.

**Control side**  
The trusted host-side FlagAgent process and associated verifier/model credentials and orchestration state.

**Model-visible result**  
The normalized tool result returned to the model and preserved in the event trajectory.

**Committed terminal result**  
A valid `result.json` written for the run. It is the authoritative structured record of `solved`, `unsolved`, or `error`.

---

## Related documentation

For Docker behavior referenced by the containment model, see the official documentation for [internal networks](https://docs.docker.com/reference/cli/docker/network/create/#network-internal-mode---internal), [seccomp](https://docs.docker.com/engine/security/seccomp/), and [resource constraints](https://docs.docker.com/engine/containers/resource_constraints/).
