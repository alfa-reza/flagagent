import contextlib
import hashlib
import math
import multiprocessing
import multiprocessing.connection
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


class InvalidChallengeSourceError(ValueError):
    pass


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
    max_source_file_bytes: int = 10 * 1024 * 1024
    max_source_total_bytes: int = 50 * 1024 * 1024
    max_source_files: int = 1024
    max_source_entries: int = 2048
    max_source_depth: int = 16

    def __post_init__(self) -> None:
        integer_values = (
            self.max_model_turns,
            self.max_model_tool_output_bytes,
            self.max_logged_tool_output_bytes,
            self.max_source_file_bytes,
            self.max_source_total_bytes,
            self.max_source_files,
            self.max_source_entries,
            self.max_source_depth,
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
            "max_source_file_bytes": self.max_source_file_bytes,
            "max_source_total_bytes": self.max_source_total_bytes,
            "max_source_files": self.max_source_files,
            "max_source_entries": self.max_source_entries,
            "max_source_depth": self.max_source_depth,
        }


def _utc_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _snapshot_source_files(
    source_dir: Path | None,
    limits: Limits | None = None,
) -> tuple[list[tuple[Path, Path]], str | None, Any | None]:
    if source_dir is None:
        return [], None, None
    if limits is None:
        limits = Limits()
    temporary = tempfile.TemporaryDirectory(prefix="flagagent-source-")
    files: list[tuple[Path, Path]] = []
    try:
        root_fd = os.open(
            source_dir,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
    except OSError as error:
        temporary.cleanup()
        raise InvalidChallengeSourceError(
            "challenge source_dir must be a directory"
        ) from error

    total_bytes: int = 0
    file_count: int = 0
    total_entries: int = 0

    def visit(directory_fd: int, relative: Path, depth: int) -> None:
        nonlocal total_bytes, file_count, total_entries
        if depth > limits.max_source_depth:
            raise InvalidChallengeSourceError("challenge source too deep")
        names: list[str] = []
        try:
            with os.scandir(directory_fd) as it:
                for entry in it:
                    total_entries += 1
                    if total_entries > limits.max_source_entries:
                        raise InvalidChallengeSourceError(
                            "challenge source has too many entries"
                        )
                    names.append(entry.name)
        except OSError as error:
            raise InvalidChallengeSourceError(
                "challenge source cannot be read"
            ) from error
        for name in sorted(names):
            entry_relative = relative / name
            if entry_relative.is_absolute() or ".." in entry_relative.parts:
                raise InvalidChallengeSourceError("challenge source path is unsafe")
            try:
                entry_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except OSError as error:
                raise InvalidChallengeSourceError(
                    "challenge source cannot be inspected"
                ) from error
            mode = entry_stat.st_mode
            if stat.S_ISLNK(mode):
                raise InvalidChallengeSourceError("challenge source contains a symlink")
            if stat.S_ISDIR(mode):
                try:
                    child_fd = os.open(
                        name,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                        dir_fd=directory_fd,
                    )
                except OSError as error:
                    raise InvalidChallengeSourceError(
                        "challenge source directory changed"
                    ) from error
                try:
                    visit(child_fd, entry_relative, depth + 1)
                finally:
                    os.close(child_fd)
            elif stat.S_ISREG(mode):
                if file_count >= limits.max_source_files:
                    raise InvalidChallengeSourceError(
                        "challenge source has too many files"
                    )
                snapshot = Path(temporary.name) / entry_relative
                snapshot.parent.mkdir(parents=True, exist_ok=True)
                try:
                    source_fd = os.open(
                        name,
                        os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                        dir_fd=directory_fd,
                    )
                except OSError as error:
                    raise InvalidChallengeSourceError(
                        "challenge source file changed"
                    ) from error
                try:
                    st = os.fstat(source_fd)
                    if not stat.S_ISREG(st.st_mode):
                        raise InvalidChallengeSourceError(
                            "challenge source contains a special file"
                        )
                    exec_bits = st.st_mode & 0o111
                    logical_size = st.st_size
                    if logical_size > limits.max_source_file_bytes:
                        raise InvalidChallengeSourceError(
                            "challenge source file too large"
                        )
                    if total_bytes + logical_size > limits.max_source_total_bytes:
                        raise InvalidChallengeSourceError("challenge source too large")
                    with os.fdopen(source_fd, "rb") as source_handle:
                        source_fd = -1
                        with snapshot.open("wb") as snapshot_handle:
                            file_bytes = 0
                            while True:
                                chunk = source_handle.read(1024 * 1024)
                                if not chunk:
                                    break
                                if (
                                    file_bytes + len(chunk)
                                    > limits.max_source_file_bytes
                                ):
                                    raise InvalidChallengeSourceError(
                                        "challenge source file too large"
                                    )
                                if (
                                    total_bytes + len(chunk)
                                    > limits.max_source_total_bytes
                                ):
                                    raise InvalidChallengeSourceError(
                                        "challenge source too large"
                                    )
                                snapshot_handle.write(chunk)
                                file_bytes += len(chunk)
                                total_bytes += len(chunk)
                    if exec_bits:
                        try:
                            snapshot_mode = snapshot.stat().st_mode
                        except OSError as error:
                            raise InvalidChallengeSourceError(
                                "challenge source cannot be inspected"
                            ) from error
                        os.chmod(snapshot, snapshot_mode | exec_bits)
                finally:
                    if source_fd >= 0:
                        os.close(source_fd)
                files.append((snapshot, entry_relative))
                file_count += 1
            else:
                raise InvalidChallengeSourceError(
                    "challenge source contains a special file"
                )

    try:
        visit(root_fd, Path(), 0)
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


def _stage_source_files(
    workspace: Path,
    files: list[tuple[Path, Path]],
    expired: Callable[[], bool] | None = None,
) -> None:
    for source, relative in files:
        if expired is not None and expired():
            break
        destination = workspace / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        try:
            mode = source.stat().st_mode
        except OSError as error:
            raise InvalidChallengeSourceError(
                "challenge source cannot be inspected"
            ) from error
        exec_bits = mode & 0o111
        if exec_bits:
            try:
                dest_mode = destination.stat().st_mode
            except OSError as error:
                raise InvalidChallengeSourceError(
                    "challenge source cannot be inspected"
                ) from error
            os.chmod(destination, dest_mode | exec_bits)


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
        self._provider_process: Any | None = None

    def _uses_provider_process(self) -> bool:
        # Only real provider adapters (real SDK client) need a supervised
        # child so the wall can kill uncooperative work (DNS etc.).
        # ScriptedModel / injected test doubles (SimpleNamespace client or
        # _WithOptionsClient wrapper) run inline so tests remain fast and
        # spawn re-import is not triggered from a __main__ script.
        try:
            kind = type(self.model).__name__
            if kind not in (
                "ChatCompletionsModel",
                "ResponsesModel",
                "AnthropicMessagesModel",
            ):
                return False
            # Injected test doubles use a SimpleNamespace client (or
            # _WithOptionsClient wrapper around it) — keep them inline.
            cli = getattr(self.model, "client", None)
            if cli is not None and type(cli).__name__ in (
                "SimpleNamespace",
                "_WithOptionsClient",
            ):
                return False
        except Exception:
            return False
        # Real CLI path sets protocol explicitly; otherwise fall back to
        # adapter class name (direct provider tests with real http).
        if self.protocol in ("openai-chat", "openai-responses", "anthropic"):
            return True
        # Direct adapter construction without protocol (e.g.
        # test_issue47_transport make_adapter) still uses process for
        # wall-deadline correctness on real HTTP.
        return True

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
        source_error: InvalidChallengeSourceError | None = None
        source_serialization_error: ValueError | None = None
        try:
            self._source_files, source_sha256, self._source_temporary = (
                _snapshot_source_files(self.challenge.source_dir, self.limits)
            )
        except InvalidChallengeSourceError as error:
            self._source_files = []
            source_sha256 = None
            source_error = error
        except ValueError as error:
            self._source_files = []
            source_sha256 = None
            source_serialization_error = error
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
        # One persistent provider child per Run — start eagerly so the first
        # model turn does not pay spawn overhead against the wall budget.
        # Spawn context "spawn" requires this to run outside __main__ import;
        # pytest and normal CLI satisfy that (AgentLoop.run is not top-level).
        if self._uses_provider_process():
            with contextlib.suppress(Exception):
                self._ensure_provider_process()
        terminal_written = False
        try:
            if source_error is not None:
                result = self._result("error", "invalid_challenge_source")
                self.artifacts.commit_result(result)
                return result
            if source_serialization_error is not None:
                raise source_serialization_error
            if not self._expired():
                _stage_source_files(
                    self.artifacts.workspace, self._source_files, self._expired
                )
            if self._expired():
                active = "unsolved", "wall_limit", []
            else:
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
            self._cleanup_provider_process()
            self._cleanup_executor()
            self.artifacts.close()
            if self._source_temporary is not None:
                self._source_temporary.cleanup()
                self._source_temporary = None
        return result

    def _prepare_or_run(self) -> tuple[str, str, list[str]]:
        prepare = getattr(self.executor, "prepare", None)
        if prepare is not None:
            set_remaining = getattr(self.executor, "set_remaining", None)
            if set_remaining is not None:
                with contextlib.suppress(Exception):
                    set_remaining(self._remaining())
            try:
                prepare(self.artifacts.workspace, self.artifacts.run_id)
            except SandboxError:
                if self._expired():
                    return "unsolved", "wall_limit", []
                return self._error("sandbox_error", "sandbox")
            lifecycle = getattr(self.executor, "sandbox_lifecycle", None)
            if lifecycle is not None:
                with contextlib.suppress(Exception):
                    self.artifacts.append_event("sandbox_lifecycle", lifecycle())
            if self._expired():
                return "unsolved", "wall_limit", []
        return self._run_active()

    def _ensure_provider_process(self) -> None:
        proc = getattr(self, "_provider_process", None)
        if proc is not None and not getattr(proc, "_closed", False) and proc.is_alive():
            return
        from flagagent.provider_process import ProviderConfig, ProviderProcess

        protocol = self.protocol
        if protocol not in ("openai-chat", "openai-responses", "anthropic"):
            name = type(self.model).__name__
            protocol = {
                "ChatCompletionsModel": "openai-chat",
                "ResponsesModel": "openai-responses",
                "AnthropicMessagesModel": "anthropic",
            }.get(name, "openai-chat")
        api_key = getattr(self.model, "api_key", "")
        base_url = getattr(self.model, "base_url", None)
        model_name = getattr(self.model, "model", "")
        cfg = ProviderConfig(
            protocol=protocol, model=model_name, api_key=api_key, base_url=base_url
        )
        self._provider_process = ProviderProcess(cfg)

    def _call_provider_via_process(
        self, remaining: float
    ) -> tuple[str, dict[str, Any]] | None:
        """Send generate to persistent child, wait on deadline, or kill.

        Returns (kind, payload) for response/provider_error/worker_error, or
        None if the absolute deadline won and the child has been killed.
        First terminal condition wins: if deadline reached, deadline wins even
        if a response became readable at the same time.
        """
        self._ensure_provider_process()
        proc = self._provider_process

        # Use monotonic absolute deadline, not remaining recompute.
        conn = proc.conn
        try:
            with contextlib.suppress(Exception):
                conn.send(
                    {
                        "type": "generate",
                        "messages": list(self.messages),
                        "tools": list(TOOL_DEFINITIONS),
                        "remaining": float(remaining),
                    }
                )
        except Exception:
            # Pipe broken → child dead before deadline.
            return ("worker_error", {"error": "pipe_send"})

        deadline = self._deadline
        while True:
            now = self.monotonic()
            wait = deadline - now
            if wait <= 0:
                # Absolute deadline won.
                self._kill_provider_process(proc)
                return None
            # Prefer blocking on readiness rather than polling.
            try:
                ready = multiprocessing.connection.wait(
                    [conn, proc.proc.sentinel], timeout=wait
                )
            except Exception:
                ready = []
            # Re-check deadline after wait return — deadline always wins even
            # if a response became readable at the same time.
            if self.monotonic() >= deadline:
                self._kill_provider_process(proc)
                return None
            if not ready:
                # Timeout with wait >0 but no fd ready → loop to re-check.
                # This can happen if wait elapsed without ready.
                continue
            if proc.proc.sentinel in ready:
                # Child died without valid response before deadline.
                # Drain any pending message before classifying.
                try:
                    if conn in ready:
                        # Child may have sent error just before exit; prefer it.
                        msg = conn.recv()
                        if (
                            isinstance(msg, dict)
                            and msg.get("type") == "provider_error"
                        ):
                            return ("provider_error", msg)
                        if isinstance(msg, dict) and msg.get("type") == "worker_error":
                            return ("worker_error", msg)
                except Exception:
                    pass
                return ("worker_error", {"error": "child_exit"})

            if conn in ready:
                # Response available before deadline — accept it.
                try:
                    msg = conn.recv()
                except EOFError:
                    return ("worker_error", {"error": "eof"})
                except Exception:
                    return ("worker_error", {"error": "recv"})
                if not isinstance(msg, dict):
                    return ("worker_error", {"error": "bad_msg"})
                t = msg.get("type")
                if t == "response":
                    return ("response", msg.get("response") or {})
                if t == "provider_error":
                    return ("provider_error", msg)
                if t == "worker_error":
                    return ("worker_error", msg)
                return ("worker_error", {"error": f"unknown:{t}"})

    def _kill_provider_process(self, proc) -> None:
        # Mark result as no longer consumable by discarding pipe after kill.
        try:
            # Bounded graceful attempt, then kill.
            try:
                proc.conn.send({"type": "close"})
            except Exception:
                pass
            # Small grace for child to exit if it was mid-SDK close.
            proc.proc.join(timeout=0.4)
            if proc.proc.is_alive():
                with contextlib.suppress(Exception):
                    proc.proc.terminate()
                proc.proc.join(timeout=0.4)
            if proc.proc.is_alive():
                with contextlib.suppress(Exception):
                    proc.proc.kill()
                proc.proc.join(timeout=0.4)
            with contextlib.suppress(Exception):
                proc.conn.close()
        except Exception:
            pass
        finally:
            # Do not reuse IPC after kill.
            try:
                if getattr(self, "_provider_process", None) is proc:
                    self._provider_process = None
            except Exception:
                pass
            # Ensure closed flag so next turn creates new child (but Run is
            # terminal after wall_limit, so this is just for hygiene).
            with contextlib.suppress(Exception):
                setattr(proc, "_closed", True)

    def _cleanup_provider_process(self) -> None:
        proc = getattr(self, "_provider_process", None)
        if proc is None:
            return
        try:
            with contextlib.suppress(Exception):
                proc.conn.send({"type": "close"})
            proc.close()
        except Exception:
            pass
        finally:
            self._provider_process = None

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
            remaining = self._remaining()
            if remaining <= 0:
                return "unsolved", "wall_limit", []
            # Keep provider SDK timeout based on remaining budget (defense in depth).
            try:
                setter = getattr(self.model, "set_remaining", None)
                if callable(setter):
                    setter(float(remaining))
            except Exception:
                pass
            if self._uses_provider_process():
                outcome = self._call_provider_via_process(remaining)
                # None means deadline won; status already wall_limit path.
                if outcome is None:
                    return "unsolved", "wall_limit", []
                kind, payload = outcome
                if kind == "provider_error":
                    if self._expired():
                        return "unsolved", "wall_limit", []
                    return self._error("provider_error", "model")
                if kind == "worker_error":
                    if self._expired():
                        return "unsolved", "wall_limit", []
                    return self._error("provider_error", "model")
                # kind == "response": reconstruct ModelResponse
                try:
                    # payload is ModelResponse dict
                    from flagagent.model import ToolCall as _ToolCall

                    tcs = tuple(
                        _ToolCall(
                            call_id=item["call_id"],
                            name=item["name"],
                            arguments=item["arguments"],
                        )
                        for item in (payload.get("tool_calls") or [])
                    )
                    response = ModelResponse(
                        content=payload.get("content") or "",
                        tool_calls=tcs,
                        usage=payload.get("usage"),
                        truncated=bool(payload.get("truncated")),
                    )
                except Exception:
                    if self._expired():
                        return "unsolved", "wall_limit", []
                    return self._error("provider_error", "model")
            else:
                # Non-provider path (ScriptedModel / test doubles) — keep
                # synchronous inline execution without process overhead.
                # Still bound by thread + abort for deadline tests that use
                # BlockingModel etc. Reuse previous thread supervision inline.
                import threading

                _result: dict[str, Any] = {}

                def _target() -> None:
                    try:
                        _result["response"] = self.model.generate(
                            self.messages, TOOL_DEFINITIONS
                        )
                    except BaseException as exc:  # noqa: BLE001
                        _result["exc"] = exc

                worker = threading.Thread(target=_target, daemon=False)
                worker.start()
                deadline = self._deadline
                wait = deadline - self.monotonic()
                if wait > 0:
                    worker.join(timeout=wait)
                if worker.is_alive():
                    abort = getattr(self.model, "abort_request", None)
                    if abort is None:
                        abort = getattr(self.model, "abort", None)
                    if callable(abort):
                        with contextlib.suppress(Exception):
                            abort()
                    worker.join(timeout=3.0)
                    return "unsolved", "wall_limit", []
                if "exc" in _result:
                    if self._expired():
                        return "unsolved", "wall_limit", []
                    return self._error("provider_error", "model")
                response = _result.get("response")
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
            if getattr(response, "truncated", False):
                return (
                    "unsolved",
                    "model_output_limit",
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
        set_execution_deadline = getattr(self.executor, "set_execution_deadline", None)
        if set_execution_deadline is not None:
            with contextlib.suppress(Exception):
                set_execution_deadline(self._deadline, self.monotonic)
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
