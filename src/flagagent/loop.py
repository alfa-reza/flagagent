import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from flagagent import __version__
from flagagent.artifacts import RunArtifacts
from flagagent.model import Model, ModelResponse
from flagagent.tools import (
    LOGGED_TOOL_OUTPUT_BYTES,
    MODEL_TOOL_OUTPUT_BYTES,
    TOOL_DEFINITIONS,
    Executor,
    Verifier,
    normalize_shell_result,
    validate_tool_arguments,
)


@dataclass(frozen=True)
class ChallengeInput:
    identity: str
    description: str

    def __post_init__(self) -> None:
        if not isinstance(self.identity, str) or not self.identity:
            raise ValueError("challenge identity must be a non-empty string")
        if not isinstance(self.description, str):
            raise TypeError("challenge description must be a string")


@dataclass(frozen=True)
class Limits:
    max_model_turns: int = 100
    wall_timeout_seconds: float = 1800
    command_timeout_seconds: float = 60
    max_model_tool_output_bytes: int = MODEL_TOOL_OUTPUT_BYTES
    max_logged_tool_output_bytes: int = LOGGED_TOOL_OUTPUT_BYTES

    def __post_init__(self) -> None:
        integer_values = (
            self.max_model_turns,
            self.max_model_tool_output_bytes,
            self.max_logged_tool_output_bytes,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in integer_values
        ):
            raise ValueError("integer limits must be positive integers")
        timeouts = (self.wall_timeout_seconds, self.command_timeout_seconds)
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
            for value in timeouts
        ):
            raise ValueError("timeouts must be positive finite numbers")
        if self.max_logged_tool_output_bytes < self.max_model_tool_output_bytes:
            raise ValueError("logged output limit must be at least model output limit")

    def to_dict(self) -> dict[str, int | float]:
        return {
            "max_model_turns": self.max_model_turns,
            "wall_timeout_seconds": self.wall_timeout_seconds,
            "command_timeout_seconds": self.command_timeout_seconds,
            "max_model_tool_output_bytes": self.max_model_tool_output_bytes,
            "max_logged_tool_output_bytes": self.max_logged_tool_output_bytes,
        }


def _utc_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass
class AgentLoop:
    model: Model
    executor: Executor
    verifier: Verifier
    challenge: ChallengeInput
    limits: Limits
    runs_root: Path
    monotonic: Callable[[], float]
    utc_now: Callable[[], datetime]
    run_id: str | None = None

    def __post_init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.artifacts: RunArtifacts
        self._model_calls = 0
        self._tool_calls = 0
        self._flag_submissions = 0
        self._seen_call_ids: set[str] = set()

    def _remaining(self) -> float:
        return max(0.0, self._deadline - self.monotonic())

    def _expired(self) -> bool:
        return self.monotonic() >= self._deadline

    def _error(
        self, reason: str, operation: str, call_id: str | None = None
    ) -> tuple[str, str, list[str]]:
        payload: dict[str, Any] = {"reason": reason, "operation": operation}
        if call_id is not None:
            payload["call_id"] = call_id
        self.artifacts.append_event("error", payload)
        return "error", reason, []

    def _tool_result(
        self,
        call_id: str,
        name: str,
        result: dict[str, Any],
        executed: bool,
        logged_result: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "call_id": call_id,
            "name": name,
            "executed": executed,
            "result": result,
        }
        if logged_result is not None:
            payload["logged_result"] = logged_result
        self.artifacts.append_event("tool_result", payload)
        self.messages.append(
            {"role": "tool", "call_id": call_id, "name": name, "result": result}
        )

    def _result(self, status: str, reason: str) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "run_id": self.artifacts.run_id,
            "status": status,
            "reason": reason,
            "status:reason": f"{status}:{reason}",
            "finished_at": _utc_timestamp(self.utc_now()),
            "duration_seconds": max(0.0, self.monotonic() - self._started),
            "model_calls": self._model_calls,
            "tool_calls": self._tool_calls,
            "flag_submissions": self._flag_submissions,
        }

    def _terminal(
        self, status: str, reason: str, unprocessed: list[str]
    ) -> dict[str, Any]:
        self.artifacts.append_event(
            "terminal_decision",
            {
                "status": status,
                "reason": reason,
                "committed": False,
                "unprocessed_call_ids": unprocessed,
            },
        )
        result = self._result(status, reason)
        self.artifacts.commit_result(result)
        return result

    def run(self) -> dict[str, Any]:
        selected_id = self.run_id or RunArtifacts.generate_run_id(now=self.utc_now)
        metadata = {
            "schema_version": 1,
            "run_id": selected_id,
            "flagagent_version": __version__,
            "concept_version": "0.1.0",
            "challenge": {
                "identity": self.challenge.identity,
                "description": self.challenge.description,
            },
            "started_at": _utc_timestamp(self.utc_now()),
            "limits": self.limits.to_dict(),
        }
        self.artifacts = RunArtifacts.create(
            self.runs_root,
            metadata,
            run_id=selected_id,
            now=self.utc_now,
        )
        self.messages = [
            {
                "role": "user",
                "content": self.challenge.description,
                "challenge_identity": self.challenge.identity,
            }
        ]
        self._started = self.monotonic()
        self._deadline = self._started + self.limits.wall_timeout_seconds
        terminal_written = False
        try:
            active = self._run_active()
            active_status, active_reason, active_unprocessed = active
            try:
                result = self._terminal(
                    active_status, active_reason, active_unprocessed
                )
                terminal_written = True
            except (OSError, TypeError, ValueError):
                terminal_written = True
                raise
        except (OSError, TypeError, ValueError):
            if terminal_written:
                raise
            result = self._result("error", "serialization_error")
            self.artifacts.commit_result(result)
        finally:
            self.artifacts.close()
        return result

    def _run_active(self) -> tuple[str, str, list[str]]:
        while True:
            if self._expired():
                return "unsolved", "wall_limit", []
            if self._model_calls >= self.limits.max_model_turns:
                return "unsolved", "model_turn_limit", []
            self._model_calls += 1
            try:
                response = self.model.generate(self.messages, TOOL_DEFINITIONS)
            except Exception:
                if self._expired():
                    return "unsolved", "wall_limit", []
                return self._error("provider_error", "model")
            if self._expired():
                return "unsolved", "wall_limit", []
            if not isinstance(response, ModelResponse):
                return self._error("provider_error", "model")
            duplicate = self._duplicate_id(response)
            accepted = duplicate is None
            self.artifacts.append_event(
                "model_response",
                {
                    "model_call": self._model_calls,
                    "accepted": accepted,
                    **response.to_dict(),
                },
            )
            if duplicate is not None:
                return self._error("provider_error", "model")
            self._seen_call_ids.update(call.call_id for call in response.tool_calls)
            self.messages.append(
                {
                    "role": "assistant",
                    "content": response.content,
                    "tool_calls": [call.to_dict() for call in response.tool_calls],
                }
            )
            if not response.tool_calls:
                return "unsolved", "model_stop", []
            terminal = self._dispatch(response)
            if terminal is not None:
                return terminal
            if self._model_calls >= self.limits.max_model_turns:
                return "unsolved", "model_turn_limit", []

    def _duplicate_id(self, response: ModelResponse) -> str | None:
        current: set[str] = set()
        for call in response.tool_calls:
            if call.call_id in current or call.call_id in self._seen_call_ids:
                return call.call_id
            current.add(call.call_id)
        return None

    def _dispatch(self, response: ModelResponse) -> tuple[str, str, list[str]] | None:
        calls = response.tool_calls
        for index, call in enumerate(calls):
            if self._expired():
                return (
                    "unsolved",
                    "wall_limit",
                    [item.call_id for item in calls[index:]],
                )
            self.artifacts.append_event(
                "tool_call",
                {
                    "call_id": call.call_id,
                    "source_index": index,
                    "name": call.name,
                    "arguments": call.arguments,
                },
            )
            self._tool_calls += 1
            if call.name not in {"shell", "submit_flag"}:
                self._tool_result(
                    call.call_id,
                    call.name,
                    {"ok": False, "error": {"type": "unknown_tool"}},
                    False,
                )
                continue
            try:
                arguments = validate_tool_arguments(call.name, call.arguments)
            except (TypeError, ValueError):
                self._tool_result(
                    call.call_id,
                    call.name,
                    {"ok": False, "error": {"type": "invalid_arguments"}},
                    False,
                )
                continue
            if call.name == "shell":
                terminal = self._shell(call.call_id, arguments["command"])
            else:
                terminal = self._submit(call.call_id, arguments["candidate"])
            if terminal is not None:
                if terminal[:2] == ("solved", "verified_flag"):
                    return *terminal[:2], [item.call_id for item in calls[index + 1 :]]
                return terminal
        return None

    def _shell(self, call_id: str, command: str) -> tuple[str, str, list[str]] | None:
        timeout = min(self.limits.command_timeout_seconds, self._remaining())
        try:
            raw_result = self.executor.execute(command, timeout)
            model_result, logged_result = normalize_shell_result(
                raw_result,
                self.limits.max_model_tool_output_bytes,
                self.limits.max_logged_tool_output_bytes,
            )
        except Exception:
            if self._expired():
                return "unsolved", "wall_limit", []
            return self._error("tool_error", "executor", call_id)
        self._tool_result(
            call_id,
            "shell",
            model_result.to_dict(),
            True,
            logged_result.to_dict(),
        )
        if self._expired():
            return "unsolved", "wall_limit", []
        return None

    def _submit(
        self, call_id: str, candidate: str
    ) -> tuple[str, str, list[str]] | None:
        stripped = candidate.strip()
        self.artifacts.append_event(
            "flag_submission", {"call_id": call_id, "candidate": stripped}
        )
        self._flag_submissions += 1
        try:
            outcome = self.verifier.check(stripped)
            if outcome not in {"correct", "incorrect"}:
                raise ValueError("unsupported verifier outcome")
        except Exception:
            if self._expired():
                return "unsolved", "wall_limit", []
            return self._error("verifier_error", "verifier", call_id)
        self.artifacts.append_event(
            "verifier_result", {"call_id": call_id, "outcome": outcome}
        )
        self._tool_result(call_id, "submit_flag", {"outcome": outcome}, True)
        if self._expired():
            return "unsolved", "wall_limit", []
        if outcome == "correct":
            return "solved", "verified_flag", []
        return None
