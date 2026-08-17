import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any

from flagagent.anthropic_messages import AnthropicMessagesModel
from flagagent.docker_executor import DockerExecutor
from flagagent.loop import (
    AgentLoop,
    ChallengeInput,
    Limits,
    _sanitize_api_base,
)
from flagagent.model import Model
from flagagent.prompt import SOLVER_PROMPT, SOLVER_PROMPT_SHA256, SOLVER_PROMPT_VERSION
from flagagent.providers import ChatCompletionsModel
from flagagent.responses import ResponsesModel
from flagagent.tools import ExactStringVerifier

PROTOCOLS = {
    "openai-chat": (ChatCompletionsModel, "OPENAI_API_KEY"),
    "openai-responses": (ResponsesModel, "OPENAI_API_KEY"),
    "anthropic": (AnthropicMessagesModel, "ANTHROPIC_API_KEY"),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="flagagent")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run")
    run.add_argument("--challenge", type=Path, required=True)
    run.add_argument("--protocol", choices=tuple(PROTOCOLS), required=True)
    run.add_argument("--model", required=True)
    run.add_argument("--api-base")
    run.add_argument("--api-key-env")
    run.add_argument("--runs-root", type=Path, default=Path("runs"))
    run.add_argument("--max-model-turns", type=int, default=100)
    run.add_argument("--wall-timeout-seconds", type=float, default=1800)
    run.add_argument("--command-timeout-seconds", type=float, default=60)
    return parser


def _require_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"challenge descriptor requires non-empty {key}")
    return value


def load_challenge(root: Path) -> tuple[ChallengeInput, str]:
    if not root.is_dir() or root.is_symlink():
        raise ValueError("challenge must be a directory")
    descriptor = root / "challenge.json"
    if descriptor.is_symlink() or not descriptor.is_file():
        raise ValueError("challenge.json must be a regular file")
    try:
        payload = json.loads(descriptor.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("challenge.json must be valid JSON") from error
    if not isinstance(payload, dict):
        raise TypeError("challenge.json must contain an object")
    allowed = {"identity", "description", "expected_flag", "network_mode", "target_context"}
    if set(payload) - allowed:
        raise ValueError("challenge.json contains unsupported fields")
    identity = _require_string(payload, "identity")
    description = payload.get("description")
    if not isinstance(description, str):
        raise TypeError("challenge descriptor requires string description")
    expected = _require_string(payload, "expected_flag")
    network_mode = payload.get("network_mode")
    if network_mode not in {"none", "local"}:
        raise ValueError("challenge network_mode must be none or local")
    target_context = payload.get("target_context")
    if target_context is not None and not isinstance(target_context, str):
        raise TypeError("challenge target_context must be a string")
    files = root / "files"
    if files.exists() and (files.is_symlink() or not files.is_dir()):
        raise ValueError("challenge files must be a directory")
    source_dir = files if files.exists() else None
    return (
        ChallengeInput(
            identity,
            description,
            source_dir=source_dir,
            target_context=target_context,
            network_mode=network_mode,
        ),
        expected,
    )


def _run(args: argparse.Namespace) -> int:
    try:
        challenge, expected_flag = load_challenge(args.challenge)
        if args.protocol == "openai-chat":
            model_class, default_key_env = ChatCompletionsModel, "OPENAI_API_KEY"
        elif args.protocol == "openai-responses":
            model_class, default_key_env = ResponsesModel, "OPENAI_API_KEY"
        else:
            model_class, default_key_env = AnthropicMessagesModel, "ANTHROPIC_API_KEY"
        key_env = args.api_key_env or default_key_env
        if not key_env or not key_env.isidentifier():
            raise ValueError("api-key-env must be an environment variable name")
        api_key = os.environ.get(key_env)
        if not api_key:
            raise ValueError(f"missing API key environment variable: {key_env}")
        api_base = _sanitize_api_base(args.api_base)
        model: Model = model_class(
            model=args.model,
            api_key=api_key,
            base_url=api_base,
        )
        executor = DockerExecutor(network_mode=challenge.network_mode)
        loop = AgentLoop(
            model=model,
            executor=executor,
            verifier=ExactStringVerifier(expected_flag),
            challenge=challenge,
            limits=Limits(
                max_model_turns=args.max_model_turns,
                wall_timeout_seconds=args.wall_timeout_seconds,
                command_timeout_seconds=args.command_timeout_seconds,
            ),
            runs_root=args.runs_root,
            monotonic=monotonic,
            utc_now=lambda: datetime.now(UTC),
            system_prompt=SOLVER_PROMPT,
            prompt_version=SOLVER_PROMPT_VERSION,
            prompt_sha256=SOLVER_PROMPT_SHA256,
            model_identity=args.model,
            protocol=args.protocol,
            api_base=api_base,
        )
        result = loop.run()
        print(f"run={loop.artifacts.directory}")
        print(result["status:reason"])
        return 0 if result["status"] != "error" else 1
    except (OSError, TypeError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "run":
        return _run(args)
    return 2
