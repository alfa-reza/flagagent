import contextlib
import hashlib
import math
import os
import shutil
import stat
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from flagagent import __version__
from flagagent.artifacts import RunArtifacts
from flagagent.model import Model, ModelResponse
from flagagent.tools import (
    LOGGED_TOOL_OUTPUT_BYTES,
    MODEL_TOOL_OUTPUT_BYTES,
    TOOL_DEFINITIONS,
    Executor,
    SandboxError,
    Verifier,
    normalize_shell_result,
    validate_tool_arguments,
)


@dataclass(frozen=True)
class ChallengeInput:
    identity: str
    description: str
    source_dir: Path | None = None
    target_context: str | None = None
    network_mode: str = "none"

    def __post_init__(self) -> None:
        if not isinstance(self.identity, str) or not self.identity:
            raise ValueError("challenge identity must be a non-empty string")
        if not isinstance(self.description, str):
            raise TypeError("challenge description must be a string")
        if self.source_dir is not None and not isinstance(self.source_dir, Path):
            raise TypeError("challenge source_dir must be a Path")
        if self.target_context is not None and not isinstance(self.target_context, str):
            raise TypeError("challenge target_context must be a string")
        if self.network_mode not in {"none", "local"}:
            raise ValueError("challenge network_mode must be none or local")


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


def _snapshot_source_files(
    source_dir: Path | None,
) -> tuple[list[tuple[Path, Path]], str | None, Any | None]:
    if source_dir is None:
        return [], None, None
    temporary = tempfile.TemporaryDirectory(prefix="flagagent-source-")
    files: list[tuple[Path, Path]] = []
    try:
        root_fd = os.open(
            source_dir,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
    except OSError as error:
        temporary.cleanup()
        raise ValueError("challenge source_dir must be a directory") from error

    def visit(directory_fd: int, relative: Path) -> None:
        try:
            entries = list(os.scandir(directory_fd))
        except OSError as error:
            raise ValueError("challenge source cannot be read") from error
        for entry in sorted(entries, key=lambda item: item.name):
            entry_relative = relative / entry.name
            if entry_relative.is_absolute() or ".." in entry_relative.parts:
                raise ValueError("challenge source path is unsafe")
            try:
                entry_stat = os.stat(
                    entry.name, dir_fd=directory_fd, follow_symlinks=False
                )
            except OSError as error:
                raise ValueError("challenge source cannot be inspected") from error
            mode = entry_stat.st_mode
            if stat.S_ISLNK(mode):
                raise ValueError("challenge source contains a symlink")
            if stat.S_ISDIR(mode):
                try:
                    child_fd = os.open(
                        entry.name,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                        dir_fd=directory_fd,
                    )
                except OSError as error:
                    raise ValueError("challenge source directory changed") from error
                try:
                    visit(child_fd, entry_relative)
                finally:
                    os.close(child_fd)
            elif stat.S_ISREG(mode):
                snapshot = Path(temporary.name) / entry_relative
                snapshot.parent.mkdir(parents=True, exist_ok=True)
                try:
                    source_fd = os.open(
                        entry.name,
                        os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                        dir_fd=directory_fd,
                    )
                except OSError as error:
                    raise ValueError("challenge source file changed") from error
                try:
                    if not stat.S_ISREG(os.fstat(source_fd).st_mode):
                        raise ValueError("challenge source contains a special file")
                    with os.fdopen(source_fd, "rb") as source_handle:
                        source_fd = -1
                        with snapshot.open("wb") as snapshot_handle:
                            shutil.copyfileobj(
                                source_handle, snapshot_handle, 1024 * 1024
                            )
                finally:
                    if source_fd >= 0:
                        os.close(source_fd)
                files.append((snapshot, entry_relative))
            else:
                raise ValueError("challenge source contains a special file")

    try:
        visit(root_fd, Path())
    except BaseException:
        os.close(root_fd)
        temporary.cleanup()
        raise
    os.close(root_fd)

    digest = hashlib.sha256(b"FLAGAGENT-SOURCE-V1")
    for snapshot, relative in sorted(files, key=lambda item: item[1].as_posix()):
        relative_bytes = relative.as_posix().encode("utf-8")
        size = snapshot.stat().st_size
        digest.update(len(relative_bytes).to_bytes(8, "big"))
        digest.update(relative_bytes)
        digest.update(size.to_bytes(8, "big"))
        with snapshot.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    return files, digest.hexdigest(), temporary


def _stage_source_files(workspace: Path, files: list[tuple[Path, Path]]) -> None:
    for source, relative in files:
        destination = workspace / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)


def _sanitize_api_base(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError("api_base must be a non-empty string")
    parsed = urlsplit(value)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("api_base must not contain credentials or query data")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


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
    system_prompt: str | None = None
    prompt_version: str | None = None
    prompt_sha256: str | None = None
    model_identity: str | None = None
    protocol: str | None = None
    api_base: str | None = None

    def __post_init__(self) -> None:
        if self.system_prompt is None:
            if self.prompt_version is not None or self.prompt_sha256 is not None:
                raise ValueError("prompt metadata requires a system prompt")
        else:
            if not isinstance(self.system_prompt, str) or not self.system_prompt:
                raise ValueError("system_prompt must be a non-empty string")
            if not isinstance(self.prompt_version, str) or not self.prompt_version:
                raise ValueError("prompt_version is required with a system prompt")
            if not isinstance(self.prompt_sha256, str) or not self.prompt_sha256:
                raise ValueError("prompt_sha256 is required with a system prompt")
            expected_sha256 = hashlib.sha256(
                self.system_prompt.encode("utf-8")
            ).hexdigest()
            if self.prompt_sha256 != expected_sha256:
                raise ValueError("prompt_sha256 does not match system_prompt")
        self.messages: list[dict[str, Any]] = []
        self.artifacts: RunArtifacts
        self._model_calls = 0
        self._tool_calls = 0
        self._flag_submissions = 0
        self._seen_call_ids: set[str] = set()
        self._input_tokens: int | None = None
        self._output_tokens: int | None = None
        self._source_files: list[tuple[Path, Path]] = []
        self._source_temporary: Any | None = None

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
        result = {
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
        if self._input_tokens is not None:
            result["input_tokens"] = self._input_tokens
        if self._output_tokens is not None:
            result["output_tokens"] = self._output_tokens
        return result

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
        source_error: ValueError | None = None
        try:
            self._source_files, source_sha256, self._source_temporary = (
                _snapshot_source_files(self.challenge.source_dir)
            )
        except ValueError as error:
            self._source_files = []
            source_sha256 = None
            source_error = error
        api_base = _sanitize_api_base(self.api_base)
        selected_id = self.run_id or RunArtifacts.generate_run_id(now=self.utc_now)
        challenge_metadata: dict[str, Any] = {
            "identity": self.challenge.identity,
            "description": self.challenge.description,
        }
        if self.challenge.target_context is not None:
            challenge_metadata["target_context"] = self.challenge.target_context
        if source_sha256 is not None:
            challenge_metadata["source_sha256"] = source_sha256
        metadata: dict[str, Any] = {
            "schema_version": 1,
            "run_id": selected_id,
            "flagagent_version": __version__,
            "concept_version": "0.1.0",
            "challenge": challenge_metadata,
            "started_at": _utc_timestamp(self.utc_now()),
            "limits": self.limits.to_dict(),
        }
        if self.system_prompt is not None:
            metadata["prompt"] = {
                "version": self.prompt_version,
                "sha256": self.prompt_sha256,
            }
        if any(
            value is not None
            for value in (self.model_identity, self.protocol, api_base)
        ):
            metadata["model"] = {
                "name": self.model_identity,
                "protocol": self.protocol,
                "base_url": api_base,
            }
        provenance = getattr(self.executor, "sandbox_provenance", None)

        if provenance is not None:
            with contextlib.suppress(Exception):
                metadata["sandbox"] = provenance()
        self.artifacts = RunArtifacts.create(
            self.runs_root,
            metadata,
            run_id=selected_id,
            now=self.utc_now,
        )
        user_content = self.challenge.description
        if self.challenge.target_context:
            user_content = (
                f"{user_content}\n\nTarget context:\n{self.challenge.target_context}"
            )
        self.messages = []
        if self.system_prompt is not None:
            self.messages.append({"role": "system", "content": self.system_prompt})
        self.messages.append(
            {
                "role": "user",
                "content": user_content,
                "challenge_identity": self.challenge.identity,
            }
        )
        self._started = self.monotonic()
        self._deadline = self._started + self.limits.wall_timeout_seconds
        terminal_written = False
        try:
            if source_error is not None:
                raise source_error
            _stage_source_files(self.artifacts.workspace, self._source_files)
            active = self._prepare_or_run()
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
            self._cleanup_executor()
            self.artifacts.close()
            if self._source_temporary is not None:
                self._source_temporary.cleanup()
                self._source_temporary = None
        return result

    def _prepare_or_run(self) -> tuple[str, str, list[str]]:
        prepare = getattr(self.executor, "prepare", None)
        if prepare is not None:
            try:
                prepare(self.artifacts.workspace, self.artifacts.run_id)
            except SandboxError:
                return self._error("sandbox_error", "sandbox")
            lifecycle = getattr(self.executor, "sandbox_lifecycle", None)
            if lifecycle is not None:
                with contextlib.suppress(Exception):
                    self.artifacts.append_event("sandbox_lifecycle", lifecycle())
        return self._run_active()

    def _cleanup_executor(self) -> None:
        cleanup = getattr(self.executor, "cleanup", None)
        if cleanup is None:
            return
        try:
            cleanup(self.artifacts.run_id)
        except Exception as error:
            with contextlib.suppress(Exception):
                self.artifacts.append_event(
                    "sandbox_cleanup_failed",
                    {"error_type": type(error).__name__},
                )

    def _run_active(self) -> tuple[str, str, list[str]]:
        while True:
            if self._expired():
                return "unsolved", "wall_limit", []
            if self._model_calls >= self.limits.max_model_turns:
                return "unsolved", "model_turn_limit", []
            self._model_calls += 1
            try:
                set_remaining = getattr(self.model, "set_remaining", None)
                if set_remaining is not None:
                    set_remaining(self._remaining())
                response = self.model.generate(self.messages, TOOL_DEFINITIONS)
            except Exception:
                if self._expired():
                    return "unsolved", "wall_limit", []
                return self._error("provider_error", "model")
            if not isinstance(response, ModelResponse):
                if self._expired():
                    return "unsolved", "wall_limit", []
                return self._error("provider_error", "model")
            self._add_usage(response.usage)
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
                if self._expired():
                    return (
                        "unsolved",
                        "wall_limit",
                        [call.call_id for call in response.tool_calls],
                    )
                return self._error("provider_error", "model")
            self._seen_call_ids.update(call.call_id for call in response.tool_calls)
            self.messages.append(
                {
                    "role": "assistant",
                    "content": response.content,
                    "tool_calls": [call.to_dict() for call in response.tool_calls],
                }
            )
            if self._expired():
                return (
                    "unsolved",
                    "wall_limit",
                    [call.call_id for call in response.tool_calls],
                )
            if not response.tool_calls:
                return "unsolved", "model_stop", []
            terminal = self._dispatch(response)
            if terminal is not None:
                return terminal
            if self._model_calls >= self.limits.max_model_turns:
                return "unsolved", "model_turn_limit", []

    def _add_usage(self, usage: Any) -> None:
        if not isinstance(usage, dict):
            return
        for name, attribute in (
            ("input_tokens", "_input_tokens"),
            ("output_tokens", "_output_tokens"),
        ):
            value = usage.get(name)
            if isinstance(value, bool) or not isinstance(value, int):
                continue
            current = getattr(self, attribute)
            setattr(self, attribute, value if current is None else current + value)

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
        except SandboxError:
            if self._expired():
                return "unsolved", "wall_limit", []
            return self._error("sandbox_error", "sandbox", call_id)
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
