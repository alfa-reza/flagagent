import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from flagagent.artifacts import (
    EventStreamPoisoned,
    RunArtifacts,
    read_events,
    validate_run_id,
)

FIXED_TIME = datetime(2026, 8, 14, 16, 15, 30, tzinfo=UTC)


def metadata():
    return {
        "schema_version": 1,
        "run_id": "FA-20260814T161530Z-a13f4c2d",
        "flagagent_version": "0.1.0",
        "concept_version": "0.1.0",
        "challenge": {"identity": "fixture", "description": "test"},
        "started_at": "2026-08-14T16:15:30Z",
        "limits": {"max_model_turns": 1},
    }


def test_create_run_artifacts_without_overwriting(tmp_path):
    artifacts = RunArtifacts.create(
        tmp_path,
        metadata(),
        run_id="FA-20260814T161530Z-a13f4c2d",
        now=lambda: FIXED_TIME,
    )

    assert artifacts.workspace.is_dir()
    assert artifacts.events_path.read_text() == ""
    assert json.loads(artifacts.run_path.read_text()) == metadata()
    assert not artifacts.result_path.exists()

    with pytest.raises(FileExistsError):
        RunArtifacts.create(
            tmp_path,
            metadata(),
            run_id=artifacts.run_id,
            now=lambda: FIXED_TIME,
        )
    assert json.loads(artifacts.run_path.read_text()) == metadata()


def test_generated_run_id_matches_contract():
    run_id = RunArtifacts.generate_run_id(
        now=lambda: FIXED_TIME, token_hex=lambda _: "a13f4c2d"
    )

    assert run_id == "FA-20260814T161530Z-a13f4c2d"


# ---------------------------------------------------------------------------
# run ID trust-boundary validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_id",
    [
        "",  # empty
        "FA/../escape",  # path separator + traversal
        "FA/x",  # path separator
        "FA\\x",  # windows path separator
        "FA:x",  # colon (Docker name:tag delimiter)
        "FA x",  # whitespace
        "FA\tx",
        "FA\nx",
        "FA@sha256:abc",  # Docker digest/spec delimiter
        "FA=x",  # label delimiter
        "FA,x",  # Docker list delimiter
        ".hidden",  # leading separator
        "-lead",
        "FA..x",  # traversal run even without separators
        "FA/x:y z",  # combined
        "F" * 200,  # too long for container/network name prefixes
        123,  # wrong type
        None,
    ],
)
def test_create_rejects_unsafe_run_ids_before_artifact_creation(tmp_path, bad_id):
    before = sorted(p.name for p in tmp_path.iterdir())
    with pytest.raises(ValueError, match="run id"):
        RunArtifacts.create(
            tmp_path,
            {**metadata(), "run_id": bad_id},
            run_id=bad_id,
            now=lambda: FIXED_TIME,
        )
    # nothing was created on disk — rejection happens before artifact creation
    assert sorted(p.name for p in tmp_path.iterdir()) == before


@pytest.mark.parametrize(
    "safe_id",
    ["FA-20260814T161530Z-a13f4c2d", "FA-TEST-abcd1234", "fa.1_x-2", "A1", "a"],
)
def test_create_accepts_safe_run_ids(tmp_path, safe_id):
    artifacts = RunArtifacts.create(
        tmp_path,
        {**metadata(), "run_id": safe_id},
        run_id=safe_id,
        now=lambda: FIXED_TIME,
    )
    assert artifacts.run_id == safe_id
    assert artifacts.workspace.is_dir()


def test_generated_run_ids_satisfy_validation():
    run_id = RunArtifacts.generate_run_id()
    assert validate_run_id(run_id) == run_id


def test_events_are_sequenced_and_flushed(tmp_path):
    artifacts = RunArtifacts.create(
        tmp_path, metadata(), run_id=metadata()["run_id"], now=lambda: FIXED_TIME
    )

    first = artifacts.append_event("model_response", {"content": "hi"})
    second = artifacts.append_event("tool_call", {"call_id": "call-1"})
    artifacts.close()

    assert first["seq"] == 1
    assert second["seq"] == 2
    assert [event["type"] for event in read_events(artifacts.events_path)] == [
        "model_response",
        "tool_call",
    ]


def test_reader_ignores_only_one_trailing_incomplete_line(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text('{"seq":1}\n{"seq":2')

    assert read_events(path) == [{"seq": 1}]

    path.write_text('{"seq":1}\nnot-json\n{"seq":3}\n')
    with pytest.raises(ValueError, match="interior"):
        read_events(path)


def test_valid_final_event_without_newline_is_committed(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text('{"seq":1}')

    assert read_events(path) == [{"seq": 1}]


def test_event_failure_poisons_stream_without_later_append(tmp_path):
    artifacts = RunArtifacts.create(
        tmp_path, metadata(), run_id=metadata()["run_id"], now=lambda: FIXED_TIME
    )

    with pytest.raises(TypeError):
        artifacts.append_event("error", {"bad": object()})
    with pytest.raises(EventStreamPoisoned):
        artifacts.append_event("error", {"safe": True})

    assert artifacts.events_path.read_text() == ""


def test_result_commit_uses_replace_and_refuses_second_commit(tmp_path, monkeypatch):
    artifacts = RunArtifacts.create(
        tmp_path, metadata(), run_id=metadata()["run_id"], now=lambda: FIXED_TIME
    )
    replacements = []
    original_replace = __import__("os").replace

    def recording_replace(source, destination):
        replacements.append((Path(source), Path(destination)))
        original_replace(source, destination)

    monkeypatch.setattr("flagagent.artifacts.os.replace", recording_replace)
    result = {
        "schema_version": 1,
        "run_id": artifacts.run_id,
        "status": "unsolved",
        "reason": "model_stop",
    }

    artifacts.commit_result(result)

    assert json.loads(artifacts.result_path.read_text()) == result
    assert replacements[-1][0].parent == replacements[-1][1].parent
    with pytest.raises(FileExistsError):
        artifacts.commit_result(result)


def test_create_rejects_metadata_run_id_mismatch(tmp_path):
    mismatched = dict(metadata())
    mismatched["run_id"] = "FA-00000000T000000Z-00000000"

    with pytest.raises(ValueError, match="run_id"):
        RunArtifacts.create(
            tmp_path, mismatched, run_id=metadata()["run_id"], now=lambda: FIXED_TIME
        )
    assert not (tmp_path / metadata()["run_id"]).exists()


def test_run_json_replacement_failure_leaves_no_metadata_or_events(
    tmp_path, monkeypatch
):
    original_replace = __import__("os").replace

    def fail_run_json_replace(source, destination):
        if str(destination).endswith("run.json"):
            raise OSError("run.json replace failed")
        return original_replace(source, destination)

    monkeypatch.setattr("flagagent.artifacts.os.replace", fail_run_json_replace)
    run_dir = tmp_path / metadata()["run_id"]

    with pytest.raises(OSError, match="run.json replace failed"):
        RunArtifacts.create(
            tmp_path, metadata(), run_id=metadata()["run_id"], now=lambda: FIXED_TIME
        )

    assert not (run_dir / "run.json").exists()
    assert not (run_dir / "events.jsonl").exists()
    assert not run_dir.exists()


def test_run_json_failure_rolls_back_directory_and_allows_retry(tmp_path, monkeypatch):
    original_replace = __import__("os").replace

    def fail_run_json_replace(source, destination):
        if str(destination).endswith("run.json"):
            raise OSError("run.json replace failed")
        return original_replace(source, destination)

    monkeypatch.setattr("flagagent.artifacts.os.replace", fail_run_json_replace)
    run_dir = tmp_path / metadata()["run_id"]

    with pytest.raises(OSError, match="run.json replace failed"):
        RunArtifacts.create(
            tmp_path, metadata(), run_id=metadata()["run_id"], now=lambda: FIXED_TIME
        )

    assert not run_dir.exists()

    monkeypatch.setattr("flagagent.artifacts.os.replace", original_replace)

    artifacts = RunArtifacts.create(
        tmp_path, metadata(), run_id=metadata()["run_id"], now=lambda: FIXED_TIME
    )
    assert artifacts.directory == run_dir
    assert artifacts.workspace.is_dir()
    assert json.loads(artifacts.run_path.read_text()) == metadata()
    assert artifacts.events_path.exists()


def test_failed_create_does_not_remove_preexisting_directory(tmp_path):
    existing = RunArtifacts.create(
        tmp_path, metadata(), run_id=metadata()["run_id"], now=lambda: FIXED_TIME
    )
    sentinel = existing.directory / "sentinel.txt"
    sentinel.write_text("preserve")
    run_dir = tmp_path / metadata()["run_id"]

    with pytest.raises(FileExistsError):
        RunArtifacts.create(
            tmp_path, metadata(), run_id=metadata()["run_id"], now=lambda: FIXED_TIME
        )

    assert run_dir.exists()
    assert (run_dir / "run.json").exists()
    assert json.loads((run_dir / "run.json").read_text()) == metadata()
    assert sentinel.read_text() == "preserve"
    assert (run_dir / "workspace").is_dir()


def test_failed_result_replace_leaves_no_committed_result(tmp_path, monkeypatch):
    artifacts = RunArtifacts.create(
        tmp_path, metadata(), run_id=metadata()["run_id"], now=lambda: FIXED_TIME
    )

    def fail_replace(source, destination):
        raise OSError("replace failed")

    monkeypatch.setattr("flagagent.artifacts.os.replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        artifacts.commit_result({"status": "unsolved"})

    assert not artifacts.result_path.exists()
