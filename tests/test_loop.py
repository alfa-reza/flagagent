from datetime import UTC, datetime

from flagagent.artifacts import read_events
from flagagent.loop import AgentLoop, ChallengeInput, Limits
from flagagent.model import ModelResponse, ScriptedModel, ToolCall
from flagagent.tools import ExactStringVerifier, FakeExecutor, SandboxError, ShellResult

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
    executor = FakeExecutor([ShellResult("", "", 0, False)])
    responses = [
        ModelResponse(
            tool_calls=(
                ToolCall("u", "invented", {}),
                ToolCall("i", "shell", {"command": " "}),
            )
        ),
        ModelResponse(content="done"),
    ]

    loop, result = run_loop(tmp_path, responses, executor=executor)

    assert result["reason"] == "model_stop"
    assert executor.calls == []
    assert [
        message["result"]["error"]["type"]
        for message in loop.messages
        if message["role"] == "tool"
    ] == ["unknown_tool", "invalid_arguments"]
    events = read_events(loop.artifacts.events_path)
    tool_calls = [event["payload"] for event in events if event["type"] == "tool_call"]
    tool_results = [
        event["payload"] for event in events if event["type"] == "tool_result"
    ]
    assert [item["call_id"] for item in tool_calls] == ["u", "i"]
    assert [item["executed"] for item in tool_results] == [False, False]
    assert [item["result"]["error"]["type"] for item in tool_results] == [
        "unknown_tool",
        "invalid_arguments",
    ]


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

    loop, result = run_loop(tmp_path, responses, executor=executor)

    assert result["status:reason"] == "error:provider_error"
    assert executor.calls == [("one", 10)]
    events = read_events(loop.artifacts.events_path)
    second = [event for event in events if event["type"] == "model_response"][1]
    assert second["payload"]["accepted"] is False
    assert len([event for event in events if event["type"] == "tool_call"]) == 1


class ComponentBoom(Exception):
    pass


def test_arbitrary_exception_subclasses_map_to_error_taxonomy(tmp_path):
    class BoomModel:
        def generate(self, messages, tools):
            raise ComponentBoom("model boundary")

    class BoomExecutor:
        def execute(self, command, timeout_seconds):
            raise ComponentBoom("executor boundary")

    class BoomVerifier:
        def check(self, candidate):
            raise ComponentBoom("verifier boundary")

    shell_response = ModelResponse(
        tool_calls=(ToolCall("s", "shell", {"command": "x"}),)
    )
    flag_response = ModelResponse(
        tool_calls=(ToolCall("f", "submit_flag", {"candidate": "guess"}),)
    )

    provider = run_loop(
        tmp_path / "p", [ComponentBoom("model boundary")], executor=FakeExecutor([])
    )
    tool = run_loop(tmp_path / "t", [shell_response], executor=BoomExecutor())
    verify = run_loop(tmp_path / "v", [flag_response], verifier=BoomVerifier())

    assert provider[1]["status:reason"] == "error:provider_error"
    assert tool[1]["status:reason"] == "error:tool_error"
    assert verify[1]["status:reason"] == "error:verifier_error"
    for loop in (provider[0], tool[0], verify[0]):
        errors = [
            event["payload"]
            for event in read_events(loop.artifacts.events_path)
            if event["type"] == "error"
        ]
        assert errors and set(errors[0]) <= {"reason", "operation", "call_id"}
        assert loop.artifacts.result_path.exists()


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
    tool_results = [
        message["result"] for message in loop.messages if message["role"] == "tool"
    ]
    assert tool_results[0] == {"outcome": "incorrect"}
    assert tool_results[1]["stdout"] == "after wrong"
    assert tool_results[2] == {"outcome": "correct"}
    verifier_outcomes = [
        event["payload"]["outcome"]
        for event in read_events(loop.artifacts.events_path)
        if event["type"] == "verifier_result"
    ]
    assert verifier_outcomes == ["incorrect", "correct"]


def test_case_mismatched_candidate_does_not_solve(tmp_path):
    loop, result = run_loop(
        tmp_path,
        [
            ModelResponse(
                tool_calls=(ToolCall("f", "submit_flag", {"candidate": "flag{ok}"}),)
            ),
            ModelResponse(content="stop"),
        ],
    )

    assert result["status:reason"] == "unsolved:model_stop"
    assert result["flag_submissions"] == 1
    assert loop.messages[-2]["result"] == {"outcome": "incorrect"}


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
    loop, result = run_loop(tmp_path, responses, executor=executor)
    assert result["reason"] == "model_stop"
    timeout_seen = loop.model.calls[1][0][-1]["result"]
    assert timeout_seen["exit_code"] is None
    assert timeout_seen["timed_out"] is True
    model_seen = loop.model.calls[1][0][-2]["result"]
    assert model_seen["exit_code"] == 2
    assert model_seen["timed_out"] is False

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


def test_loop_passes_remaining_budget_to_adapter_seam(tmp_path):
    recorded = []

    class _BudgetAdapter:
        def set_remaining(self, remaining):
            recorded.append(remaining)

        def generate(self, messages, tools):
            return ModelResponse(content="stop")

    loop = AgentLoop(
        model=_BudgetAdapter(),
        executor=FakeExecutor([]),
        verifier=ExactStringVerifier("Flag{ok}"),
        challenge=ChallengeInput("fixture", "solve it"),
        limits=Limits(
            max_model_turns=10, wall_timeout_seconds=100, command_timeout_seconds=10
        ),
        runs_root=tmp_path,
        monotonic=Clock(),
        utc_now=lambda: NOW,
        run_id="FA-20260814T161530Z-a13f4c2d",
    )
    result = loop.run()

    assert result["status:reason"] == "unsolved:model_stop"
    assert recorded == [100.0]


class _FirstCallExpiredClock:
    """Returns 0 on the first call (for _started), then a large value."""

    def __init__(self):
        self._first = True

    def __call__(self):
        if self._first:
            self._first = False
            return 0.0
        return 100.0


def test_wall_budget_expired_before_preparation_skips_staging_and_prepare(tmp_path):
    """If the Run wall budget is already exhausted before staging, neither
    staging, prepare, nor model execution starts; the Run is wall_limit."""

    executor = FakeExecutor([])
    loop = AgentLoop(
        model=ScriptedModel([ModelResponse(content="never")]),
        executor=executor,
        verifier=ExactStringVerifier("Flag{ok}"),
        challenge=ChallengeInput("fixture", "solve it"),
        limits=Limits(
            max_model_turns=2, wall_timeout_seconds=1, command_timeout_seconds=1
        ),
        runs_root=tmp_path,
        monotonic=_FirstCallExpiredClock(),
        utc_now=lambda: NOW,
        run_id="FA-20260814T161530Z-a13f4c2d",
    )
    result = loop.run()

    assert result["status:reason"] == "unsolved:wall_limit"
    assert result["model_calls"] == 0
    assert executor.prepared == []


def test_set_remaining_passed_to_executor_before_prepare(tmp_path):
    """The loop calls set_remaining on executors that expose it before
    prepare, so preparation-time timeouts can be bounded by the shared
    Run wall budget."""

    executor = FakeExecutor([ShellResult("ok", "", 0, False)])
    responses = [
        ModelResponse(tool_calls=(ToolCall("s", "shell", {"command": "x"}),)),
        ModelResponse(content="done"),
    ]
    loop = AgentLoop(
        model=ScriptedModel(responses),
        executor=executor,
        verifier=ExactStringVerifier("Flag{ok}"),
        challenge=ChallengeInput("fixture", "solve it"),
        limits=Limits(
            max_model_turns=10, wall_timeout_seconds=100, command_timeout_seconds=10
        ),
        runs_root=tmp_path,
        monotonic=Clock(),
        utc_now=lambda: NOW,
        run_id="FA-20260814T161530Z-a13f4c2d",
    )
    result = loop.run()

    assert result["status:reason"] == "unsolved:model_stop"
    assert executor.remaining_budgets == [100.0]


def test_prepare_sandbox_error_after_deadline_is_wall_limit(tmp_path):
    """A SandboxError from prepare after the wall deadline is wall_limit,
    not sandbox_error, and model execution does not begin."""

    clock = Clock()

    class LatePrepareExecutor(FakeExecutor):
        def prepare(self, workspace, run_id):
            super().prepare(workspace, run_id)
            clock.value = 100
            raise SandboxError("preparation exceeded budget")

    executor = LatePrepareExecutor([])
    loop = AgentLoop(
        model=ScriptedModel([ModelResponse(content="never")]),
        executor=executor,
        verifier=ExactStringVerifier("Flag{ok}"),
        challenge=ChallengeInput("fixture", "solve it"),
        limits=Limits(
            max_model_turns=2, wall_timeout_seconds=1, command_timeout_seconds=1
        ),
        runs_root=tmp_path,
        monotonic=clock,
        utc_now=lambda: NOW,
        run_id="FA-20260814T161530Z-a13f4c2d",
    )
    result = loop.run()

    assert result["status:reason"] == "unsolved:wall_limit"
    assert result["model_calls"] == 0
    assert executor.prepared != []


def test_wall_expired_after_successful_prepare_skips_model(tmp_path):
    """If the wall deadline expires during a successful prepare, model
    execution does not begin and the Run returns wall_limit."""

    clock = Clock()

    class LatePrepareExecutor(FakeExecutor):
        def prepare(self, workspace, run_id):
            super().prepare(workspace, run_id)
            clock.value = 100

    executor = LatePrepareExecutor([ShellResult("ok", "", 0, False)])
    loop = AgentLoop(
        model=ScriptedModel([ModelResponse(content="never")]),
        executor=executor,
        verifier=ExactStringVerifier("Flag{ok}"),
        challenge=ChallengeInput("fixture", "solve it"),
        limits=Limits(
            max_model_turns=2, wall_timeout_seconds=1, command_timeout_seconds=1
        ),
        runs_root=tmp_path,
        monotonic=clock,
        utc_now=lambda: NOW,
        run_id="FA-20260814T161530Z-a13f4c2d",
    )
    result = loop.run()

    assert result["status:reason"] == "unsolved:wall_limit"
    assert result["model_calls"] == 0
    assert executor.prepared != []


def test_staging_respects_active_deadline(tmp_path):
    """Staging checks the wall deadline between file copies; if the deadline
    expires during staging, the Run returns wall_limit without calling prepare
    or the model."""

    source = tmp_path / "source"
    source.mkdir()
    (source / "a.txt").write_text("a")
    (source / "b.txt").write_text("b")

    class SteppingClock:
        def __init__(self):
            self.value = 0.0

        def __call__(self):
            result = self.value
            self.value += 0.5
            return result

    executor = FakeExecutor([])
    loop = AgentLoop(
        model=ScriptedModel([ModelResponse(content="never")]),
        executor=executor,
        verifier=ExactStringVerifier("Flag{ok}"),
        challenge=ChallengeInput("files", "solve it", source_dir=source),
        limits=Limits(
            max_model_turns=2, wall_timeout_seconds=1, command_timeout_seconds=1
        ),
        runs_root=tmp_path,
        monotonic=SteppingClock(),
        utc_now=lambda: NOW,
        run_id="FA-20260814T161530Z-a13f4c2d",
    )
    result = loop.run()

    assert result["status:reason"] == "unsolved:wall_limit"
    assert result["model_calls"] == 0
    assert executor.prepared == []


def test_truncated_model_response_does_not_execute_tools_and_records_limit(tmp_path):
    executor = FakeExecutor([ShellResult("should not run", "", 0, False)])
    responses = [
        ModelResponse(
            content="partial",
            usage={"input_tokens": 5, "output_tokens": 7},
            truncated=True,
        ),
    ]
    loop, result = run_loop(tmp_path, responses, executor=executor)

    assert result["status:reason"] == "unsolved:model_output_limit"
    assert result["input_tokens"] == 5
    assert result["output_tokens"] == 7
    assert executor.calls == []
    events = read_events(loop.artifacts.events_path)
    assert [e["type"] for e in events if e["type"] == "tool_call"] == []
    model_events = [e for e in events if e["type"] == "model_response"]
    assert model_events[0]["payload"]["truncated"] is True
    assert model_events[0]["payload"]["content"] == "partial"
    assistant_msgs = [m for m in loop.messages if m["role"] == "assistant"]
    assert assistant_msgs[0]["content"] == "partial"


def test_truncated_response_with_tool_calls_does_not_execute_partial_tools(tmp_path):
    executor = FakeExecutor([ShellResult("should not run", "", 0, False)])
    responses = [
        ModelResponse(
            content="partial",
            tool_calls=(ToolCall("c1", "shell", {"command": "rm -rf /"}),),
            truncated=True,
        ),
    ]
    loop, result = run_loop(tmp_path, responses, executor=executor)

    assert result["status:reason"] == "unsolved:model_output_limit"
    assert executor.calls == []
    events = read_events(loop.artifacts.events_path)
    assert [e["type"] for e in events if e["type"] == "tool_call"] == []
    assert result["tool_calls"] == 0


def test_natural_model_stop_is_not_truncation(tmp_path):
    _, result = run_loop(tmp_path, [ModelResponse(content="done")])
    assert result["status:reason"] == "unsolved:model_stop"

    _, result2 = run_loop(
        tmp_path / "second",
        [ModelResponse(content="done", truncated=False)],
    )
    assert result2["status:reason"] == "unsolved:model_stop"


def test_provider_error_still_maps_to_error(tmp_path):
    _, result = run_loop(tmp_path, [RuntimeError("provider down")])
    assert result["status:reason"] == "error:provider_error"
