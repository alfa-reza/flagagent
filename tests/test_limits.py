import os
import stat
from datetime import UTC, datetime

import pytest

from flagagent.artifacts import read_events
from flagagent.loop import (
    AgentLoop,
    ChallengeInput,
    InvalidChallengeSourceError,
    Limits,
    _snapshot_source_files,
)
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
        {"max_source_file_bytes": 0},
        {"max_source_file_bytes": True},
        {"max_source_total_bytes": -1},
        {"max_source_total_bytes": False},
        {"max_source_files": 0},
        {"max_source_files": True},
        {"max_source_entries": 0},
        {"max_source_entries": True},
        {"max_source_depth": 0},
        {"max_source_depth": True},
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


# -- source ingestion limits ------------------------------------------------


def _make_source(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    return source


def _snapshot_ok(source_dir, limits):
    files, digest, temporary = _snapshot_source_files(source_dir, limits)
    assert temporary is not None
    try:
        return files, digest
    finally:
        temporary.cleanup()


def test_individual_source_file_size_limit_rejected(tmp_path):
    source = _make_source(tmp_path)
    (source / "big.bin").write_bytes(b"x" * 11)
    limits = Limits(max_source_file_bytes=10)
    with pytest.raises(InvalidChallengeSourceError, match="file too large"):
        _snapshot_source_files(source, limits)


def test_individual_source_file_size_exact_boundary(tmp_path):
    source = _make_source(tmp_path)
    (source / "exact.bin").write_bytes(b"x" * 10)
    limits = Limits(max_source_file_bytes=10)
    files, _ = _snapshot_ok(source, limits)
    assert sorted(relative.as_posix() for _, relative in files) == ["exact.bin"]


def test_aggregate_source_byte_limit_rejected(tmp_path):
    source = _make_source(tmp_path)
    (source / "a.txt").write_bytes(b"a" * 6)
    (source / "b.txt").write_bytes(b"b" * 6)
    limits = Limits(max_source_total_bytes=10)
    with pytest.raises(InvalidChallengeSourceError, match="source too large"):
        _snapshot_source_files(source, limits)


def test_aggregate_source_byte_exact_boundary(tmp_path):
    source = _make_source(tmp_path)
    (source / "a.txt").write_bytes(b"a" * 6)
    (source / "b.txt").write_bytes(b"b" * 6)
    limits = Limits(max_source_total_bytes=12)
    files, _ = _snapshot_ok(source, limits)
    assert sorted(relative.as_posix() for _, relative in files) == ["a.txt", "b.txt"]


def test_source_file_count_limit_rejected(tmp_path):
    source = _make_source(tmp_path)
    for i in range(3):
        (source / f"file{i}.txt").write_text("data")
    limits = Limits(max_source_files=2)
    with pytest.raises(InvalidChallengeSourceError, match="too many files"):
        _snapshot_source_files(source, limits)


def test_source_file_count_exact_boundary(tmp_path):
    source = _make_source(tmp_path)
    for i in range(2):
        (source / f"file{i}.txt").write_text("data")
    limits = Limits(max_source_files=2)
    files, _ = _snapshot_ok(source, limits)
    assert sorted(relative.as_posix() for _, relative in files) == [
        "file0.txt",
        "file1.txt",
    ]


def test_source_entry_breadth_global_across_directories(tmp_path):
    source = _make_source(tmp_path)
    (source / "d1").mkdir()
    (source / "d2").mkdir()
    (source / "d1" / "a.txt").write_text("a")
    (source / "d1" / "b.txt").write_text("b")
    (source / "d2" / "c.txt").write_text("c")
    (source / "d2" / "d.txt").write_text("d")
    limits = Limits(max_source_entries=4)
    with pytest.raises(InvalidChallengeSourceError, match="too many entries"):
        _snapshot_source_files(source, limits)


def test_source_entry_count_includes_empty_directories(tmp_path):
    source = _make_source(tmp_path)
    for i in range(5):
        (source / f"empty{i}").mkdir()
    limits = Limits(max_source_entries=4)
    with pytest.raises(InvalidChallengeSourceError, match="too many entries"):
        _snapshot_source_files(source, limits)


def test_source_entry_count_exact_boundary(tmp_path):
    source = _make_source(tmp_path)
    for i in range(3):
        (source / f"entry{i}.txt").write_text(str(i))
    limits = Limits(max_source_entries=3)
    files, _ = _snapshot_ok(source, limits)
    assert sorted(relative.as_posix() for _, relative in files) == [
        "entry0.txt",
        "entry1.txt",
        "entry2.txt",
    ]


def test_source_depth_at_max_allowed(tmp_path):
    source = _make_source(tmp_path)
    deep = source / "d1" / "d2"
    deep.mkdir(parents=True)
    (deep / "file.txt").write_text("data")
    limits = Limits(max_source_depth=2)
    files, _ = _snapshot_ok(source, limits)
    assert [relative.as_posix() for _, relative in files] == ["d1/d2/file.txt"]


def test_source_depth_one_over_max_rejected(tmp_path):
    source = _make_source(tmp_path)
    deeper = source / "d1" / "d2" / "d3"
    deeper.mkdir(parents=True)
    (deeper / "file.txt").write_text("data")
    limits = Limits(max_source_depth=2)
    with pytest.raises(InvalidChallengeSourceError, match="too deep"):
        _snapshot_source_files(source, limits)


def test_incremental_enforcement_catches_growth_after_fstat(tmp_path, monkeypatch):
    source = _make_source(tmp_path)
    (source / "growing.txt").write_bytes(b"x" * 100)

    real_fstat = os.fstat

    def fake_fstat(fd):
        st = real_fstat(fd)
        if stat.S_ISREG(st.st_mode) and st.st_size > 10:
            return os.stat_result(
                (
                    st.st_mode,
                    st.st_ino,
                    st.st_dev,
                    st.st_nlink,
                    st.st_uid,
                    st.st_gid,
                    5,
                    st.st_atime,
                    st.st_mtime,
                    st.st_ctime,
                )
            )
        return st

    monkeypatch.setattr(os, "fstat", fake_fstat)

    limits = Limits(max_source_file_bytes=10, max_source_total_bytes=1000)
    with pytest.raises(InvalidChallengeSourceError, match="file too large"):
        _snapshot_source_files(source, limits)


def test_sparse_file_rejected_by_logical_size(tmp_path):
    source = _make_source(tmp_path)
    sparse = source / "sparse.bin"
    with sparse.open("wb") as handle:
        handle.truncate(1024 * 1024)
    limits = Limits(max_source_file_bytes=1024)
    with pytest.raises(InvalidChallengeSourceError, match="file too large"):
        _snapshot_source_files(source, limits)


def test_over_limit_source_does_not_prepare_or_run_model(tmp_path):
    source = _make_source(tmp_path)
    (source / "big.txt").write_bytes(b"x" * 100)
    executor = FakeExecutor([])
    model = ScriptedModel([ModelResponse(content="never called")])
    loop = AgentLoop(
        model=model,
        executor=executor,
        verifier=ExactStringVerifier("Flag{x}"),
        challenge=ChallengeInput("files", "inspect files", source_dir=source),
        limits=Limits(
            max_model_turns=5,
            wall_timeout_seconds=100,
            command_timeout_seconds=10,
            max_source_file_bytes=10,
        ),
        runs_root=tmp_path,
        monotonic=Clock(),
        utc_now=lambda: NOW,
        run_id="FA-20260814T000000Z-a13f4c2d",
    )
    result = loop.run()
    assert result["status:reason"] == "error:invalid_challenge_source"
    assert executor.prepared == []
    assert executor.calls == []
    assert model.calls == []


def test_snapshot_single_arg_backward_compat(tmp_path):
    source = _make_source(tmp_path)
    (source / "ok.txt").write_text("ok")
    files, digest, temporary = _snapshot_source_files(source)
    try:
        assert len(files) == 1
        assert len(digest) == 64
    finally:
        if temporary is not None:
            temporary.cleanup()


def test_to_dict_includes_source_limits():
    values = Limits().to_dict()
    assert values["max_source_file_bytes"] == 10 * 1024 * 1024
    assert values["max_source_total_bytes"] == 50 * 1024 * 1024
    assert values["max_source_files"] == 1024
    assert values["max_source_entries"] == 2048
    assert values["max_source_depth"] == 16
