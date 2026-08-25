import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from flagagent.cli import load_challenge
from flagagent.docker_executor import DockerExecutor
from flagagent.loop import AgentLoop, Limits, _snapshot_source_files
from flagagent.model import ModelResponse, ScriptedModel, ToolCall
from flagagent.tools import ExactStringVerifier

ROOT = Path(__file__).parents[1]


def test_layered_file_fixture_is_deterministic_and_tool_minimal():
    challenge_dir = ROOT / "challenges" / "layered-file"
    challenge, expected = load_challenge(challenge_dir)
    files, digest, temporary = _snapshot_source_files(challenge.source_dir)
    try:
        assert challenge.network_mode == "none"
        assert expected == "Flag{layered_file_ok}"
        assert [relative.as_posix() for _, relative in files] == ["evidence.txt"]
        assert len(digest) == 64
        evidence = (challenge_dir / "files" / "evidence.txt").read_text()
        assert "expected_flag" not in evidence
        assert expected not in evidence
    finally:
        if temporary is not None:
            temporary.cleanup()


def test_local_marker_fixture_uses_existing_audited_target():
    challenge, expected = load_challenge(ROOT / "challenges" / "local-marker")

    assert challenge.network_mode == "local"
    assert challenge.target_context == (
        "The audited local target is reachable at target:9999. "
        "It returns one deterministic marker per connection."
    )
    assert expected == "Flag{flagagent_target}"
    assert challenge.source_dir is None


def test_fixtures_require_only_documented_tools():
    manifest = {
        "layered-file": ["python3", "coreutils"],
        "local-marker": ["netcat-openbsd"],
    }

    assert manifest == {
        "layered-file": ["python3", "coreutils"],
        "local-marker": ["netcat-openbsd"],
    }


@pytest.mark.docker
def test_layered_file_fixture_runs_in_none_sandbox(tmp_path, sandbox_image):
    challenge, expected = load_challenge(ROOT / "challenges" / "layered-file")
    executor = DockerExecutor(image=sandbox_image, network_mode=challenge.network_mode)
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        "s",
                        "shell",
                        {"command": "cat evidence.txt | base64 -d | base64 -d"},
                    ),
                )
            ),
            ModelResponse(
                tool_calls=(ToolCall("f", "submit_flag", {"candidate": expected}),)
            ),
        ]
    )
    loop = AgentLoop(
        model=model,
        executor=executor,
        verifier=ExactStringVerifier(expected),
        challenge=challenge,
        limits=Limits(
            max_model_turns=5, wall_timeout_seconds=120, command_timeout_seconds=30
        ),
        runs_root=tmp_path,
        monotonic=time.monotonic,
        utc_now=lambda: datetime.now(UTC),
    )

    result = loop.run()

    assert result["status:reason"] == "solved:verified_flag"
    assert model.calls[1][0][-1]["result"]["stdout"].strip() == expected


@pytest.mark.docker
def test_local_marker_fixture_runs_against_audited_target(tmp_path, sandbox_image):
    challenge, expected = load_challenge(ROOT / "challenges" / "local-marker")
    executor = DockerExecutor(image=sandbox_image, network_mode=challenge.network_mode)
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=(ToolCall("s", "shell", {"command": "nc -w 2 target 9999"}),)
            ),
            ModelResponse(
                tool_calls=(ToolCall("f", "submit_flag", {"candidate": expected}),)
            ),
        ]
    )
    loop = AgentLoop(
        model=model,
        executor=executor,
        verifier=ExactStringVerifier(expected),
        challenge=challenge,
        limits=Limits(
            max_model_turns=5, wall_timeout_seconds=120, command_timeout_seconds=30
        ),
        runs_root=tmp_path,
        monotonic=time.monotonic,
        utc_now=lambda: datetime.now(UTC),
    )

    result = loop.run()

    assert result["status:reason"] == "solved:verified_flag"
    assert "flagagent-target-ok" in model.calls[1][0][-1]["result"]["stdout"]
