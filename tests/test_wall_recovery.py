import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from flagagent.docker_executor import DockerExecutor
from flagagent.loop import AgentLoop, ChallengeInput, Limits
from flagagent.model import ModelResponse, ScriptedModel, ToolCall
from flagagent.tools import ExactStringVerifier, SandboxError

NOW = datetime(2026, 8, 14, 16, 15, 30, tzinfo=UTC)
RUN_ID = "FA-20260814T161530Z-a13f4c2d"


class _FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _FakePopen:
    def __init__(self, args, **kwargs):
        self.args = list(args)
        self.pid = 4242
        self.returncode = 0

    def wait(self, timeout=None):
        return self.returncode


def test_recovery_wall_exhausted_skips_restart_and_probe(monkeypatch):
    clock = [0.0]

    def mono():
        return clock[0]

    executor = DockerExecutor(monotonic=mono)
    executor._container_id = "cid"
    executor.set_wall_remaining(0.2)

    def fake_collect(proc, deadline):
        clock[0] = 0.3
        return bytearray(b"partial"), bytearray(b""), True, False

    monkeypatch.setattr(executor, "_collect", fake_collect)
    calls: list[tuple[str, float | None]] = []

    def fake_run(args, **kwargs):
        calls.append((args[1], kwargs.get("timeout")))
        if args[1] == "inspect":
            return _FakeCompleted(stdout="true\n")
        return _FakeCompleted()

    monkeypatch.setattr(subprocess, "run", fake_run)
    waits: list[float | None] = []

    def fake_wait(self, timeout=None):
        waits.append(timeout)
        return 0

    monkeypatch.setattr(_FakePopen, "wait", fake_wait)
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(os, "getpgid", lambda pid: 999)
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: None)

    with pytest.raises(SandboxError, match="wall budget exhausted"):
        executor.execute("sleep 1", 0.2)

    kill_calls = [c for c in calls if c[0] == "kill"]
    assert kill_calls
    assert kill_calls[0][1] <= 1.0
    assert waits and waits[0] <= 1.0
    assert not any(c[0] == "start" for c in calls)
    assert not any(c[0] == "exec" for c in calls)
    inspect_running = [c for c in calls if c[0] == "inspect"]
    assert len(inspect_running) == 1


def test_recovery_wall_budget_bounds_containment_timeouts(monkeypatch):
    clock = [0.0]

    def mono():
        return clock[0]

    executor = DockerExecutor(monotonic=mono)
    executor._container_id = "cid"
    executor.set_wall_remaining(5.0)
    clock[0] = 4.95

    monkeypatch.setattr(
        executor,
        "_collect",
        lambda proc, deadline: (bytearray(b""), bytearray(b""), True, False),
    )
    calls: list[tuple[str, float | None]] = []

    def fake_run(args, **kwargs):
        calls.append((args[1], kwargs.get("timeout")))
        if args[1] == "inspect":
            return _FakeCompleted(stdout="true\n")
        return _FakeCompleted()

    monkeypatch.setattr(subprocess, "run", fake_run)
    waits: list[float | None] = []

    def fake_wait(self, timeout=None):
        waits.append(timeout)
        return 0

    monkeypatch.setattr(_FakePopen, "wait", fake_wait)
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(os, "getpgid", lambda pid: 999)
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: None)

    executor.execute("sleep 1", 5)

    kill = [c for c in calls if c[0] == "kill"][0]
    assert kill[1] == pytest.approx(0.05, abs=0.02)
    assert waits[0] == pytest.approx(0.05, abs=0.02)


def test_recovery_budget_remaining_preserves_kill_restart_probe(monkeypatch):
    executor = DockerExecutor(monotonic=lambda: 0.0)
    executor._container_id = "cid"
    executor.set_wall_remaining(60.0)

    monkeypatch.setattr(
        executor,
        "_collect",
        lambda p, d: (bytearray(b"partial"), bytearray(b""), True, False),
    )
    calls: list[tuple[list[str], float | None]] = []

    def fake_run(args, **kwargs):
        calls.append((list(args), kwargs.get("timeout")))
        if "inspect" in args:
            return _FakeCompleted(stdout="true\n")
        return _FakeCompleted()

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(os, "getpgid", lambda pid: 999)
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: None)
    waits: list[float | None] = []

    def fake_wait(self, timeout=None):
        waits.append(timeout)
        return 0

    monkeypatch.setattr(_FakePopen, "wait", fake_wait)

    result = executor.execute("sleep 1", 10)
    assert result.timed_out is True
    seq = [a[1] for a, _ in calls if a[1] in ("kill", "start", "exec")]
    assert seq == ["kill", "start", "exec"]
    assert [t for a, t in calls if a[1] == "kill"][0] == 30
    assert [t for a, t in calls if a[1] == "start"][0] == 60
    assert [t for a, t in calls if a[1] == "exec"][0] == 30
    assert waits == [10]


def test_loop_wall_exhausted_after_timed_shell_resolves_wall_limit(
    tmp_path: Path, monkeypatch
):
    clock = [0.0]

    def mono():
        return clock[0]

    executor = DockerExecutor(monotonic=mono)

    def fake_prepare(workspace, run_id):
        executor._container_id = "cid"
        executor._container_name = executor._container_name_for(run_id)

    monkeypatch.setattr(executor, "prepare", fake_prepare)

    def fake_collect(proc, deadline):
        clock[0] = 1.0
        return bytearray(b"partial"), bytearray(b""), True, False

    monkeypatch.setattr(executor, "_collect", fake_collect)

    def fake_run(args, **kwargs):
        if args[1] == "inspect":
            return _FakeCompleted(stdout="true\n")
        return _FakeCompleted()

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(os, "getpgid", lambda pid: 999)
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: None)
    monkeypatch.setattr(_FakePopen, "wait", lambda self, timeout=None: 0)

    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=(ToolCall("c1", "shell", {"command": "sleep 1000"}),)
            )
        ]
    )
    loop = AgentLoop(
        model=model,
        executor=executor,
        verifier=ExactStringVerifier("Flag{ok}"),
        challenge=ChallengeInput("fixture", "solve it"),
        limits=Limits(
            max_model_turns=5, wall_timeout_seconds=1, command_timeout_seconds=10
        ),
        runs_root=tmp_path,
        monotonic=mono,
        utc_now=lambda: NOW,
        run_id=RUN_ID,
    )
    result = loop.run()
    assert result["status:reason"] == "unsolved:wall_limit"


def test_loop_timed_out_with_budget_returns_normal_evidence(
    tmp_path: Path, monkeypatch
):
    executor = DockerExecutor(monotonic=lambda: 0.0)

    def fake_prepare(workspace, run_id):
        executor._container_id = "cid"
        executor._container_name = executor._container_name_for(run_id)

    monkeypatch.setattr(executor, "prepare", fake_prepare)
    monkeypatch.setattr(
        executor,
        "_collect",
        lambda p, d: (bytearray(b"out"), bytearray(b""), True, False),
    )

    def fake_run(args, **kwargs):
        if args[1] == "inspect":
            return _FakeCompleted(stdout="true\n")
        return _FakeCompleted()

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(os, "getpgid", lambda pid: 999)
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: None)
    monkeypatch.setattr(_FakePopen, "wait", lambda self, timeout=None: 0)

    model = ScriptedModel(
        [
            ModelResponse(tool_calls=(ToolCall("c1", "shell", {"command": "slow"}),)),
            ModelResponse(content="stop"),
        ]
    )
    loop = AgentLoop(
        model=model,
        executor=executor,
        verifier=ExactStringVerifier("Flag{ok}"),
        challenge=ChallengeInput("fixture", "solve it"),
        limits=Limits(
            max_model_turns=5, wall_timeout_seconds=100, command_timeout_seconds=10
        ),
        runs_root=tmp_path,
        monotonic=lambda: 0.0,
        utc_now=lambda: NOW,
        run_id=RUN_ID,
    )
    result = loop.run()
    assert result["status:reason"] == "unsolved:model_stop"
    assert any(
        m.get("result", {}).get("timed_out") is True
        for m in loop.messages
        if m.get("role") == "tool"
    )
