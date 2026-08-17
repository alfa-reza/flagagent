# FlagAgent

FlagAgent is a small single-agent harness for authorized CTFs, security labs, benchmarks, and sandboxed experiments. A model can inspect a challenge through `shell`, submit a candidate through `submit_flag`, and is marked solved only when the trusted verifier accepts it.

## v0.1.0 status

M0 and M1 are complete. M2 implementation provides the first usable CLI and real-provider adapters for:

- OpenAI-compatible Chat Completions
- OpenAI-compatible Responses
- Anthropic-compatible Messages
- OpenRouter through the OpenAI-compatible Chat Completions path

This release is a narrow baseline, not a full CTF distribution or a perfect security boundary. Docker is the containment baseline and shares the host kernel.

Use FlagAgent only against systems and challenges you are authorized to test.

## Requirements

The supported reference environment is:

- Linux
- Python 3.12 or newer
- `uv`
- Docker Engine with the Docker CLI

The release gate is tested with Docker Engine 29.7.2 on Linux. Docker Desktop, Podman, macOS, and Windows are not part of the v0.1.0 containment claim.

## Install from a checkout

```bash
uv sync
```

Build the project-owned sandbox and local target images:

```bash
docker build -t flagagent-sandbox:dev images/sandbox
docker build -t flagagent-target:dev images/target
```

The sandbox runs as the non-root `agent` user. The frozen smoke set uses only the Ubuntu base utilities, Python, and `netcat-openbsd`.

## Configure a provider

OpenAI Chat Completions and Responses use `OPENAI_API_KEY` by default:

```bash
export OPENAI_API_KEY='your-key'
```

Anthropic uses `ANTHROPIC_API_KEY` by default:

```bash
export ANTHROPIC_API_KEY='your-key'
```

OpenRouter uses the OpenAI-compatible Chat Completions adapter:

```bash
export OPENROUTER_API_KEY='your-key'
```

```bash
uv run flagagent run \
  --challenge challenges/layered-file \
  --protocol openai-chat \
  --model openai/gpt-4o-mini \
  --api-base https://openrouter.ai/api/v1 \
  --api-key-env OPENROUTER_API_KEY
```

Use a direct OpenAI endpoint by omitting `--api-base` and `--api-key-env`, or select another supported protocol:

```bash
uv run flagagent run \
  --challenge challenges/layered-file \
  --protocol openai-responses \
  --model your-model
```

```bash
uv run flagagent run \
  --challenge challenges/layered-file \
  --protocol anthropic \
  --model your-model
```

Provider credentials stay on the trusted control side. They are not passed to the Agent or Target containers and are not written to Run artifacts.

## Frozen smoke challenges

The repository contains two project-owned fixtures.

### Layered file

```bash
uv run flagagent run \
  --challenge challenges/layered-file \
  --protocol openai-chat \
  --model your-model
```

The Agent receives one evidence file in its writable workspace. The challenge requires two base64 decoding steps and a verifier-backed submission. It uses `network_mode: none`.

### Local marker

```bash
uv run flagagent run \
  --challenge challenges/local-marker \
  --protocol openai-chat \
  --model your-model
```

The Agent can connect to the project-owned audited target at `target:9999` through a Run-scoped internal Docker network. The target returns the deterministic M1 marker `flagagent-target-ok`. It uses `network_mode: local`.

The smoke fixtures are release evidence, not a claim of broad CTF coverage.

## Custom file challenges

A custom challenge is a directory containing `challenge.json` and, optionally, a `files/` directory:

```text
my-challenge/
├── challenge.json
└── files/
    └── evidence.bin
```

Minimal descriptor:

```json
{
  "identity": "my-challenge",
  "description": "Inspect the evidence and submit the flag.",
  "expected_flag": "Flag{example}",
  "network_mode": "none"
}
```

Optional `target_context` is prompt context for the audited `local` target mode. Challenge files must be regular files; symlinks, special files, and arbitrary provisioning files are rejected. `expected_flag` remains control-side and is not copied into the Agent workspace.

## Run artifacts

Each Run is one attempt under `runs/<run-id>/`:

```text
run.json       immutable configuration and provenance
events.jsonl   normalized model/tool/verifier trajectory
result.json    authoritative terminal result
writeup.md     deterministic human-readable summary
workspace/     Run-local writable challenge workspace
```

`run.json`, `events.jsonl`, and `result.json` are authoritative. `writeup.md` is derived from them and does not make another model request.

Terminal statuses are:

- `solved`: the authoritative verifier accepted a candidate.
- `unsolved`: the harness ended normally without a verified flag.
- `error`: the harness or infrastructure failed, such as a provider, sandbox, verifier, or serialization error.

A missing or invalid `result.json` means that no committed terminal result exists; it is not automatically an `error` or `unsolved` Run.

## Security and execution limits

- Model commands execute inside one Run-scoped Docker Agent container.
- Each `shell` call starts a fresh non-interactive process in that container.
- Workspace filesystem state persists; shell-local state does not.
- The Agent is non-root, has no Docker socket, uses no host networking by default, and receives no provider/verifier secrets.
- CPU, memory, PID, command-time, wall-time, and model-visible output limits are explicit.
- Unknown tools never execute.
- Non-zero command exits and incorrect flag submissions are normal evidence.
- Challenge Dockerfiles, Compose files, Makefiles, and provisioning scripts are not automatically executed.

## Known limitations

v0.1.0 intentionally does not provide:

- PTY or persistent interactive sessions
- provider routing, fallback, or automatic retries beyond the selected SDK behavior
- automatic retry/best-of-N solving
- multi-agent or planner/executor workflows
- resume/checkpoint support
- external Internet/VPN/CTFd integration
- automatic tool installation or a full Kali/CTF image
- streaming UI, TUI, database storage, or benchmark infrastructure
- advanced exploit write-ups

Only the concrete provider/model combinations tested for the release should be advertised as supported.

## Development checks

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

Docker-backed tests require Docker Engine. Paid provider calls are release evidence and are not part of the ordinary deterministic test suite.

## License

FlagAgent is released under the MIT License. See [LICENSE](LICENSE).
