# M2 Evidence

## Status

Implementation evidence collected on 2026-08-17 from commit `17c6a67` and its preceding checkpoints on branch `feat/m2-usefulness`.

The deterministic and Docker-backed evidence is complete. Real paid provider/model evidence is pending because `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, and `OPENROUTER_API_KEY` were unset in the build environment. No API key was inspected or recorded.

## Reference environment

- Linux host
- Docker Engine 29.7.2
- Python 3.14.4 build environment
- `uv`

## Sandbox images

- Agent image: `flagagent-sandbox:dev`
- Agent image ID: `sha256:1d0083a119edcf4a5c0e05b905e05a7cf4bc2b985f7442afe19ff3eb80104474`
- Target image: `flagagent-target:dev`
- Target image ID: `sha256:9f3a7951e1622be913f5e6c66e183ddcd0a17358f1833f71dbe1c35d2a8a871b`
- Agent user observation: UID 1000, user `agent`
- Agent tools observed: `/usr/bin/python3`, `/usr/bin/nc`

The target image and target server were not changed from the M1 audited marker fixture.

## Implemented protocol paths

- OpenAI-compatible Chat Completions
- OpenAI-compatible Responses with stateless replay and encrypted reasoning inclusion
- Anthropic-compatible Messages
- OpenRouter through OpenAI-compatible Chat Completions configuration

All three adapters have deterministic controlled-client tests. Provider SDK request failures are normalized to `error:provider_error`; SDK credentials are not written to artifacts.

## Frozen smoke set

- `challenges/layered-file`: `none` networking; staged evidence file; two base64 decoding steps.
- `challenges/local-marker`: `local` networking; audited `target:9999`; deterministic M1 marker observation.

Both scripted Docker-backed smoke tests produced verifier-backed `solved:verified_flag` results. The fixtures use project-owned content and have deterministic trusted expected flags kept outside the staged workspace.

## Verification observed

- Baseline before M2: 210 tests passed.
- Full deterministic suite after Slice 6: 318 tests passed.
- Full deterministic suite after Slice 7: 322 tests passed.
- Current focused write-up suite: 3 tests passed.
- Current focused CLI/core suite: 25 tests passed before release documentation changes.
- Docker smoke fixture tests: 2 passed.
- M1 Docker regression selection: 32 passed, 88 deselected.
- Sandbox/target/provenance/network/lifecycle Docker selection: 66 passed.
- `uv lock --check`: passed.
- `uv build`: passed.
- `uv run ruff check .`: passed.
- `git diff --check`: passed.
- `flagagent --help`: passed.
- `flagagent run --help`: passed.

## Real-model evidence

Required before the v0.1.0 human release decision:

1. Configure one supported real provider/model endpoint without committing its key.
2. Run the layered-file fixture through `flagagent run`.
3. Run the local-marker fixture through `flagagent run`.
4. Inspect each `run.json`, `events.jsonl`, `result.json`, `writeup.md`, and workspace.
5. Record at least one real verifier-backed solve; target verified solves on both fixtures.
6. Record any valid `unsolved` or `error` attempts without secrets or full secret-bearing requests.

The hard framework claim remains limited until this paid end-to-end evidence is observed.

## Known limitations

- No PTY or persistent interactive shell.
- No provider routing/fallback or product retry framework.
- No automatic best-of-N evaluation.
- Docker is a containment baseline, not perfect isolation.
- Real provider/model solve rate is not claimed by deterministic tests.
