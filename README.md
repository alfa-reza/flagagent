<div align="center">

# 🚩 FlagAgent

**A small, inspectable LLM agent harness for CTFs, security labs, and reproducible security experiments.**

[![Python](https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-AGPL_v3-green.svg)](LICENSE)

</div>

FlagAgent gives a language model a small interface for solving security challenges: run commands in a contained environment, inspect the results, and submit a candidate flag.

A challenge only counts as solved when the trusted verifier accepts a submitted flag. Each run is recorded so the model's actions and the final result can be inspected afterwards.

> [!IMPORTANT]
> FlagAgent is intended for CTFs, security labs, benchmarks, and systems you are explicitly authorized to test. Do not use it against systems without permission.

## How it works

```text
Challenge
   │
   ▼
Agent Loop ────── Model API
   │
   ├── shell ─── Docker Sandbox
   │
   └── submit_flag ─── Verifier
   │
   ▼
Run Artifacts
```

The agent has two tools:

- `shell` runs a command inside the Docker sandbox and returns its result.
- `submit_flag` sends a candidate to the trusted verifier.

Only a verifier-accepted submission marks the run as solved.

## Quick start

### Requirements

- Linux
- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- Docker Engine and the Docker CLI

Clone the repository and install the project:

```bash
git clone https://github.com/alfa-reza/flagagent.git
cd flagagent
uv sync
```

Build the agent sandbox:

```bash
docker build -t flagagent-sandbox:dev images/sandbox
```

Set an API key. For example:

```bash
export OPENAI_API_KEY="your-key"
```

Run the included file challenge:

```bash
uv run flagagent run \
  --challenge challenges/layered-file \
  --protocol openai-chat \
  --model your-model
```

FlagAgent prints the run directory and terminal result when the attempt finishes. The full run is stored under `runs/`.

## Model APIs

FlagAgent currently exposes three protocol paths:

| CLI value | Protocol |
| --- | --- |
| `openai-chat` | OpenAI-compatible Chat Completions |
| `openai-responses` | OpenAI Responses |
| `anthropic` | Anthropic Messages |

`--api-base` can point an adapter at a compatible endpoint, while `--api-key-env` selects the environment variable containing its API key.

<details>
<summary>OpenRouter example</summary>

```bash
export OPENROUTER_API_KEY="your-key"

uv run flagagent run \
  --challenge challenges/layered-file \
  --protocol openai-chat \
  --model provider/model \
  --api-base https://openrouter.ai/api/v1 \
  --api-key-env OPENROUTER_API_KEY
```

</details>

Provider credentials stay on the control side and are not passed into the agent or target containers.

## Challenges

A challenge is a directory containing a `challenge.json` descriptor and, optionally, files for the agent to inspect:

```text
my-challenge/
├── challenge.json
└── files/
    └── evidence.bin
```

A minimal descriptor looks like this:

```json
{
  "identity": "my-challenge",
  "description": "Inspect the evidence and submit the flag.",
  "expected_flag": "Flag{example}",
  "network_mode": "none"
}
```

Then run it like any other challenge:

```bash
uv run flagagent run \
  --challenge path/to/my-challenge \
  --protocol openai-chat \
  --model your-model
```

The expected flag stays on the trusted control side and is not copied into the agent workspace.

Two network modes are currently supported:

- `none` gives the agent no challenge network.
- `local` connects the agent to a run-scoped internal Docker network with the project-owned target fixture.

The repository includes two small smoke challenges:

- `challenges/layered-file`
- `challenges/local-marker`

They exercise the harness itself; they are not a benchmark of general CTF capability.

`challenge.json` is bounded to 64 KiB of UTF-8 bytes and is validated before model execution or sandbox preparation; an oversized descriptor is rejected as invalid challenge input.

Challenge source ingestion is bounded before sandbox preparation:

| Limit | Default |
| --- | --- |
| Maximum individual source file size | 10 MiB |
| Maximum aggregate source content | 50 MiB |
| Maximum regular source files | 1024 |
| Maximum source entries | 2048 |
| Maximum directory depth | 16 |

A challenge source exceeding these limits is rejected as `error:invalid_challenge_source` before sandbox preparation or model execution.

<details>
<summary>Run the local target challenge</summary>

Build the project-owned target image:

```bash
docker build -t flagagent-target:dev images/target
```

Then run:

```bash
uv run flagagent run \
  --challenge challenges/local-marker \
  --protocol openai-chat \
  --model your-model
```

The target is reachable from the agent at `target:9999` over a run-scoped internal Docker network.

</details>

## Run artifacts

Every attempt gets its own directory:

```text
runs/<run-id>/
├── run.json
├── events.jsonl
├── result.json
├── writeup.md
└── workspace/
```

| Path | Purpose |
| --- | --- |
| `run.json` | Run configuration and provenance |
| `events.jsonl` | Model, tool, verifier, and lifecycle events |
| `result.json` | Authoritative terminal result |
| `writeup.md` | Human-readable summary derived from the run |
| `workspace/` | Writable workspace used by the agent |

`result.json` distinguishes `solved`, `unsolved`, and `error`, so a solver failure is separate from a harness or infrastructure failure.

## Security model

FlagAgent treats model-generated commands as untrusted.

Commands run inside a run-scoped Docker container with a non-root user, dropped Linux capabilities, `no-new-privileges`, explicit resource limits, and no host networking by default. The host Docker socket and provider credentials are not exposed to the agent.

For `network_mode: local`, FlagAgent creates a run-scoped internal Docker network for the agent and the project-owned target container. No target port is published to the host.

> [!WARNING]
> Docker is a containment boundary, not a hardened virtual machine. Containers share the host kernel. Use FlagAgent only with workloads and systems you are authorized to run or test.

## Base images

The project-owned Dockerfiles pin their `FROM` base to an immutable digest:

- `images/sandbox/Dockerfile`: `ubuntu:24.04@sha256:33ceb71981b602c1a7443a53469e4dba065f7503eab3078a2d7a57a2ab987517`
- `images/target/Dockerfile`: `python:3.12-slim@sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a`

Digests are multi-architecture image-index digests verified against `docker.io/library/...` via `skopeo inspect` / `docker buildx imagetools inspect`, so the pinned identity preserves the original platform semantics instead of locking to a single architecture. Updating a base image requires changing the digest in version control, rebuilding (`docker build -t flagagent-sandbox:dev images/sandbox` / `docker build -t flagagent-target:dev images/target`), and re-running release tests before committing. Pinning the `FROM` digest removes base-tag drift but does not by itself make the complete final images byte-for-byte reproducible because other build inputs (for example `apt-get update` package repositories) may still be mutable.

## Development

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

Docker-backed tests require Docker Engine.

## License

FlagAgent is released under the [GNU Affero General Public License v3.0](LICENSE).
