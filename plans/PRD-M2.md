# FlagAgent v0.1.0 — PRD M2: Prove Usefulness

> **Status:** DRAFT — requires human approval before implementation  
> **Release:** v0.1.0  
> **Milestone:** M2 — Prove Usefulness  
> **Source of truth:** `plans/Flagagent-v0.1.0.md`  
> **Prerequisites:** M0 — Prove the Loop; M1 — Prove Containment  
> **Development philosophy:** KISS, YAGNI, working software first, minimum sufficient functionality, executable evidence

---

## 0. Contract

M2 is the final implementation milestone for FlagAgent v0.1.0.

M0 proved the deterministic agent loop.  
M1 proved contained Docker execution.  
M2 must prove that the same architecture is **actually usable with a real model on small CTF challenges**.

The release objective is deliberately modest:

> A user can clone FlagAgent, install it, configure a supported real model endpoint, run a documented challenge, observe real tool use inside the sandbox, receive a verifier-backed terminal outcome, and inspect useful Run artifacts.

M2 is not required to make FlagAgent complete, benchmark-leading, production-hardened, or universally compatible.

Use this precedence:

```text
latest explicit human decision
→ frozen Concept
→ approved PRD-M2
→ AGENTS.md
→ implementation plan
→ implementation
```

This PRD defines required behavior and evidence.  
Implementation mechanisms belong in the separate Plan.

### M2 scope filter

A capability belongs in M2 only if it is required for at least one of:

1. a real model completing the existing FlagAgent loop;
2. a small representative real CTF smoke evaluation;
3. a clean-clone user successfully following the README.

Otherwise defer it.

---

## 1. Release Outcome

FlagAgent v0.1.0 is ready to release when the following end-to-end journey works:

```text
clean clone
→ install dependencies
→ configure a supported model endpoint
→ prepare the sandbox
→ run FlagAgent on a supported challenge
→ real model calls shell
→ shell executes through M1 containment
→ model submits candidate with submit_flag
→ trusted verifier determines outcome
→ Run ends as solved / unsolved / error
→ user can inspect Run artifacts and simple write-up
```

The release must prove a **working vertical slice**, not feature completeness.

---

## 2. Scope

M2 adds only the minimum functionality required to prove usefulness:

```text
minimal runnable CLI
real model protocol adapters
OpenAI-compatible Chat Completions
OpenAI-compatible Responses
Anthropic-compatible Messages
OpenRouter through OpenAI-compatible configuration
one small versioned solver prompt
challenge context input
minimum-sufficient CTF tools for the frozen smoke set
small real smoke evaluation
simple deterministic write-up artifact
README with clean-clone install/use path
release evidence
```

Existing M0/M1 contracts remain authoritative unless this PRD explicitly changes them.

In particular:

- one Run remains one attempt;
- one active model per Run;
- one linear conversation;
- only `shell` and `submit_flag` are product tools;
- the verifier alone establishes `solved`;
- correct flag terminates immediately;
- model confidence never establishes success;
- real shell work remains inside the M1 Docker boundary;
- no product multi-agent or planner/executor architecture is introduced.

---

## 3. Explicit Non-Goals

M2 does **not** require:

- multi-agent solving;
- planner/executor architecture;
- model racing;
- provider routing or fallback;
- automatic provider discovery;
- a provider capability matrix;
- support for every OpenAI-compatible vendor;
- streaming UI;
- provider-managed conversation state;
- web-search tool;
- PTY or persistent interactive shell;
- debugger-specific APIs;
- VPN integration;
- CTFd/platform integration;
- MCP product runtime;
- automatic challenge retries;
- best-of-N scheduling;
- checkpoint/resume;
- full Kali/CTF kitchen-sink image;
- automatic tool installation;
- advanced write-up generation;
- LLM-generated post-run reports;
- pricing engine;
- usage dashboard;
- benchmark framework or leaderboard;
- PyPI publication as a release prerequisite;
- TUI or setup wizard;
- plugin ecosystem;
- remote/cloud sandbox;
- database-backed Run storage;
- generalized challenge orchestration.

These may be considered after v0.1.0 based on actual usage evidence.

---

## 4. Minimal User Interface

M2 provides one small command-line path for running FlagAgent.

The public interface should conceptually be:

```text
flagagent run ...
```

Exact argument names and internal CLI structure belong in the implementation plan.

The supported path must allow the user to provide, directly or through a small configuration surface:

- challenge name/identifier;
- challenge description;
- challenge workspace/files when applicable;
- target endpoint/context when applicable;
- network mode required by the supported challenge;
- model protocol;
- model name;
- API base URL when needed;
- credentials through environment/configuration that is not committed to the repository.

The CLI must not become a provider manager, profile system, interactive wizard, TUI, or configuration framework.

Invalid user configuration must fail clearly before model execution when practical.

---

## 5. Real Model Boundary

The existing normalized `Model`/`ModelResponse` boundary remains the contract seen by `AgentLoop`.

Provider-specific request/response objects MUST NOT leak into the core loop.

M2 supports two protocol families:

```text
OpenAI-compatible
├── /v1/chat/completions
└── /v1/responses

Anthropic-compatible
└── Messages
```

OpenRouter is supported through the OpenAI-compatible path using configuration such as base URL, API key, and model name. It must not require a separate general provider architecture unless implementation evidence proves that unavoidable.

### Required normalized behavior

Each real adapter must map the selected protocol into existing FlagAgent semantics for:

- assistant text content;
- ordered tool calls;
- tool call ID;
- tool name;
- tool arguments;
- finish/stop condition needed by the core;
- provider usage when available;
- provider failures.

Tool execution and verifier semantics remain provider-independent.

### Compatibility scope

M2 proves protocol behavior, not universal vendor compatibility.

It is acceptable for v0.1.0 documentation to state which concrete endpoints/models were actually tested.

A single designated provider/model combination is sufficient for the authoritative real CTF release smoke.

---

## 6. Provider Errors, Retries, and Usage

Provider failure must continue to produce the existing provider-error terminal semantics rather than an invented model result.

M2 MUST NOT introduce a generic retry framework.

If the selected SDK/client performs automatic retries, the implementation must understand and intentionally configure or accept that behavior so retry semantics are not accidental.

The Run should record provider usage fields that are actually returned and can be normalized without inventing values.

M2 does not implement:

```text
provider price catalogs
cost estimation across vendors
historical pricing
billing dashboards
```

If a provider does not expose a particular usage field, absence is preferable to fabrication.

Secrets must never be written into Run artifacts, model-visible workspace, logs, or write-up.

---

## 7. Solver Prompt

M2 uses one project-owned solver prompt for the v0.1.0 baseline.

The prompt must be:

- small;
- versioned;
- deterministic as an artifact;
- hashed/identified in Run provenance;
- provider-independent where practical.

The prompt should communicate only what the baseline agent actually needs:

```text
authorized CTF role/context
objective: obtain and submit the challenge flag
sandbox/workspace context
available tools: shell + submit_flag
non-interactive command expectation
challenge-specific context
verifier-backed success semantics
```

The prompt should encourage evidence-driven iteration through tool observations without prescribing a large planner, reflection protocol, category playbook, or long solving methodology.

Challenge-specific data belongs in challenge context rather than by creating a new global solver prompt for every challenge.

Correct `submit_flag` verification must still stop the Run immediately; no extra model call is required to generate a report.

---

## 8. Challenge Input and Smoke Set

M2 uses a **small frozen release smoke set** of simple, legally usable CTF challenges.

The set may use suitable PicoCTF-style challenges or other lightweight CTF sources, subject to their actual availability, license/redistribution constraints, and reproducible setup.

Do not build a benchmark platform.

The initial target is a small handful, preferably covering different execution shapes such as:

```text
simple file/shell challenge
simple TCP/netcat-style challenge
one modest multi-step challenge
```

The exact challenges and categories are selected during implementation planning/reconnaissance and frozen before the final M2 release gate.

### Smoke-set requirements

Each selected challenge must have:

- a stable challenge description;
- deterministic trusted verifier expectation;
- reproducible local fixture/setup or documented external dependency;
- known network requirement;
- bounded execution suitable for the existing Run limits;
- no requirement to weaken M1 containment unless explicitly approved;
- no hidden dependency on tools absent from the release image.

The smoke set is release evidence, not a claim of broad CTF coverage.

---

## 9. Minimum-Sufficient CTF Tooling

M2 extends the M1 Agent image only as needed to run the frozen smoke set.

The goal is **minimum sufficient**, not smallest possible and not kitchen-sink completeness.

A lightweight baseline may reasonably include common shell/network/file-analysis utilities such as:

```text
Bash/core utilities
Python
file
strings/binutils
curl or equivalent HTTP client
netcat-compatible client
OpenSSL
common archive utilities
```

This list is informative, not a mandatory package manifest.

Additional tools such as pwntools, cryptographic libraries, GDB, radare2, Sage, angr, or similar MUST be added only when a selected smoke challenge demonstrates a concrete need.

Do not add tools merely because they are common in mature CTF distributions.

The implementation plan must record why each non-trivial M2 tool dependency is needed by the frozen smoke set.

---

## 10. Real Smoke Evaluation

M2 must run real model attempts against the frozen smoke set.

A model failing to solve a challenge is not automatically a framework failure.

For every attempt, FlagAgent must still produce a valid trajectory and trusted terminal outcome:

```text
solved
unsolved
error
```

### Release evidence target

The release should demonstrate that the selected real provider/model can solve **multiple simple representative challenges** end-to-end when practical.

The preferred target is:

```text
at least 2 distinct frozen smoke challenges
solved at least once
```

This is evidence of usefulness, not a benchmark score.

If one challenge remains unsolved while the framework behavior is correct, that alone must not trigger architecture expansion. The final release decision remains human-controlled and should consider the complete evidence.

A small number of independent attempts may be run manually for evaluation.

M2 does not add automatic retry/best-of-N product behavior.

One Run remains one attempt.

---

## 11. Simple Human-Readable Write-up

M2 adds a simple human-readable Run summary, preferably:

```text
writeup.md
```

This file is a derived convenience artifact.

Authoritative facts remain in the structured Run artifacts such as:

```text
run.json
events.jsonl
result.json
workspace/
```

The write-up may summarize:

- challenge identity;
- terminal status/reason;
- model/protocol identity;
- prompt identity/hash;
- important shell/tool actions from the recorded trajectory;
- submitted/verified outcome;
- basic timing/usage facts when available.

The write-up MUST NOT require another LLM call after the Run.

It does not need:

```text
polished exploit narrative
screenshots
HTML
MITRE mapping
executive report
advanced formatting
```

If a faithful useful summary cannot be deterministically derived from existing artifacts, prefer a smaller write-up rather than inventing facts.

---

## 12. README and Clean-Clone Experience

M2 MUST replace the placeholder README with a practical v0.1.0 README.

The README is the human entry point, not a copy of the Concept or PRDs.

It must clearly cover:

```text
what FlagAgent is
v0.1.0 status and scope
authorized-use/security notice
requirements
source installation
model/API configuration
sandbox preparation
one copy-paste Quick Start
how to run a supported challenge
how to provide a simple custom challenge
where Run artifacts are written
what solved / unsolved / error mean
major v0.1.0 limitations
development/test commands
license
```

The README must not advertise unsupported future capabilities.

### README acceptance contract

A clean-clone user following only the documented supported path must be able to:

```text
install FlagAgent
configure one documented real model endpoint
prepare the sandbox
run the documented example
locate and understand the resulting artifacts
```

The README may link to Concept/PRDs for deeper architecture information rather than duplicating them.

---

## 13. Research, Source Reuse, and Provenance

PRD-M2 does not prescribe external project architecture.

During planning and implementation, source/reference research is expected to answer concrete implementation questions rather than justify feature expansion.

For substantial new M2 mechanisms, the implementation process should compare existing relevant implementations before unnecessary reinvention.

Actual source adaptation is allowed only when:

- the source license is compatible with the current FlagAgent source-adaptation policy;
- exact repository/revision/path provenance is known;
- adaptation is materially simpler or more robust than an independent implementation;
- the adapted code does not import unnecessary architecture.

Actual adaptation must follow `AGENTS.md` provenance and notice requirements before the adaptation commit.

Reading source or learning a concept does not itself require a third-party notice.

No requirement exists to copy code merely to prove that research occurred.

---

## 14. Acceptance Criteria

**AC-M2-01 — Minimal CLI**  
A clean installed checkout exposes one documented command path that starts a FlagAgent Run without requiring the user to write a Python harness.

**AC-M2-02 — OpenAI Chat Completions**  
A deterministic/controlled adapter test proves OpenAI-compatible Chat Completions responses and tool calls normalize into the existing FlagAgent model boundary.

**AC-M2-03 — OpenAI Responses**  
A deterministic/controlled adapter test proves OpenAI-compatible Responses content/tool calls normalize into the same model boundary.

**AC-M2-04 — Anthropic Messages**  
A deterministic/controlled adapter test proves Anthropic-compatible Messages tool-use/content normalize into the same model boundary.

**AC-M2-05 — OpenRouter compatibility**  
A documented OpenRouter configuration uses the OpenAI-compatible implementation path rather than requiring a duplicate AgentLoop/provider architecture.

**AC-M2-06 — Core independence**  
`AgentLoop` remains free of provider-specific request/response types and provider-selection logic.

**AC-M2-07 — Provider failure**  
A provider/API failure produces the existing provider-error semantics without executing fabricated tool calls or leaking credentials.

**AC-M2-08 — Prompt provenance**  
A real Run records enough information to identify the exact solver prompt version/hash used.

**AC-M2-09 — Real tool trajectory**  
At least one real-model Run demonstrates model-generated `shell` calls executing through the existing M1 Docker boundary and returning normalized observations.

**AC-M2-10 — Verifier authority**  
A real-model candidate becomes `solved` only when the trusted verifier accepts it; an incorrect candidate remains non-terminal/recoverable according to existing semantics.

**AC-M2-11 — Frozen smoke fixtures**  
The final smoke set is reproducible, has deterministic trusted expected flags, and documents its required network/tool environment.

**AC-M2-12 — Useful solve evidence**  
The release evidence includes successful verifier-backed solves on multiple simple representative smoke challenges when practical, with at least one successful real end-to-end solve as the hard minimum.

**AC-M2-13 — Failure is valid evidence**  
A model that fails to solve a smoke challenge still yields a valid `unsolved` or appropriate `error` outcome without framework corruption.

**AC-M2-14 — Minimum-sufficient tooling**  
Every non-trivial tool added to the M2 sandbox is justified by a frozen smoke challenge or documented baseline need; no full CTF toolkit is added speculatively.

**AC-M2-15 — Simple write-up**  
A completed Run produces a deterministic human-readable summary without making an additional model request or contradicting structured artifacts.

**AC-M2-16 — Secret hygiene**  
Provider credentials are absent from Agent workspace, model-visible output, structured Run artifacts, and write-up.

**AC-M2-17 — README clean-clone path**  
The documented Quick Start succeeds from a clean checkout on the supported reference environment using one documented real provider/model configuration.

**AC-M2-18 — Regression safety**  
All M0 and M1 tests remain green and M2 does not weaken the frozen loop, verifier, containment, or cleanup contracts.

---

## 15. Verification and Release Gate

The implementation plan may add focused provider/CLI/smoke test groups, but M2 must preserve the existing project verification baseline.

At minimum the release gate includes:

```bash
uv lock --check
uv run pytest
uv run ruff check .
uv run ruff format --check .
git diff --check
```

Docker-backed M1 integration tests remain part of the required gate.

M2 additionally requires:

- build/use of the final supported sandbox image;
- controlled tests for each supported protocol adapter;
- at least one documented real-model end-to-end solve;
- execution of the frozen smoke set according to the release evidence policy;
- clean-clone README verification;
- no committed secrets;
- final `flagagent-gate` review.

Real provider tests that consume paid API calls may be separated from ordinary deterministic unit/integration tests, but they are mandatory for the authoritative M2 release evidence.

---

## 16. M2 Evidence

Keep durable evidence short.

A release evidence artifact such as:

```text
plans/M2-EVIDENCE.md
```

SHOULD record:

```text
tested FlagAgent commit
reference host / Docker facts
sandbox image identity
solver prompt identity/hash
tested protocol implementations
reference provider + model used for real smoke
smoke challenge identities
attempt outcomes
successful verified solves
verification commands/results
README clean-clone result
known limitations
source-adaptation/provenance decisions if any
flagagent-gate verdict
human release decision
```

Do not turn the evidence file into a benchmark paper.

API keys, full secret-bearing requests, and sensitive provider headers must never be recorded.

---

## 17. Definition of Done

M2 is complete when:

- FlagAgent can be run through the documented CLI;
- at least one real provider/model path completes the full model → tool → verifier loop;
- OpenAI-compatible Chat Completions, OpenAI-compatible Responses, and Anthropic-compatible Messages are implemented at the normalized model boundary;
- OpenRouter works through OpenAI-compatible configuration;
- the solver prompt is small, versioned, and recorded;
- the final smoke set is frozen and reproducible;
- the sandbox contains only minimum-sufficient tooling;
- real smoke evidence demonstrates actual usefulness;
- structured artifacts remain authoritative;
- a simple deterministic write-up is produced;
- README enables the documented clean-clone path;
- M0 and M1 contracts remain green;
- no unrequired v0.1.0 architecture was added;
- no known material release blocker remains.

---

## 18. v0.1.0 Release Gate

After M2 PASS, there is no M3.

The human release decision should answer:

> Can another user reasonably clone this repository, follow the README, configure a supported real model, run the baseline system on a documented CTF challenge, obtain a truthful verifier-backed outcome, and inspect useful evidence?

If yes, FlagAgent v0.1.0 is releasable even if it lacks advanced features.

The following are **not** reasons by themselves to block v0.1.0:

```text
not all providers tested
not all smoke attempts solved
no PTY
no multi-agent
no advanced CTF image
no TUI
no PyPI release
no benchmark leaderboard
no advanced write-up
```

Release should be blocked only by failures that make the supported v0.1.0 journey incorrect, unusable, misleading, unsafe relative to its declared boundary, or irreproducible.

---

## 19. Deferred After v0.1.0

Future work may consider, based on real evidence:

```text
additional providers/models
provider routing/fallback
PTY and persistent interactive sessions
debugger-specific interfaces
larger CTF tool images
on-demand tool installation
external/VPN challenge networking
CTFd/platform integrations
MCP
multi-agent/planner architectures
automatic retry / multiple-attempt orchestration
resume/checkpoint
advanced write-ups/reports
cost/pricing dashboards
benchmark suites
TUI
PyPI/release automation
remote/cloud sandboxes
```

None of these are required to prove the first usable FlagAgent release.

---

## 20. Planning Handoff

The Plan Agent reading this PRD should optimize for:

```text
smallest implementation that satisfies the acceptance criteria
reuse of existing M0/M1 contracts
source-level research before substantial reinvention
small reviewable implementation batches
deterministic tests before paid real-model evidence
no speculative framework building
```

The Plan must explicitly separate:

```text
requirements from this PRD
implementation choices
source/reference findings
deferred opportunities
```

A technically elegant expansion that is not required by this PRD is not a reason to expand M2.

**The priority is to ship a working FlagAgent v0.1.0.**
