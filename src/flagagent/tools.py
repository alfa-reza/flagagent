from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

MODEL_TOOL_OUTPUT_BYTES = 16 * 1024
LOGGED_TOOL_OUTPUT_BYTES = 64 * 1024
TRUNCATION_MARKER = "[truncated]"

TOOL_DEFINITIONS = [
    {
        "name": "shell",
        "description": "Run one non-interactive shell command.",
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
            "additionalProperties": False,
        },
    },
    {
        "name": "submit_flag",
        "description": "Submit one candidate flag for authoritative verification.",
        "parameters": {
            "type": "object",
            "properties": {"candidate": {"type": "string"}},
            "required": ["candidate"],
            "additionalProperties": False,
        },
    },
]


def validate_tool_arguments(name: str, arguments: Any) -> dict[str, str]:
    if not isinstance(arguments, Mapping):
        raise TypeError("arguments must be an object")
    required = {"shell": "command", "submit_flag": "candidate"}.get(name)
    if required is None:
        raise KeyError(name)
    if set(arguments) != {required}:
        raise ValueError("arguments must contain exactly the required field")
    value = arguments[required]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{required} must be a non-empty string")
    return {required: value}


@dataclass(frozen=True)
class ShellResult:
    stdout: str
    stderr: str
    exit_code: int | None
    timed_out: bool
    truncated: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.stdout, str) or not isinstance(self.stderr, str):
            raise TypeError("stdout and stderr must be strings")
        if not isinstance(self.timed_out, bool) or not isinstance(self.truncated, bool):
            raise TypeError("timed_out and truncated must be booleans")
        if self.timed_out:
            if self.exit_code is not None:
                raise ValueError("timed out results require a null exit code")
        elif isinstance(self.exit_code, bool) or not isinstance(self.exit_code, int):
            raise TypeError("completed results require an integer exit code")

    def to_dict(self) -> dict[str, Any]:
        return {
            "stdout": self.stdout,
            "stderr": self.stderr,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "truncated": self.truncated,
        }


def _utf8_prefix(data: bytes, limit: int) -> str:
    return data[:limit].decode("utf-8", errors="ignore")


def _utf8_suffix(data: bytes, limit: int) -> str:
    return data[-limit:].decode("utf-8", errors="ignore")


def truncate_utf8(value: str, limit: int) -> tuple[str, bool]:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError("output limit must be a positive integer")
    data = value.encode("utf-8")
    if len(data) <= limit:
        return value, False
    marker = TRUNCATION_MARKER.encode()
    if limit <= len(marker):
        return _utf8_prefix(marker, limit), True
    available = limit - len(marker)
    head_bytes = (available + 1) // 2
    tail_bytes = available // 2
    rendered = (
        _utf8_prefix(data, head_bytes)
        + TRUNCATION_MARKER
        + _utf8_suffix(data, tail_bytes)
    )
    while len(rendered.encode()) > limit and head_bytes:
        head_bytes -= 1
        rendered = (
            _utf8_prefix(data, head_bytes)
            + TRUNCATION_MARKER
            + _utf8_suffix(data, tail_bytes)
        )
    return rendered, True


def _normalize_view(result: ShellResult, limit: int) -> ShellResult:
    stdout, stdout_truncated = truncate_utf8(result.stdout, limit)
    stderr, stderr_truncated = truncate_utf8(result.stderr, limit)
    return ShellResult(
        stdout=stdout,
        stderr=stderr,
        exit_code=result.exit_code,
        timed_out=result.timed_out,
        truncated=result.truncated or stdout_truncated or stderr_truncated,
    )


def normalize_shell_result(
    result: ShellResult,
    model_limit: int = MODEL_TOOL_OUTPUT_BYTES,
    logged_limit: int = LOGGED_TOOL_OUTPUT_BYTES,
) -> tuple[ShellResult, ShellResult]:
    if logged_limit < model_limit:
        raise ValueError("logged output limit must be at least the model output limit")
    return _normalize_view(result, model_limit), _normalize_view(result, logged_limit)


class Executor(Protocol):
    def execute(self, command: str, timeout_seconds: float) -> ShellResult: ...


@dataclass
class FakeExecutor:
    script: Sequence[ShellResult | Exception]
    calls: list[tuple[str, float]] = field(default_factory=list, init=False)
    _index: int = field(default=0, init=False)

    def execute(self, command: str, timeout_seconds: float) -> ShellResult:
        self.calls.append((command, timeout_seconds))
        if self._index >= len(self.script):
            raise RuntimeError("fake executor exhausted")
        item = self.script[self._index]
        self._index += 1
        if isinstance(item, Exception):
            raise item
        if not isinstance(item, ShellResult):
            raise TypeError("executor returned an invalid result")
        return item


VerifierOutcome = Literal["correct", "incorrect"]


class Verifier(Protocol):
    def check(self, candidate: str) -> VerifierOutcome: ...


@dataclass(frozen=True)
class ExactStringVerifier:
    _expected: str

    def check(self, candidate: str) -> VerifierOutcome:
        return "correct" if candidate.strip() == self._expected else "incorrect"
