from datetime import UTC, datetime

from flagagent.artifacts import read_events
from flagagent.loop import AgentLoop, ChallengeInput, Limits
from flagagent.model import ModelResponse, ScriptedModel, ToolCall
from flagagent.tools import ExactStringVerifier, FakeExecutor, ShellResult

NOW = datetime(2026, 8, 14, 16, 15, 30, tzinfo=UTC)


class Clock:
    def __init__(self, value=0.0):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, amount):
        self.value += amount


def run_loop(
    tmp_path, responses, executor=None, verifier=None, limits=None, clock=None
):
    loop = AgentLoop(
        model=ScriptedModel(responses),
        executor=executor or FakeExecutor([]),
        verifier=verifier or ExactStringVerifier("Flag{ok}"),
        challenge=ChallengeInput("fixture", "solve it"),
        limits=limits
        or Limits(
            max_model_turns=10, wall_timeout_seconds=100, command_timeout_seconds=10
        ),
        runs_root=tmp_path,
        monotonic=clock or Clock(),
        utc_now=lambda: NOW,
        run_id="FA-20260814T161530Z-a13f4c2d",
    )
    result = loop.run()
    return loop, result


def test_assistant_precedes_correlated_sequential_results(tmp_path):
    executor = FakeExecutor(
        [ShellResult("one", "", 0, False), ShellResult("two", "", 0, False)]
    )
    responses = [
        ModelResponse(
            tool_calls=(
                ToolCall("c1", "shell", {"command": "one"}),
                ToolCall("c2", "shell", {"command": "two"}),
            )
        ),
        ModelResponse(content="stop"),
    ]

    loop, result = run_loop(tmp_path, responses, executor=executor)

    assert result["status:reason"] == "unsolved:model_stop"
    assert executor.calls == [("one", 10), ("two", 10)]
    assert [message["role"] for message in loop.messages] == [
        "user",
        "assistant",
        "tool",
        "tool",
        "assistant",
    ]
    assert [message.get("call_id") for message in loop.messages[2:4]] == ["c1", "c2"]


def test_unknown_and_invalid_calls_recover_without_execution(tmp_path):
    responses = [
        ModelResponse(
            tool_calls=(
                ToolCall("u", "invented", {}),
                ToolCall("i", "shell", {"command": " "}),
            )
        ),
        ModelResponse(content="done"),
    ]

    loop, result = run_loop(tmp_path, responses)

    assert result["reason"] == "model_stop"
    assert [
        message["result"]["error"]["type"]
        for message in loop.messages
        if message["role"] == "tool"
    ] == ["unknown_tool", "invalid_arguments"]


def test_duplicate_call_id_is_provider_error_without_execution(tmp_path):
    response = ModelResponse(
        tool_calls=(
            ToolCall("dup", "shell", {"command": "one"}),
            ToolCall("dup", "shell", {"command": "two"}),
        )
    )
    executor = FakeExecutor([ShellResult("", "", 0, False)])

    loop, result = run_loop(tmp_path, [response], executor=executor)

    assert result["status:reason"] == "error:provider_error"
    assert executor.calls == []
    events = read_events(loop.artifacts.events_path)
    assert events[0]["type"] == "model_response"
    assert events[0]["payload"]["accepted"] is False


def test_reused_call_id_on_later_turn_is_provider_error(tmp_path):
    executor = FakeExecutor([ShellResult("ok", "", 0, False)])
    responses = [
        ModelResponse(tool_calls=(ToolCall("same", "shell", {"command": "one"}),)),
        ModelResponse(tool_calls=(ToolCall("same", "shell", {"command": "two"}),)),
    ]

    _, result = run_loop(tmp_path, responses, executor=executor)

    assert result["status:reason"] == "error:provider_error"
    assert executor.calls == [("one", 10)]


def test_model_text_is_not_success(tmp_path):
    _, result = run_loop(tmp_path, [ModelResponse(content="Flag{ok}")])

    assert result["status:reason"] == "unsolved:model_stop"


def test_wrong_flag_continues_and_correct_flag_short_circuits(tmp_path):
    executor = FakeExecutor(
        [
            ShellResult("after wrong", "", 0, False),
            ShellResult("must not run", "", 0, False),
        ]
    )
    response = ModelResponse(
        tool_calls=(
            ToolCall("w", "submit_flag", {"candidate": "wrong"}),
            ToolCall("s", "shell", {"command": "after-wrong"}),
            ToolCall("c", "submit_flag", {"candidate": "  Flag{ok}\n"}),
            ToolCall("x", "shell", {"command": "skipped"}),
        )
    )

    loop, result = run_loop(tmp_path, [response], executor=executor)

    assert result["status:reason"] == "solved:verified_flag"
    assert executor.calls == [("after-wrong", 10)]
    assert result["flag_submissions"] == 2
    terminal = read_events(loop.artifacts.events_path)[-1]
    assert terminal["payload"]["unprocessed_call_ids"] == ["x"]


def test_nonzero_and_timeout_are_normal_but_executor_failure_is_error(tmp_path):
    executor = FakeExecutor(
        [
            ShellResult("", "bad", 2, False),
            ShellResult("partial", "", None, True),
        ]
    )
    responses = [
        ModelResponse(
            tool_calls=(
                ToolCall("a", "shell", {"command": "bad"}),
                ToolCall("b", "shell", {"command": "slow"}),
            )
        ),
        ModelResponse(content="done"),
    ]
    _, result = run_loop(tmp_path, responses, executor=executor)
    assert result["reason"] == "model_stop"

    _, failed = run_loop(
        tmp_path / "failed",
        [ModelResponse(tool_calls=(ToolCall("e", "shell", {"command": "x"}),))],
        executor=FakeExecutor([RuntimeError("secret")]),
    )
    assert failed["status:reason"] == "error:tool_error"


def test_model_failure_and_verifier_failure_are_distinct_and_safe(tmp_path):
    _, provider = run_loop(tmp_path / "provider", [RuntimeError("provider secret")])
    assert provider["reason"] == "provider_error"

    class FailingVerifier:
        def check(self, candidate):
            raise RuntimeError("Flag{control-secret}")

    loop, verifier = run_loop(
        tmp_path / "verifier",
        [
            ModelResponse(
                tool_calls=(ToolCall("f", "submit_flag", {"candidate": "guess"}),)
            )
        ],
        verifier=FailingVerifier(),
    )
    assert verifier["reason"] == "verifier_error"
    assert "Flag{control-secret}" not in loop.artifacts.events_path.read_text()


def test_exact_turn_limit_allows_final_turn_tools(tmp_path):
    executor = FakeExecutor([ShellResult("ok", "", 0, False)])
    loop, result = run_loop(
        tmp_path,
        [
            ModelResponse(tool_calls=(ToolCall("c", "shell", {"command": "last"}),)),
            ModelResponse(content="never"),
        ],
        executor=executor,
        limits=Limits(
            max_model_turns=1, wall_timeout_seconds=100, command_timeout_seconds=10
        ),
    )

    assert result["status:reason"] == "unsolved:model_turn_limit"
    assert result["model_calls"] == 1
    assert executor.calls == [("last", 10)]
    assert len(loop.model.calls) == 1


def test_remaining_wall_bounds_command_and_wall_crossing_wins(tmp_path):
    clock = Clock(95)

    class AdvancingExecutor(FakeExecutor):
        def execute(self, command, timeout_seconds):
            result = super().execute(command, timeout_seconds)
            clock.advance(5)
            return result

    executor = AdvancingExecutor([ShellResult("late", "", 0, False)])
    _, result = run_loop(
        tmp_path,
        [ModelResponse(tool_calls=(ToolCall("c", "shell", {"command": "x"}),))],
        executor=executor,
        limits=Limits(
            max_model_turns=2, wall_timeout_seconds=5, command_timeout_seconds=60
        ),
        clock=clock,
    )

    assert executor.calls == [("x", 5)]
    assert result["status:reason"] == "unsolved:wall_limit"


def test_result_counters_match_processed_calls(tmp_path):
    response = ModelResponse(
        tool_calls=(
            ToolCall("u", "unknown", {}),
            ToolCall("i", "shell", {"command": " "}),
            ToolCall("f", "submit_flag", {"candidate": "Flag{ok}"}),
            ToolCall("skip", "shell", {"command": "skip"}),
        )
    )
    _, result = run_loop(tmp_path, [response])

    assert result["model_calls"] == 1
    assert result["tool_calls"] == 3
    assert result["flag_submissions"] == 1
