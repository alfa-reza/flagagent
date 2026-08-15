import json
import os
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Self


class EventStreamPoisoned(RuntimeError):
    pass


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _serialize(value: Any) -> str:
    try:
        return json.dumps(
            value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        )
    except (TypeError, ValueError) as error:
        raise TypeError("value must be strict JSON") from error


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    encoded = _serialize(value)
    temporary: Path | None = None
    try:
        with NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(encoded)
            handle.flush()
        os.replace(temporary, path)
    except BaseException:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise


def read_events(path: Path) -> list[dict[str, Any]]:
    content = path.read_text(encoding="utf-8")
    lines = content.splitlines()
    events: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as error:
            if index == len(lines) - 1 and not content.endswith("\n"):
                break
            raise ValueError("events contain interior corruption") from error
        if not isinstance(event, dict):
            raise TypeError("event must be a JSON object")
        events.append(event)
    return events


@dataclass
class RunArtifacts:
    run_id: str
    directory: Path
    now: Callable[[], datetime]
    _events: Any = field(repr=False)
    _seq: int = 0
    _poisoned: bool = False

    @property
    def run_path(self) -> Path:
        return self.directory / "run.json"

    @property
    def events_path(self) -> Path:
        return self.directory / "events.jsonl"

    @property
    def result_path(self) -> Path:
        return self.directory / "result.json"

    @property
    def workspace(self) -> Path:
        return self.directory / "workspace"

    @staticmethod
    def generate_run_id(
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        token_hex: Callable[[int], str] = secrets.token_hex,
    ) -> str:
        timestamp = now().astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
        return f"FA-{timestamp}-{token_hex(4)}"

    @classmethod
    def create(
        cls,
        root: Path,
        metadata: Mapping[str, Any],
        *,
        run_id: str | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> "RunArtifacts":
        selected_id = run_id or cls.generate_run_id(now=now)
        metadata_run_id = metadata.get("run_id")
        if metadata_run_id != selected_id:
            raise ValueError("metadata run_id must match the selected run id")
        directory = Path(root) / selected_id
        directory.mkdir(parents=True, exist_ok=False)
        artifacts: RunArtifacts | None = None
        try:
            (directory / "workspace").mkdir()
            _atomic_json(directory / "run.json", metadata)
            events = (directory / "events.jsonl").open("a", encoding="utf-8")
            artifacts = cls(selected_id, directory, now, events)
            return artifacts
        except BaseException:
            if artifacts is not None:
                artifacts.close()
            raise

    def append_event(
        self, event_type: str, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        if self._poisoned:
            raise EventStreamPoisoned("event stream is poisoned")
        event = {
            "schema_version": 1,
            "seq": self._seq + 1,
            "timestamp": _timestamp(self.now()),
            "type": event_type,
            "payload": dict(payload),
        }
        try:
            encoded = _serialize(event)
            self._events.write(encoded + "\n")
            self._events.flush()
        except BaseException:
            self._poisoned = True
            raise
        self._seq += 1
        return event

    def commit_result(self, result: Mapping[str, Any]) -> None:
        if self.result_path.exists():
            raise FileExistsError(self.result_path)
        _atomic_json(self.result_path, result)

    def close(self) -> None:
        if not self._events.closed:
            self._events.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
