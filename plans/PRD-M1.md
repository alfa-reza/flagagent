# FlagAgent v0.1.0 — PRD M1: Prove Containment

> **Status:** DRAFT — human approval required before implementation  
> **Milestone:** M1 — Prove Containment  
> **Source of truth:** `plans/Flagagent-v0.1.0.md`  
> **Previous gate:** M0 — Prove the Loop  
> **Reference host:** local Linux, Docker Engine 29.7.2, rootful  
> **Philosophy:** KISS, YAGNI, evidence before abstraction, release first

## 0. Contract

M1 changes one major thing:

> Replace M0's fake shell executor with real Docker-contained execution **without redesigning the M0 AgentLoop**.

This PRD defines required behavior and evidence, not detailed implementation.

Use this precedence:

```text
latest human decision
→ frozen Concept
→ approved PRD-M1
→ AGENTS.md
→ implementation
```

A feature belongs in M1 only when the Concept requires it, containment cannot be honestly demonstrated without it, or a concrete failure mode justifies it. Otherwise defer it.

---

## 1. Goal

M1 must prove that the same deterministic FlagAgent core can execute real `shell` calls inside one controlled Docker Agent container while preserving M0 semantics.

M1 PASS requires evidence that:

- commands execute through the sandbox boundary;
- one Agent container belongs to one Run;
- filesystem state persists across fresh shell calls;
- shell-local state is not relied on;
- timeouts leave no invocation process tree running;
- output collection is bounded;
- memory/CPU/PID limits are applied;
- default containment settings are present;
- Docker socket, control secrets, and unrelated host mounts are absent;
- `none` and `local` networking behave as intended;
- Run-owned Docker resources are identifiable and cleanable;
- sandbox failures remain distinct from command failures.

A real LLM and real CTF solve are not required.

---

## 2. Scope

M1 adds only:

```text
Docker-backed shell executor
one minimal project-owned Agent image
one Agent container per Run
resource/security defaults
none + local networking
one tiny project-owned TCP target fixture
sandbox_error
sandbox provenance
targeted cleanup
Docker integration tests
```

Keep M0 contracts for model/tool ordering, verifier authority, persistence roles, limits, and terminal semantics.

### Non-goals

Do not add for M1:

```text
real provider/model
full CTF tooling
PTY/persistent terminal
planner/multi-agent
parallel tools
resume/checkpoint
external Internet mode
VPN / CTFd / platform integration
MCP runtime
Docker Compose / Docker SDK
multiple sandbox backends
Podman/Kubernetes/SSH
rootless support
custom images or arbitrary Docker flags
generic security-relaxation framework
AppArmor/SELinux management
custom seccomp profiles
disk/blkio/tmpfs/telemetry subsystems
minimum Docker-version matrix
GitHub Actions as the authoritative M1 gate
```

---

## 3. Docker Boundary

The authoritative M1 gate runs on the local reference host.

Docker Engine 29.7.2 is the **tested reference**, not the declared minimum supported version.

Docker control remains trusted-side.

Requirements:

- use Docker CLI, not Docker SDK;
- construct Docker commands as subprocess argument vectors;
- do not use `shell=True` for Docker control commands;
- never expose Docker socket/daemon credentials to Agent or Target;
- keep `AgentLoop` Docker-agnostic;
- do not build a generic executor registry for hypothetical backends.

Docker-control failures are framework diagnostics, not raw shell results for the model.

---

## 4. Agent Image and Filesystem

Development image:

```text
flagagent-sandbox:dev
```

Baseline:

```text
base image: Ubuntu 24.04 LTS
user: agent (non-root)
working directory: /workspace
shell: /bin/bash
```

M1 image is intentionally minimal. CTF tooling is deferred.

M1 does not require `sudo`.

The container writable layer and `/tmp` may remain normally writable.

The only intended writable host bind mount is:

```text
runs/<run-id>/workspace → /workspace
```

Do not mount the Docker socket, host home, FlagAgent repository, donors, `.git`, credential directories, or unrelated host paths.

M1 uses controlled test fixtures; generalized hostile challenge-ingestion semantics are deferred.

Record the resolved image identity used by the M1 gate.

---

## 5. Shell Semantics

One real Run owns one Agent container. The container lives for the Run.

Each `shell(command)` creates a fresh non-interactive process in that same container:

```text
/bin/bash -lc <command>
cwd = /workspace
```

Required behavior:

- files written by call A are visible to call B;
- call B starts again at `/workspace`;
- prior `cd`, exported variables, shell functions, and jobs are not persistence contracts;
- stdout/stderr remain separate;
- non-zero exit is normal tool evidence;
- timeout is normal tool evidence when the sandbox remains healthy.

Do not add PTY/session/process-handle APIs.

---

## 6. Bounded Execution

Keep M0 defaults:

```text
command timeout = 60 s
model-visible output = 16 KiB per stream
logged output = 64 KiB per stream
```

Effective command timeout remains bounded by remaining Run wall time.

### Timeout

After a shell timeout returns:

> No process created by that invocation may remain running.

Killing only the local `docker exec` client while leaving the container process alive is a failure.

The implementation mechanism is not prescribed here.

### Output

Output MUST be bounded while being collected.

Do not buffer arbitrary command output in host memory and truncate afterward.

After the retained limit is reached, excess output may be drained and discarded so execution can finish without unbounded memory growth.

The model receives the existing M0 normalized/truncated result.

---

## 7. Default Containment

Agent defaults:

```text
privileged = false
user = non-root
Docker socket = absent
host network = false
no-new-privileges = true
Docker default seccomp = not disabled
extra host devices = none
unrelated host mounts = none
control environment = not inherited wholesale
```

M1 SHOULD use `cap-drop=ALL` because the minimal fixture should need no Linux capabilities.

If the controlled fixture proves a capability is genuinely required, propose the smallest exception for human approval. Do not build a generic capability framework.

AppArmor/SELinux management is out of scope.

Environment passed to the Agent is explicitly constructed.

---

## 8. Resource Limits

Agent defaults:

```text
memory = 2 GiB
cpus = 2
pids = 256
```

These are containment limits, not performance targets.

Do not add swap-policy, CPU-share, block-I/O, disk-quota, or continuous-stats subsystems.

Trusted-side Docker evidence must confirm the required limits are applied on the reference host.

If a required containment setting cannot be applied, M1 fails instead of silently weakening it.

---

## 9. Networking and Target Fixture

M1 implements only:

```text
none
local
```

`external` is deferred until M2 if a real challenge requires it.

### `none`

Use Docker no-network mode. The Agent receives no intended non-loopback connectivity.

### `local`

Create one Run-scoped user-defined **internal** Docker bridge containing:

```text
Agent
Target
```

Target alias:

```text
target
```

Do not publish the Target port to the host for M1.

Required claim:

```text
Agent ↔ intended Target
no normal external egress
```

No automatic Internet fallback.

Do not accept `host`, `container:<id>`, arbitrary network names, or arbitrary Docker network flags from untrusted input.

### Target

Use one tiny project-owned TCP service that returns a deterministic marker such as:

```text
flagagent-target-ok
```

It is only a networking fixture, not a Target framework or CTF.

The Target must be non-privileged, have no Docker socket/control secrets/host mounts, use bounded resources, and join only the intended network.

Readiness is bounded and deterministic.

Target preparation failure is `sandbox_error`.

---

## 10. Lifecycle and Cleanup

Run-owned resources use identifiable names/labels.

Minimum labels:

```text
flagagent.managed=true
flagagent.run_id=<run-id>
flagagent.role=<agent|target>
flagagent.version=0.1.0
```

Run-created networks also carry Run ownership labels.

Lifecycle:

```text
prepare sandbox
→ run AgentLoop
→ commit terminal result
→ best-effort targeted cleanup
```

Cleanup may remove only known Run-owned resources.

Allowed: targeted removal by owned ID/name/label.

Forbidden:

```text
docker system prune
docker container prune
broad unrelated cleanup
```

M1 must support discovery/reporting of owned orphan resources.

Do not automatically delete arbitrary old orphans at startup.

A cleanup failure **after** a result is committed does not rewrite that result. Record cleanup failure separately.

---

## 11. Sandbox Errors

M1 uses terminal reason:

```text
sandbox_error
```

Examples:

- Docker unavailable during active sandbox preparation;
- container create/start failure;
- network create/connect failure;
- Target start/readiness failure;
- required containment setting cannot be applied;
- Agent container disappears/becomes unusable;
- `docker exec` cannot run because the owned sandbox is gone.

These remain normal shell evidence when the sandbox survives:

```text
exit 1
exit 127
command timeout
resource-killed command with usable container
```

Invalid tool arguments remain the M0 recoverable path and never reach Docker.

Do not add a generic retry framework.

---

## 12. Persistence and Provenance

Keep M0 artifact roles.

`run.json` adds only normalized containment configuration needed to understand the Run, such as:

```text
sandbox backend
image reference
network mode
memory/cpu/pids limits
container user
security relaxations (normally empty)
Docker Engine observation
rootful/rootless observation
```

Do not dump full `docker inspect`.

Runtime identities resolved after immutable metadata—container ID, network ID/name, resolved image ID—may be recorded in one small sandbox lifecycle event.

Avoid a large sandbox event taxonomy.

---

## 13. Donor and Research Rules

Before invasive implementation, coding agent must inspect the local canonical donors for concrete M1 questions:

```text
../donors/ctf-agent
../donors/mini-swe-agent
../donors/nyuctf_agents
../donors/pi
```

Priority:

- **ctf-agent:** Docker sandbox, file/environment handling, execution, lifecycle, cleanup; reject swarm/coordinator complexity.
- **mini-swe-agent:** Docker/environment boundary and command semantics.
- **nyuctf_agents:** relevant CTF execution/container patterns; reject planner/executor scope.
- **pi:** consult only for existing tool/result boundary semantics when needed.

External projects/papers—BoxPwnr, CAI, CTFusion, SWE-ReX, `ctf-skills`, CyBench, EnIGMA—are targeted research references, not feature checklists.

BoxPwnr remains research-only under current license policy.

Actual donor source adaptation follows `AGENTS.md` provenance/license rules. Concept-only learning does not create fake license commits.

---

## 14. Acceptance Criteria

**AC-M1-01 — Docker-backed solved Run**  
The existing deterministic AgentLoop completes a verifier-confirmed `solved` Run using the real Docker shell executor.

**AC-M1-02 — Sandbox execution**  
Model-controlled shell commands execute through the owned Agent container, not intentionally through a host shell path.

**AC-M1-03 — One Agent container per Run**  
The Run owns one identifiable Agent container with Run-specific identity/labels.

**AC-M1-04 — Process/filesystem semantics**  
Filesystem changes persist across calls; `cd`/exported shell state does not.

**AC-M1-05 — Timeout termination**  
A timed-out invocation leaves no process from that invocation running afterward.

**AC-M1-06 — Bounded output**  
Very large stdout/stderr does not cause unbounded host buffering and still produces the M0 normalized result.

**AC-M1-07 — Resource limits**  
Trusted-side Docker evidence confirms 2 GiB memory, 2 CPU, and 256 PID limits.

**AC-M1-08 — Security posture**  
Trusted-side evidence confirms non-root, non-privileged, `no-new-privileges`, seccomp not disabled, and the adopted capability posture.

**AC-M1-09 — Control isolation**  
Agent cannot see Docker socket, a dummy control secret, or an unrelated temporary host marker/path.

**AC-M1-10 — Mount boundary**  
Only intended Run mounts are present; project/donor/home/credential paths are absent.

**AC-M1-11 — `none` network**  
Agent in `none` mode has no intended non-loopback network connectivity.

**AC-M1-12 — `local` network**  
Agent reaches the TCP Target via `target`; Docker evidence confirms the Run network is internal and scoped.

**AC-M1-13 — Target posture**  
Target has no privileged mode, Docker socket, control secret, or host mount and has bounded resources.

**AC-M1-14 — Error distinction**  
Sandbox infrastructure failure becomes `error/sandbox_error`; ordinary non-zero shell exit does not.

**AC-M1-15 — Targeted cleanup**  
Normal completion removes only Run-owned Agent/Target/network resources.

**AC-M1-16 — Orphan discovery**  
Simulated leftover Run resources are discoverable via FlagAgent ownership labels without broad prune operations.

**AC-M1-17 — Cleanup failure semantics**  
Post-result cleanup failure is recorded without rewriting the committed primary result.

**AC-M1-18 — Provenance**  
Artifacts/evidence identify the sandbox configuration and resolved image/runtime identity used by the Run.

---

## 15. Verification Gate

Docker integration tests should use a `docker` pytest marker.

M1 PASS requires, on the reference host:

```bash
uv lock --check
uv run pytest -m "not docker"
uv run pytest -m docker
uv run ruff check .
uv run ruff format --check .
git diff --check
```

The M1 gate must also successfully build/use `flagagent-sandbox:dev`.

Docker tests may skip when Docker is unavailable during ordinary development, but the authoritative M1 gate must fail if Docker prerequisites are unavailable.

`flagagent-gate` supplements deterministic evidence; it does not replace tests.

---

## 16. Definition of Done

M1 is done when:

- all M0 behavior remains green;
- all applicable M1 acceptance criteria pass locally;
- AgentLoop remains Docker-agnostic;
- no real provider/model or full CTF toolchain was added;
- no speculative sandbox/plugin/network framework was introduced;
- no donor repository was modified;
- any adapted donor source has required provenance;
- Run-owned Docker resources are cleaned or explicitly identified;
- no known material containment invariant is violated.

A short `plans/M1-EVIDENCE.md` SHOULD record the tested commit, Docker/image identity, verification results, donor SHAs/paths actually inspected, reuse decisions, known limitations, gate verdict, and human PASS decision.

Do not turn the evidence file into another PRD.

---

## 17. Deferred to M2 or Later

```text
real provider/model
solver prompt
full CTF tool set
external Internet mode
representative real CTF smoke set
root/sudo/capability relaxations
PTY/debugger/session tooling
provider retry policy
public CLI UX
package publishing
minimum Docker compatibility matrix
multi-agent/planner architecture
```

Add these only when M2 evidence demonstrates the need.

---

## 18. Informative Research Basis

The PRD shape follows current coding-agent guidance that favors explicit goals, constraints, boundaries, and verifiable completion instead of over-specified implementation steps.

The scope is additionally informed by:

- Docker official security/resource/network documentation;
- BoxPwnr's evolution and later fixes for executor bypass, process-tree termination, and massive command output;
- `ctf-agent`'s isolated CTF solver containers;
- CyBench's command-execution environment as a meaningful CTF-agent primitive;
- *Hacking CTFs with Plain Agents*, showing strong results without heavy harness engineering;
- EnIGMA, showing interactive tools are useful later but not required to prove M1 containment;
- *Autonomous LLM Agents & CTFs: A Second Look*, supporting strong simple/general-purpose baselines before adding orchestration.

These sources inform scope. The frozen Concept and approved PRD remain normative.
