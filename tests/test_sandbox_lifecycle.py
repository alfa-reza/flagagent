"""M1 lifecycle slice: executor prepare/cleanup contract and sandbox_error mapping.

These tests define the smallest Docker-backed execution lifecycle that AgentLoop
must support without redesigning the M0 loop:

- ``prepare`` is called after RunArtifacts creates the workspace and before
  ``_run_active`` runs the model.
- ``cleanup`` is called best-effort after the terminal result is committed and
  never rewrites ``result.json``.
- ``SandboxError`` from preparation or execution maps to ``error:sandbox_error``;
  ordinary non-zero shell exit and non-sandbox executor exceptions stay distinct.

FakeExecutor supports the lifecycle hooks as no-ops. Executors that do not
implement the hooks (M0 minimal executors) keep working.
"""

import json
from datetime import UTC, datetime

from flagagent.artifacts import read_events
from flagagent.loop import AgentLoop, ChallengeInput, Limits
from flagagent.model import ModelResponse, ScriptedModel, ToolCall
from flagagent.tools import (
    ExactStringVerifier,
    FakeExecutor,
    SandboxError,
    ShellResult,
)

NOW = datetime(2026, 8, 14, 16, 15, 30, tzinfo=UTC)
RUN_ID = "FA-20260814T161530Z-a13f4c2d"


class Clock:
    def __init__(self, value=0.0):
        self.value = value

    def __call__(self):
        return self.value


def build_loop(
    tmp_path,
    responses,
    executor=None,
    verifier=None,
    limits=None,
    run_id=RUN_ID,
):
    return AgentLoop(
        model=ScriptedModel(responses),
        executor=executor or FakeExecutor([]),
        verifier=verifier or ExactStringVerifier("Flag{ok}"),
        challenge=ChallengeInput("fixture", "solve it"),
        limits=limits
        or Limits(
            max_model_turns=10, wall_timeout_seconds=100, command_timeout_seconds=10
        ),
        runs_root=tmp_path,
        monotonic=Clock(),
        utc_now=lambda: NOW,
        run_id=run_id,
    )


def test_prepare_runs_after_workspace_creation_and_before_model(tmp_path):
    class RecordingPrepare(FakeExecutor):
        def __init__(self, script):
            super().__init__(script)
            self.workspace_existed_at_prepare = None

        def prepare(self, workspace, run_id):
            super().prepare(workspace, run_id)
            self.workspace_existed_at_prepare = workspace.is_dir()

    executor = RecordingPrepare([ShellResult("ok", "", 0, False)])
    responses = [
        ModelResponse(tool_calls=(ToolCall("s", "shell", {"command": "x"}),)),
        ModelResponse(content="done"),
    ]
    loop = build_loop(tmp_path, responses, executor=executor)
    result = loop.run()

    # prepare was called once with the run workspace and run id
    assert len(executor.prepared) == 1
    workspace, run_id = executor.prepared[0]
    assert run_id == loop.artifacts.run_id
    assert workspace == loop.artifacts.workspace
    # the workspace already existed when prepare was called
    assert executor.workspace_existed_at_prepare is True

    # the model ran, so prepare happened before _run_active
    assert result["model_calls"] == 2
    assert result["status:reason"] == "unsolved:model_stop"


def test_cleanup_runs_after_terminal_result_commit(tmp_path):
    executor = FakeExecutor([ShellResult("ok", "", 0, False)])
    responses = [
        ModelResponse(tool_calls=(ToolCall("s", "shell", {"command": "x"}),)),
        ModelResponse(content="done"),
    ]
    loop = build_loop(tmp_path, responses, executor=executor)
    loop.run()

    assert executor.cleaned == [loop.artifacts.run_id]
    assert loop.artifacts.result_path.exists()


def test_prepare_sandbox_error_maps_to_error_sandbox_error_without_running_model(
    tmp_path,
):
    class PrepareFails(FakeExecutor):
        def prepare(self, workspace, run_id):
            super().prepare(workspace, run_id)
            raise SandboxError("container create failed")

    executor = PrepareFails([])
    loop = build_loop(tmp_path, [ModelResponse(content="never")], executor=executor)
    result = loop.run()

    assert result["status:reason"] == "error:sandbox_error"
    # the model never ran because preparation failed before _run_active
    assert result["model_calls"] == 0
    assert loop.model.calls == []

    events = read_events(loop.artifacts.events_path)
    error_events = [event for event in events if event["type"] == "error"]
    assert len(error_events) == 1
    payload = error_events[0]["payload"]
    assert payload["reason"] == "sandbox_error"
    assert payload["operation"] == "sandbox"
    assert "call_id" not in payload

    # cleanup still ran best-effort even though preparation failed
    assert executor.cleaned == [loop.artifacts.run_id]


def test_execute_sandbox_error_maps_to_error_sandbox_error(tmp_path):
    class ExecuteSandboxFails(FakeExecutor):
        def execute(self, command, timeout_seconds):
            self.calls.append((command, timeout_seconds))
            raise SandboxError("agent container is gone")

    executor = ExecuteSandboxFails([])
    responses = [
        ModelResponse(tool_calls=(ToolCall("s", "shell", {"command": "x"}),)),
    ]
    loop = build_loop(tmp_path, responses, executor=executor)
    result = loop.run()

    assert result["status:reason"] == "error:sandbox_error"

    events = read_events(loop.artifacts.events_path)
    error_events = [event for event in events if event["type"] == "error"]
    assert len(error_events) == 1
    payload = error_events[0]["payload"]
    assert payload["reason"] == "sandbox_error"
    assert payload["operation"] == "sandbox"
    assert payload["call_id"] == "s"


def test_non_sandbox_executor_exception_still_maps_to_tool_error(tmp_path):
    executor = FakeExecutor([RuntimeError("command boundary failure")])
    responses = [
        ModelResponse(tool_calls=(ToolCall("s", "shell", {"command": "x"}),)),
    ]
    loop = build_loop(tmp_path, responses, executor=executor)
    result = loop.run()

    assert result["status:reason"] == "error:tool_error"

    events = read_events(loop.artifacts.events_path)
    error_events = [event for event in events if event["type"] == "error"]
    assert len(error_events) == 1
    assert error_events[0]["payload"]["operation"] == "executor"


def test_nonzero_exit_and_timeout_remain_normal_shell_evidence(tmp_path):
    executor = FakeExecutor(
        [ShellResult("", "bad", 2, False), ShellResult("partial", "", None, True)]
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
    loop = build_loop(tmp_path, responses, executor=executor)
    result = loop.run()

    assert result["status:reason"] == "unsolved:model_stop"
    assert result["tool_calls"] == 2


def test_cleanup_failure_is_recorded_without_rewriting_committed_result(tmp_path):
    class CleanupFails(FakeExecutor):
        def cleanup(self, run_id):
            super().cleanup(run_id)
            raise RuntimeError("docker remove failed")

    executor = CleanupFails([ShellResult("ok", "", 0, False)])
    responses = [
        ModelResponse(tool_calls=(ToolCall("s", "shell", {"command": "x"}),)),
        ModelResponse(content="done"),
    ]
    loop = build_loop(tmp_path, responses, executor=executor)
    result = loop.run()

    # the committed terminal result is unchanged by the cleanup failure
    committed = json.loads(loop.artifacts.result_path.read_text())
    assert committed["status:reason"] == "unsolved:model_stop"
    assert result["status:reason"] == "unsolved:model_stop"

    # the cleanup failure is recorded as a separate event after the terminal
    # decision, never as a second result.json replacement
    events = read_events(loop.artifacts.events_path)
    types = [event["type"] for event in events]
    assert "terminal_decision" in types
    assert "sandbox_cleanup_failed" in types
    assert types.index("terminal_decision") < types.index("sandbox_cleanup_failed")

    result_replacements = [
        event for event in events if event["type"] == "sandbox_cleanup_failed"
    ]
    assert len(result_replacements) == 1
    assert "error_type" in result_replacements[0]["payload"]


def test_cleanup_runs_even_when_preparation_failed(tmp_path):
    class PrepareFails(FakeExecutor):
        def prepare(self, workspace, run_id):
            super().prepare(workspace, run_id)
            raise SandboxError("prepare failed")

    executor = PrepareFails([])
    loop = build_loop(tmp_path, [ModelResponse(content="never")], executor=executor)
    loop.run()

    assert executor.prepared == [(loop.artifacts.workspace, loop.artifacts.run_id)]
    assert executor.cleaned == [loop.artifacts.run_id]


def test_executor_without_lifecycle_hooks_still_runs(tmp_path):
    """An executor that only implements execute (M0 minimal shape) still works."""

    class MinimalExecutor:
        def __init__(self):
            self.calls = []

        def execute(self, command, timeout_seconds):
            self.calls.append((command, timeout_seconds))
            return ShellResult("out", "", 0, False)

    executor = MinimalExecutor()
    responses = [
        ModelResponse(tool_calls=(ToolCall("s", "shell", {"command": "x"}),)),
        ModelResponse(content="done"),
    ]
    loop = build_loop(tmp_path, responses, executor=executor)
    result = loop.run()

    assert result["status:reason"] == "unsolved:model_stop"
    assert executor.calls == [("x", 10)]
    assert loop.artifacts.result_path.exists()


def test_sandbox_error_is_a_runtime_error_subclass():
    assert issubclass(SandboxError, RuntimeError)
