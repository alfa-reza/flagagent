# FlagAgent Repository Instructions

FlagAgent is a small, model-independent agent harness for authorized CTFs, security labs, benchmarks, and sandboxed experiments.

This file contains durable repository-level guidance for coding agents. Keep changes consistent with the current code, tests, and public documentation.

## Development Principles

Prefer simple, explicit, maintainable solutions.

- **KISS:** choose the simplest implementation that satisfies the concrete requirement.
- **YAGNI:** do not add features, abstractions, dependencies, configuration, or extensibility for hypothetical future needs.
- **DRY:** avoid meaningful duplication, but do not introduce abstraction solely to eliminate small or incidental repetition.
- Prefer deterministic verification over assumptions or LLM judgement.
- Read relevant code and tests before changing behavior.
- Avoid unrelated refactors while implementing a focused change.
- Preserve existing behavior unless the task intentionally changes it.

Abstract proven variation, not anticipated variation.

## Repository Structure

- `src/flagagent/` — FlagAgent implementation.
- `tests/` — deterministic and Docker-backed tests.
- `challenges/` — project-owned challenge fixtures.
- `images/` — project-owned Docker images.

Use `README.md` as the source for user-facing behavior, supported features, setup, security limitations, and usage.

## Development and Verification

FlagAgent requires Python 3.12 or newer and uses `uv`.

Common checks:

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

Run the smallest relevant checks first, followed by broader applicable checks.

Docker-backed tests require Docker Engine.

Never claim that a test, command, security property, or Docker behavior passed unless it was actually observed.

When behavior changes, add or update deterministic tests when that behavior can reasonably be tested.

Preserve documented runtime semantics: non-zero shell exits and incorrect flag submissions are execution evidence, not automatically harness errors.

## Dependencies

Add dependencies only for a concrete requirement.

Prefer the Python standard library or existing dependencies when they adequately solve the task. Do not add dependencies speculatively.

When changing dependencies:

1. update `pyproject.toml`;
2. update `uv.lock` using `uv`;
3. run the relevant checks.

Do not edit `uv.lock` manually.

## Security Invariants

FlagAgent is intended only for legal and authorized CTFs, security labs, benchmarks, and sandboxed experiments.

Preserve the repository's containment and trust boundaries:

- model-generated challenge commands must not intentionally execute directly on the host;
- provider and verifier secrets must remain outside Agent and Target containers;
- unknown model-requested tools must never execute;
- only the authoritative verifier may establish a solved result;
- security relaxations must be explicit rather than silent;
- do not silently broaden container networking or host access;
- do not automatically execute untrusted challenge provisioning with host privileges.

Docker is a containment baseline, not a perfect isolation boundary.

Do not weaken security controls merely to make a test or challenge work.

## Code and Documentation

Follow existing code style and local patterns before introducing new ones.

Keep implementation, tests, and public documentation consistent. Update the relevant documentation when user-facing behavior changes.

Do not introduce speculative frameworks, generic plugin systems, architectural layers, or generalized abstractions unless a concrete requirement justifies them.

## Third-Party Code

Treat external repositories as references unless source reuse is explicitly justified.

Before copying or adapting third-party source, verify its provenance, license, compatibility, and attribution requirements.

Do not copy code from sources whose licensing is incompatible with FlagAgent.

## Git Safety

Keep changes focused and reviewable.

Use Conventional Commits when creating commits.

Do not modify Git identity, remotes, repository settings, repository visibility, or branch protection unless explicitly requested.

Do not run destructive operations such as `git reset --hard`, `git clean -fd`, force-pushes, or history rewrites without explicit authorization.

## Definition of Done

A change is complete when:

- the requested behavior is implemented;
- relevant deterministic checks pass;
- security boundaries remain intact;
- dependency state is consistent;
- documentation is updated when required; and
- the final diff contains no unrelated changes.

When uncertain, prefer evidence and the smallest reversible change.
