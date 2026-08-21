from datetime import UTC, datetime

import pytest

from flagagent.artifacts import read_events
from flagagent.loop import AgentLoop, ChallengeInput, Limits
from flagagent.model import ModelResponse, ScriptedModel, ToolCall
from flagagent.tools import ExactStringVerifier, FakeExecutor

NOW = datetime(2026, 8, 14, tzinfo=UTC)


class Clock:
    def __init__(self, value=0.0):
        self.value = value

    def __call__(self):
        return self.value


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_model_turns": 0},
        {"max_model_turns": True},
        {"wall_timeout_seconds": float("nan")},
        {"wall_timeout_seconds": float("inf")},
        {"command_timeout_seconds": False},
        {"max_model_tool_output_bytes": 20, "max_logged_tool_output_bytes": 10},
    ],
)
def test_limits_reject_nonpositive_nonfinite_and_boolean_values(kwargs):
    with pytest.raises(ValueError):
        Limits(**kwargs)


def test_wall_exhaustion_before_model_starts_no_operation(tmp_path):
    clock = Clock(0)
    model = ScriptedModel([ModelResponse(content="never")])
    loop = AgentLoop(
        model=model,
        executor=FakeExecutor([]),
        verifier=ExactStringVerifier("Flag{x}"),
        challenge=ChallengeInput("fixture", "test"),
        limits=Limits(
            max_model_turns=2, wall_timeout_seconds=1, command_timeout_seconds=1
        ),
        runs_root=tmp_path,
        monotonic=clock,
        utc_now=lambda: NOW,
        run_id="FA-20260814T000000Z-a13f4c2d",
    )
    original_run_active = loop._run_active

    def expired_run_active():
        clock.value = 1
        return original_run_active()

    loop._run_active = expired_run_active
    result = loop.run()

    assert result["status:reason"] == "unsolved:wall_limit"
    assert result["model_calls"] == 0
    assert model.calls == []


def test_model_exception_after_deadline_is_wall_limit(tmp_path):
    clock = Clock()

    class LateModel:
        def __init__(self):
            self.calls = []

        def generate(self, messages, tools):
            self.calls.append(1)
            clock.value = 1
            raise RuntimeError("late provider failure")

    loop = AgentLoop(
        model=LateModel(),
        executor=FakeExecutor([]),
        verifier=ExactStringVerifier("Flag{x}"),
        challenge=ChallengeInput("fixture", "test"),
        limits=Limits(
            max_model_turns=2, wall_timeout_seconds=1, command_timeout_seconds=1
        ),
        runs_root=tmp_path,
        monotonic=clock,
        utc_now=lambda: NOW,
        run_id="FA-20260814T000000Z-a13f4c2d",
    )

    result = loop.run()

    assert result["status:reason"] == "unsolved:wall_limit"
    assert result["model_calls"] == 1


def test_executor_exception_after_deadline_is_wall_limit(tmp_path):
    clock = Clock()

    class LateExecutor:
        def __init__(self):
            self.calls = []

        def execute(self, command, timeout_seconds):
            self.calls.append((command, timeout_seconds))
            clock.value = 1
            raise RuntimeError("late executor failure")

    loop = AgentLoop(
        model=ScriptedModel(
            [ModelResponse(tool_calls=(ToolCall("s", "shell", {"command": "x"}),))]
        ),
        executor=LateExecutor(),
        verifier=ExactStringVerifier("Flag{x}"),
        challenge=ChallengeInput("fixture", "test"),
        limits=Limits(
            max_model_turns=2, wall_timeout_seconds=1, command_timeout_seconds=2
        ),
        runs_root=tmp_path,
        monotonic=clock,
        utc_now=lambda: NOW,
        run_id="FA-20260814T000000Z-a13f4c2d",
    )

    assert loop.run()["status:reason"] == "unsolved:wall_limit"


def test_verifier_exception_after_deadline_is_wall_limit(tmp_path):
    clock = Clock()

    class LateVerifier:
        def check(self, candidate):
            clock.value = 1
            raise RuntimeError("late verifier failure")

    loop = AgentLoop(
        model=ScriptedModel(
            [
                ModelResponse(
                    tool_calls=(ToolCall("f", "submit_flag", {"candidate": "Flag{x}"}),)
                )
            ]
        ),
        executor=FakeExecutor([]),
        verifier=LateVerifier(),
        challenge=ChallengeInput("fixture", "test"),
        limits=Limits(
            max_model_turns=2, wall_timeout_seconds=1, command_timeout_seconds=1
        ),
        runs_root=tmp_path,
        monotonic=clock,
        utc_now=lambda: NOW,
        run_id="FA-20260814T000000Z-a13f4c2d",
    )

    assert loop.run()["status:reason"] == "unsolved:wall_limit"


def test_correct_verifier_after_deadline_is_wall_limit(tmp_path):
    clock = Clock()

    class LateVerifier:
        def check(self, candidate):
            clock.value = 1
            return "correct"

    loop = AgentLoop(
        model=ScriptedModel(
            [
                ModelResponse(
                    tool_calls=(ToolCall("f", "submit_flag", {"candidate": "Flag{x}"}),)
                )
            ]
        ),
        executor=FakeExecutor([]),
        verifier=LateVerifier(),
        challenge=ChallengeInput("fixture", "test"),
        limits=Limits(
            max_model_turns=2, wall_timeout_seconds=1, command_timeout_seconds=1
        ),
        runs_root=tmp_path,
        monotonic=clock,
        utc_now=lambda: NOW,
        run_id="FA-20260814T000000Z-a13f4c2d",
    )

    result = loop.run()

    assert result["status:reason"] == "unsolved:wall_limit"


def test_model_response_returned_after_wall_deadline_is_preserved(tmp_path):
    clock = Clock()

    class LateModel:
        def generate(self, messages, tools):
            clock.value = 2
            return ModelResponse(
                content="late",
                tool_calls=(ToolCall("c1", "shell", {"command": "echo hi"}),),
                usage={"input_tokens": 11, "output_tokens": 22},
            )

    executor = FakeExecutor([])
    loop = AgentLoop(
        model=LateModel(),
        executor=executor,
        verifier=ExactStringVerifier("Flag{x}"),
        challenge=ChallengeInput("fixture", "test"),
        limits=Limits(
            max_model_turns=5, wall_timeout_seconds=1, command_timeout_seconds=1
        ),
        runs_root=tmp_path,
        monotonic=clock,
        utc_now=lambda: NOW,
        run_id="FA-20260814T000000Z-a13f4c2d",
    )

    result = loop.run()

    assert result["status:reason"] == "unsolved:wall_limit"
    assert result["model_calls"] == 1
    assert result["input_tokens"] == 11
    assert result["output_tokens"] == 22
    assert result["tool_calls"] == 0
    assert executor.calls == []
    events = read_events(loop.artifacts.events_path)
    model_responses = [e for e in events if e["type"] == "model_response"]
    assert len(model_responses) == 1
    assert model_responses[0]["payload"]["tool_calls"][0]["call_id"] == "c1"
    assert any(m["role"] == "assistant" for m in loop.messages)
    terminal = next(e for e in events if e["type"] == "terminal_decision")
    assert terminal["payload"]["unprocessed_call_ids"] == ["c1"]
    assert not any(e["type"] == "tool_call" for e in events)
