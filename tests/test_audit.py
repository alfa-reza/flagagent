import json
from datetime import UTC, datetime

import pytest

from flagagent.artifacts import read_events
from flagagent.loop import AgentLoop, ChallengeInput, Limits
from flagagent.model import ModelResponse, ScriptedModel, ToolCall
from flagagent.tools import ExactStringVerifier, FakeExecutor, ShellResult

NOW = datetime(2026, 8, 14, tzinfo=UTC)


def build_loop(
    tmp_path,
    responses,
    executor=None,
    verifier=None,
    run_id="FA-20260814T000000Z-a13f4c2d",
):
    return AgentLoop(
        model=ScriptedModel(responses),
        executor=executor or FakeExecutor([]),
        verifier=verifier or ExactStringVerifier("Flag{ok}"),
        challenge=ChallengeInput("fixture", "audit test"),
        limits=Limits(
            max_model_turns=5,
            wall_timeout_seconds=100,
            command_timeout_seconds=10,
            max_model_tool_output_bytes=16,
            max_logged_tool_output_bytes=32,
        ),
        runs_root=tmp_path,
        monotonic=lambda: 0,
        utc_now=lambda: NOW,
        run_id=run_id,
    )


def test_run_metadata_is_not_rewritten_as_trajectory_state(tmp_path):
    loop = build_loop(
        tmp_path,
        [
            ModelResponse(tool_calls=(ToolCall("s", "shell", {"command": "x"}),)),
            ModelResponse(content="done"),
        ],
        executor=FakeExecutor([ShellResult("out", "", 0, False)]),
    )
    loop.run()
    stored = json.loads(loop.artifacts.run_path.read_text())

    assert stored["schema_version"] == 1
    assert stored["flagagent_version"] == "0.1.0"
    assert stored["concept_version"] == "0.1.0"
    assert stored["run_id"] == loop.artifacts.run_id
    assert stored["limits"]["max_model_turns"] == 5
    assert read_events(loop.artifacts.events_path)


def test_exact_model_visible_shell_result_is_persisted(tmp_path):

    loop = build_loop(
        tmp_path,
        [
            ModelResponse(tool_calls=(ToolCall("s", "shell", {"command": "large"}),)),
            ModelResponse(content="done"),
        ],
        executor=FakeExecutor([ShellResult("A" * 100, "B" * 100, 0, False)]),
    )

    loop.run()
    events = read_events(loop.artifacts.events_path)
    persisted = next(
        event["payload"] for event in events if event["type"] == "tool_result"
    )
    model_seen = loop.model.calls[1][0][-1]["result"]

    assert persisted["result"] == model_seen
    assert len(persisted["logged_result"]["stdout"]) > len(
        persisted["result"]["stdout"]
    )


def test_trajectory_reconstructs_calls_results_submission_and_stop(tmp_path):
    loop = build_loop(
        tmp_path,
        [
            ModelResponse(
                content="trying",
                tool_calls=(
                    ToolCall("s", "shell", {"command": "inspect"}),
                    ToolCall("f", "submit_flag", {"candidate": "Flag{ok}"}),
                    ToolCall("skip", "shell", {"command": "never"}),
                ),
            )
        ],
        executor=FakeExecutor([ShellResult("evidence", "", 0, False)]),
    )

    result = loop.run()
    events = read_events(loop.artifacts.events_path)
    by_type = {
        event_type: [event for event in events if event["type"] == event_type]
        for event_type in {event["type"] for event in events}
    }

    assert by_type["model_response"][0]["payload"]["content"] == "trying"
    assert [event["payload"]["call_id"] for event in by_type["tool_call"]] == ["s", "f"]
    assert by_type["tool_result"][0]["payload"]["result"]["stdout"] == "evidence"
    assert by_type["flag_submission"][0]["payload"]["candidate"] == "Flag{ok}"
    assert by_type["verifier_result"][0]["payload"]["outcome"] == "correct"
    assert by_type["terminal_decision"][0]["payload"] == {
        "status": "solved",
        "reason": "verified_flag",
        "committed": False,
        "unprocessed_call_ids": ["skip"],
    }
    assert (
        json.loads(loop.artifacts.result_path.read_text())["status:reason"]
        == "solved:verified_flag"
    )
    assert result["tool_calls"] == 2


def test_event_failure_commits_serialization_error_without_later_events(
    tmp_path, monkeypatch
):
    loop = build_loop(tmp_path, [ModelResponse(content="stop")])
    original_append = loop.__class__.__module__
    del original_append
    original = __import__(
        "flagagent.artifacts", fromlist=["RunArtifacts"]
    ).RunArtifacts.append_event

    def fail_model_event(self, event_type, payload):
        if event_type == "model_response":
            self._poisoned = True
            raise OSError("event failed")
        return original(self, event_type, payload)

    monkeypatch.setattr(
        "flagagent.artifacts.RunArtifacts.append_event", fail_model_event
    )

    result = loop.run()

    assert result["status:reason"] == "error:serialization_error"
    assert loop.artifacts.events_path.read_text() == ""


def test_transient_result_commit_failure_is_never_retried(tmp_path, monkeypatch):
    loop = build_loop(tmp_path, [ModelResponse(content="stop")])
    original_replace = __import__("os").replace
    attempts = []

    def flaky_replace(source, destination):
        if str(destination).endswith("result.json"):
            attempts.append(destination)
            if len(attempts) == 1:
                raise OSError("transient replace")
        return original_replace(source, destination)

    monkeypatch.setattr("flagagent.artifacts.os.replace", flaky_replace)

    with pytest.raises(OSError, match="transient replace"):
        loop.run()

    assert len(attempts) == 1
    assert not loop.artifacts.result_path.exists()


def test_solved_unsolved_error_and_no_committed_result_remain_distinct(
    tmp_path, monkeypatch
):
    solved = build_loop(
        tmp_path / "solved",
        [
            ModelResponse(
                tool_calls=(ToolCall("f", "submit_flag", {"candidate": "Flag{ok}"}),)
            )
        ],
        run_id="FA-20260814T000000Z-00000001",
    )
    unsolved = build_loop(
        tmp_path / "unsolved",
        [ModelResponse(content="stop")],
        run_id="FA-20260814T000000Z-00000002",
    )
    error = build_loop(
        tmp_path / "error",
        [RuntimeError("provider")],
        run_id="FA-20260814T000000Z-00000003",
    )

    assert solved.run()["status"] == "solved"
    assert unsolved.run()["status"] == "unsolved"
    assert error.run()["status"] == "error"

    no_result = build_loop(
        tmp_path / "no-result",
        [ModelResponse(content="stop")],
        run_id="FA-20260814T000000Z-00000004",
    )

    def fail_replace(source, destination):
        if str(destination).endswith("result.json"):
            raise OSError("commit failed")
        return original_replace(source, destination)

    original_replace = __import__("os").replace
    monkeypatch.setattr("flagagent.artifacts.os.replace", fail_replace)
    with pytest.raises(OSError, match="commit failed"):
        no_result.run()
    assert not no_result.artifacts.result_path.exists()
    assert (
        read_events(no_result.artifacts.events_path)[-1]["type"] == "terminal_decision"
    )
