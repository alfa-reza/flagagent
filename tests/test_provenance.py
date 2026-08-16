"""M1 provenance/integration slice: sandbox configuration provenance and
lifecycle event recording.

Tests the smallest provenance interface on DockerExecutor that lets AgentLoop
include normalized sandbox configuration in run.json before RunArtifacts.create,
and append one compact sandbox lifecycle event after prepare resolves IDs.

AgentLoop remains Docker-agnostic: it uses getattr to discover provenance
methods on the executor.  FakeExecutor (M0) works unchanged.

Docker-marked end-to-end test proves a verifier-confirmed solved Run through
DockerExecutor with artifact provenance, exact model-visible shell result
persistence, and owned resource cleanup.
"""

import json
import secrets
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from flagagent.artifacts import read_events
from flagagent.docker_executor import AGENT_USER, SANDBOX_IMAGE, DockerExecutor
from flagagent.loop import AgentLoop, ChallengeInput, Limits
from flagagent.model import ModelResponse, ScriptedModel, ToolCall
from flagagent.tools import ExactStringVerifier, FakeExecutor, SandboxError, ShellResult

NOW = datetime(2026, 8, 14, 16, 15, 30, tzinfo=UTC)
RUN_ID = "FA-20260814T161530Z-a13f4c2d"


class Clock:
    def __init__(self, value=0.0):
        self.value = value

    def __call__(self):
        return self.value


class _FakeRun:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def build_loop(tmp_path, responses, executor=None, verifier=None, run_id=RUN_ID):
    return AgentLoop(
        model=ScriptedModel(responses),
        executor=executor or FakeExecutor([]),
        verifier=verifier or ExactStringVerifier("Flag{ok}"),
        challenge=ChallengeInput("fixture", "solve it"),
        limits=Limits(
            max_model_turns=10, wall_timeout_seconds=100, command_timeout_seconds=10
        ),
        runs_root=tmp_path,
        monotonic=Clock(),
        utc_now=lambda: NOW,
        run_id=run_id,
    )


class ProvenanceExecutor(FakeExecutor):
    """FakeExecutor with provenance methods for AgentLoop integration tests."""

    def sandbox_provenance(self):
        return {"backend": "fake", "image": "test:dev", "network_mode": "none"}

    def sandbox_lifecycle(self):
        return {"agent_container_id": "fake-cid", "image_id": "sha256:fake"}


# ---------------------------------------------------------------------------
# Unit tests — DockerExecutor.sandbox_provenance (no Docker required)
# ---------------------------------------------------------------------------


def test_sandbox_provenance_returns_backend_docker():
    provenance = DockerExecutor().sandbox_provenance()
    assert provenance["backend"] == "docker"


def test_sandbox_provenance_includes_image_and_network_mode():
    provenance = DockerExecutor().sandbox_provenance()
    assert provenance["image"] == SANDBOX_IMAGE
    assert provenance["network_mode"] == "none"


def test_sandbox_provenance_includes_resource_limits():
    provenance = DockerExecutor().sandbox_provenance()
    assert provenance["memory"] == "2g"
    assert provenance["cpus"] == "2"
    assert provenance["pids_limit"] == 256


def test_sandbox_provenance_includes_container_user():
    provenance = DockerExecutor().sandbox_provenance()
    assert provenance["container_user"] == AGENT_USER


def test_sandbox_provenance_has_empty_security_relaxations():
    provenance = DockerExecutor().sandbox_provenance()
    assert provenance["security_relaxations"] == []


def test_sandbox_provenance_includes_docker_engine_and_rootless_keys():
    provenance = DockerExecutor().sandbox_provenance()
    assert "docker_engine" in provenance
    assert "rootless" in provenance


def test_sandbox_provenance_local_mode_network():
    provenance = DockerExecutor(network_mode="local").sandbox_provenance()
    assert provenance["network_mode"] == "local"


def test_sandbox_provenance_engine_info_when_docker_unavailable(monkeypatch):
    """When Docker CLI is missing, engine info is None without raising."""

    def raise_not_found(*args, **kwargs):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(subprocess, "run", raise_not_found)
    provenance = DockerExecutor().sandbox_provenance()
    assert provenance["docker_engine"] is None
    assert provenance["rootless"] is None


def test_sandbox_provenance_engine_info_rootful(monkeypatch):
    """Rootful Docker: version captured, rootless is False."""

    def fake_run(args, **kwargs):
        if args[:2] == ["docker", "info"] and "--format" not in args:
            return _FakeRun(stdout="Server Version: 29.7.2\n")
        return _FakeRun()

    monkeypatch.setattr(subprocess, "run", fake_run)
    provenance = DockerExecutor().sandbox_provenance()
    assert provenance["docker_engine"] == "29.7.2"
    assert provenance["rootless"] is False


def test_sandbox_provenance_engine_info_rootless(monkeypatch):
    """Rootless Docker: version captured, rootless is True."""

    def fake_run(args, **kwargs):
        if args[:2] == ["docker", "info"] and "--format" not in args:
            return _FakeRun(stdout="Server Version: 29.7.2\nrootless\n")
        return _FakeRun()

    monkeypatch.setattr(subprocess, "run", fake_run)
    provenance = DockerExecutor().sandbox_provenance()
    assert provenance["docker_engine"] == "29.7.2"
    assert provenance["rootless"] is True


# ---------------------------------------------------------------------------
# Unit tests — DockerExecutor.sandbox_lifecycle (no Docker required)
# ---------------------------------------------------------------------------


def test_sandbox_lifecycle_empty_before_prepare():
    assert DockerExecutor().sandbox_lifecycle() == {}


def test_sandbox_lifecycle_none_mode_has_agent_and_image():
    executor = DockerExecutor(network_mode="none")
    executor._container_id = "agent-cid"
    executor._resolved_image_id = "sha256:abc"
    assert executor.sandbox_lifecycle() == {
        "agent_container_id": "agent-cid",
        "image_id": "sha256:abc",
    }


def test_sandbox_lifecycle_local_includes_target_and_network():
    executor = DockerExecutor(network_mode="local")
    executor._container_id = "agent-cid"
    executor._target_id = "target-cid"
    executor._network_id = "net-id"
    executor._network_name = "net-name"
    executor._resolved_image_id = "sha256:abc"
    assert executor.sandbox_lifecycle() == {
        "agent_container_id": "agent-cid",
        "target_container_id": "target-cid",
        "network_id": "net-id",
        "network_name": "net-name",
        "image_id": "sha256:abc",
    }


# ---------------------------------------------------------------------------
# Unit tests — _create_agent resolves image ID (no Docker required)
# ---------------------------------------------------------------------------


def test_create_agent_resolves_image_id(monkeypatch):
    """After _create_agent, _resolved_image_id is set from docker inspect."""

    def fake_run(args, **kwargs):
        if args[1] == "run":
            return _FakeRun(stdout="agentcid\n")
        if args[1] == "inspect" and "{{.State.Running}}" in args:
            return _FakeRun(stdout="true\n")
        if args[1] == "inspect" and "{{.Image}}" in args:
            return _FakeRun(stdout="sha256:img123\n")
        return _FakeRun()

    monkeypatch.setattr(subprocess, "run", fake_run)
    executor = DockerExecutor()
    executor._create_agent(Path("/tmp/ws"), RUN_ID)
    assert executor._container_id == "agentcid"
    assert executor._resolved_image_id == "sha256:img123"


def test_create_agent_image_id_missing_removes_container_and_fails(monkeypatch):
    """AC-M1-18: when inspect cannot resolve the image ID, the owned container
    is removed and preparation fails with SandboxError before model execution."""

    def fake_run(args, **kwargs):
        if args[1] == "run":
            return _FakeRun(stdout="agentcid\n")
        if args[1] == "inspect" and "{{.State.Running}}" in args:
            return _FakeRun(stdout="true\n")
        if args[1] == "inspect" and "{{.Image}}" in args:
            return _FakeRun(returncode=1, stderr="inspect failed")
        return _FakeRun()

    calls = []

    def recording_run(args, **kwargs):
        calls.append(list(args))
        return fake_run(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", recording_run)
    executor = DockerExecutor()
    with pytest.raises(SandboxError, match="image identit"):
        executor._create_agent(Path("/tmp/ws"), RUN_ID)
    assert executor._container_id is None
    assert executor._resolved_image_id is None
    # the owned container was removed before failing
    assert ["docker", "rm", "-f", "agentcid"] in calls


def test_create_agent_empty_image_id_removes_container_and_fails(monkeypatch):
    """An inspect that returns no image ID is also a preparation failure."""

    def fake_run(args, **kwargs):
        if args[1] == "run":
            return _FakeRun(stdout="agentcid\n")
        if args[1] == "inspect" and "{{.State.Running}}" in args:
            return _FakeRun(stdout="true\n")
        if args[1] == "inspect" and "{{.Image}}" in args:
            return _FakeRun(stdout="\n")
        return _FakeRun()

    monkeypatch.setattr(subprocess, "run", fake_run)
    executor = DockerExecutor()
    with pytest.raises(SandboxError, match="image identit"):
        executor._create_agent(Path("/tmp/ws"), RUN_ID)
    assert executor._container_id is None


# ---------------------------------------------------------------------------
# Unit tests — AgentLoop provenance integration (no Docker required)
# ---------------------------------------------------------------------------


def test_agentloop_includes_provenance_in_run_json(tmp_path):
    executor = ProvenanceExecutor([])
    loop = build_loop(tmp_path, [ModelResponse(content="stop")], executor=executor)
    loop.run()
    run_json = json.loads(loop.artifacts.run_path.read_text())
    assert run_json["sandbox"] == {
        "backend": "fake",
        "image": "test:dev",
        "network_mode": "none",
    }


def test_agentloop_appends_lifecycle_event_after_prepare(tmp_path):
    executor = ProvenanceExecutor([])
    loop = build_loop(tmp_path, [ModelResponse(content="stop")], executor=executor)
    loop.run()
    events = read_events(loop.artifacts.events_path)
    lifecycle = [e for e in events if e["type"] == "sandbox_lifecycle"]
    assert len(lifecycle) == 1
    assert lifecycle[0]["payload"]["agent_container_id"] == "fake-cid"
    assert lifecycle[0]["payload"]["image_id"] == "sha256:fake"


def test_lifecycle_event_precedes_model_response(tmp_path):
    executor = ProvenanceExecutor([])
    loop = build_loop(tmp_path, [ModelResponse(content="stop")], executor=executor)
    loop.run()
    events = read_events(loop.artifacts.events_path)
    types = [e["type"] for e in events]
    assert types.index("sandbox_lifecycle") < types.index("model_response")


def test_agentloop_without_provenance_methods_still_works(tmp_path):
    loop = build_loop(tmp_path, [ModelResponse(content="stop")])
    result = loop.run()
    run_json = json.loads(loop.artifacts.run_path.read_text())
    assert "sandbox" not in run_json
    events = read_events(loop.artifacts.events_path)
    assert not any(e["type"] == "sandbox_lifecycle" for e in events)
    assert result["status:reason"] == "unsolved:model_stop"


def test_provenance_failure_does_not_break_run(tmp_path):
    class BrokenProvenance(FakeExecutor):
        def sandbox_provenance(self):
            raise RuntimeError("provenance failed")

        def sandbox_lifecycle(self):
            raise RuntimeError("lifecycle failed")

    loop = build_loop(
        tmp_path, [ModelResponse(content="stop")], executor=BrokenProvenance([])
    )
    result = loop.run()
    assert result["status:reason"] == "unsolved:model_stop"
    run_json = json.loads(loop.artifacts.run_path.read_text())
    assert "sandbox" not in run_json


def test_cleanup_failure_does_not_rewrite_result_with_provenance(tmp_path):
    class CleanupFailsWithProvenance(ProvenanceExecutor):
        def cleanup(self, run_id):
            super().cleanup(run_id)
            raise RuntimeError("docker remove failed")

    executor = CleanupFailsWithProvenance([ShellResult("ok", "", 0, False)])
    loop = build_loop(
        tmp_path,
        [
            ModelResponse(tool_calls=(ToolCall("s", "shell", {"command": "x"}),)),
            ModelResponse(content="done"),
        ],
        executor=executor,
    )
    result = loop.run()

    committed = json.loads(loop.artifacts.result_path.read_text())
    assert committed["status:reason"] == "unsolved:model_stop"
    assert result["status:reason"] == "unsolved:model_stop"

    events = read_events(loop.artifacts.events_path)
    types = [e["type"] for e in events]
    assert "terminal_decision" in types
    assert "sandbox_cleanup_failed" in types
    assert types.index("terminal_decision") < types.index("sandbox_cleanup_failed")

    run_json = json.loads(loop.artifacts.run_path.read_text())
    assert "sandbox" in run_json


# ---------------------------------------------------------------------------
# Docker end-to-end test
# ---------------------------------------------------------------------------

docker = pytest.mark.docker


@docker
def test_docker_e2e_solved_run_with_provenance_and_cleanup(tmp_path, sandbox_image):
    """End-to-end: ScriptedModel + DockerExecutor + ExactStringVerifier.

    Proves AC-M1-01 (Docker-backed solved Run) and AC-M1-18 (Provenance):
    - verifier-confirmed solved Run through the Docker shell executor;
    - run.json contains normalized sandbox configuration (backend, image,
      network mode, resource limits, container user, security relaxations,
      Docker Engine/rootless observation);
    - events.jsonl contains one compact sandbox_lifecycle event with the
      resolved Agent container ID and image ID, before model_response;
    - the exact model-visible shell result is persisted in the tool_result
      event;
    - owned Run resources (Agent container) are cleaned up after the run.
    """
    run_id = f"FA-E2E-{secrets.token_hex(4)}"
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    flag = "Flag{e2e_provenance}"
    executor = DockerExecutor(image=sandbox_image)
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=(ToolCall("s", "shell", {"command": f"echo {flag}"}),)
            ),
            ModelResponse(
                tool_calls=(ToolCall("f", "submit_flag", {"candidate": flag}),)
            ),
        ]
    )

    loop = AgentLoop(
        model=model,
        executor=executor,
        verifier=ExactStringVerifier(flag),
        challenge=ChallengeInput("e2e", "Find and submit the flag"),
        limits=Limits(
            max_model_turns=5, wall_timeout_seconds=120, command_timeout_seconds=30
        ),
        runs_root=tmp_path,
        monotonic=Clock(),
        utc_now=lambda: NOW,
        run_id=run_id,
    )

    result = loop.run()

    # 1. Verifier-confirmed solved Run
    assert result["status:reason"] == "solved:verified_flag"
    assert result["flag_submissions"] == 1
    assert result["tool_calls"] == 2

    # 2. Artifact provenance in run.json
    run_json = json.loads(loop.artifacts.run_path.read_text())
    sandbox = run_json["sandbox"]
    assert sandbox["backend"] == "docker"
    assert sandbox["image"] == sandbox_image
    assert sandbox["network_mode"] == "none"
    assert sandbox["memory"] == "2g"
    assert sandbox["cpus"] == "2"
    assert sandbox["pids_limit"] == 256
    assert sandbox["container_user"] == AGENT_USER
    assert sandbox["security_relaxations"] == []
    assert sandbox["docker_engine"] is not None
    assert sandbox["rootless"] is not None

    # 3. Compact sandbox lifecycle event with resolved IDs
    events = read_events(loop.artifacts.events_path)
    lifecycle = [e for e in events if e["type"] == "sandbox_lifecycle"]
    assert len(lifecycle) == 1
    payload = lifecycle[0]["payload"]
    assert payload["agent_container_id"] is not None
    assert payload["image_id"] is not None
    assert len(payload) <= 5  # compact: no full inspect dump

    types = [e["type"] for e in events]
    assert types.index("sandbox_lifecycle") < types.index("model_response")

    # 4. Exact model-visible shell result persistence
    tool_results = [e for e in events if e["type"] == "tool_result"]
    shell_result = next(r for r in tool_results if r["payload"]["name"] == "shell")
    model_seen = model.calls[1][0][-1]["result"]
    assert shell_result["payload"]["result"] == model_seen
    assert flag in shell_result["payload"]["result"]["stdout"]
    assert shell_result["payload"]["executed"] is True

    # 5. Owned resource cleanup
    cid = payload["agent_container_id"]
    check = subprocess.run(
        ["docker", "inspect", "--format", "{{.Id}}", cid],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert check.returncode != 0  # container is gone
