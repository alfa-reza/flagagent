import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from flagagent.artifacts import read_events


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"{path.name} must contain an object")
    return value


def _render_actions(events: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for event in events:
        event_type = event.get("type")
        payload = event.get("payload", {})
        if not isinstance(payload, dict):
            continue
        if event_type == "tool_call":
            name = payload.get("name", "unknown")
            call_id = payload.get("call_id", "")
            arguments = payload.get("arguments")
            if name == "shell" and isinstance(arguments, dict):
                command = arguments.get("command")
                if isinstance(command, str):
                    command = command.replace("`", "\\`")
                    lines.append(f"- `shell` call `{call_id}`: `{command}`")
                    continue
            lines.append(f"- `{name}` call `{call_id}`")
        elif event_type == "flag_submission":
            lines.append(f"- `submit_flag` candidate: `{payload.get('candidate', '')}`")
        elif event_type == "verifier_result":
            lines.append(f"- verifier outcome: `{payload.get('outcome', '')}`")
    return lines or ["- no tool actions recorded"]


def _render(
    run: dict[str, Any], events: list[dict[str, Any]], result: dict[str, Any]
) -> str:
    challenge = run.get("challenge", {})
    model = run.get("model", {})
    prompt = run.get("prompt", {})
    lines = [
        "# FlagAgent Run",
        "",
        f"- Run ID: `{run.get('run_id', '')}`",
        f"- Challenge: `{challenge.get('identity', '')}`",
        f"- Status: `{result.get('status', '')}`",
        f"- Reason: `{result.get('reason', '')}`",
        f"- Model: `{model.get('name', '')}`",
        f"- Protocol: `{model.get('protocol', '')}`",
        f"- Prompt version: `{prompt.get('version', '')}`",
        f"- Prompt SHA-256: `{prompt.get('sha256', '')}`",
        "",
        "## Actions",
        "",
        *_render_actions(events),
        "",
        "## Metrics",
        "",
        f"- Duration seconds: `{result.get('duration_seconds', '')}`",
        f"- Model calls: `{result.get('model_calls', '')}`",
        f"- Tool calls: `{result.get('tool_calls', '')}`",
        f"- Flag submissions: `{result.get('flag_submissions', '')}`",
    ]
    if "input_tokens" in result:
        lines.append(f"- Input tokens: `{result['input_tokens']}`")
    if "output_tokens" in result:
        lines.append(f"- Output tokens: `{result['output_tokens']}`")
    lines.extend(["", "Structured artifacts remain authoritative.", ""])
    return "\n".join(lines)


def write_writeup(run_directory: Path) -> Path:
    directory = Path(run_directory)
    run = _json(directory / "run.json")
    events = read_events(directory / "events.jsonl")
    result = _json(directory / "result.json")
    destination = directory / "writeup.md"
    temporary: Path | None = None
    try:
        with NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=directory,
            prefix=".writeup.md.",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(_render(run, events, result))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    except BaseException:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise
    return destination
