"""M1 Docker executor slice: Docker CLI executor for contained shell execution.

Unit tests (no Docker required) verify the argument vectors, run-id
validation, bounded prefix+suffix output collection, timeout recovery
(kill/restart the owned container), and error mapping.  Docker integration
tests (marked ``@pytest.mark.docker``) verify real container creation,
execution, security posture, resource limits, cleanup, and containment
evidence.
"""

import contextlib
import json
import secrets
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from flagagent.docker_executor import (
    AGENT_USER,
    KEEPALIVE_COMMAND,
    SANDBOX_IMAGE,
    WORKSPACE_TARGET,
    DockerExecutor,
)
from flagagent.loop import AgentLoop, ChallengeInput, Limits
from flagagent.model import ModelResponse, ScriptedModel, ToolCall
from flagagent.tools import (
    LOGGED_TOOL_OUTPUT_BYTES,
    MODEL_TOOL_OUTPUT_BYTES,
    TRUNCATION_MARKER,
    ExactStringVerifier,
    SandboxError,
    ShellResult,
    normalize_shell_result,
)

RUN_ID = "FA-20260814T161530Z-a13f4c2d"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _index(args, flag):
    return args.index(flag)


def _value(args, flag):
    return args[_index(args, flag) + 1]


def _values(args, flag):
    return [args[i + 1] for i, a in enumerate(args) if a == flag]


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
        self.stdout = None
        self.stderr = None

    def wait(self, timeout=None):
        return self.returncode


# ---------------------------------------------------------------------------
# Unit tests — argument vectors (no Docker required)
# ---------------------------------------------------------------------------


def test_container_name_is_run_scoped():
    name = DockerExecutor._container_name_for(RUN_ID)
    assert name == f"flagagent-agent-{RUN_ID}"


def test_run_args_are_a_subprocess_vector_not_a_shell_string():
    executor = DockerExecutor()
    args = executor._run_args(Path("/tmp/ws"), RUN_ID)
    assert isinstance(args, list)
    assert all(isinstance(a, str) for a in args)
    assert args[0] == "docker"


def test_run_args_contain_required_security_and_resource_flags():
    executor = DockerExecutor()
    args = executor._run_args(Path("/tmp/ws"), RUN_ID)

    assert "run" in args
    assert "-d" in args
    assert "--init" in args
    assert _value(args, "--user") == AGENT_USER
    assert _value(args, "-w") == WORKSPACE_TARGET
    assert _value(args, "--memory") == "2g"
    assert _value(args, "--cpus") == "2"
    assert _value(args, "--pids-limit") == "256"
    assert "no-new-privileges" in _values(args, "--security-opt")
    assert "ALL" in _values(args, "--cap-drop")
    assert _value(args, "--network") == "none"
    assert SANDBOX_IMAGE in args
    assert args[-2:] == ["sleep", "infinity"]
    assert KEEPALIVE_COMMAND == "sleep infinity"


def test_run_args_contain_all_required_labels():
    executor = DockerExecutor()
    args = executor._run_args(Path("/tmp/ws"), RUN_ID)
    labels = _values(args, "--label")
    assert "flagagent.managed=true" in labels
    assert f"flagagent.run_id={RUN_ID}" in labels
    assert "flagagent.role=agent" in labels
    assert "flagagent.version=0.1.0" in labels


def test_run_args_mount_only_workspace():
    executor = DockerExecutor()
    workspace = Path("/tmp/fa-ws")
    args = executor._run_args(workspace, RUN_ID)
    mounts = _values(args, "-v")
    assert mounts == [f"{workspace}:{WORKSPACE_TARGET}"]


def test_run_args_exclude_privileged_hostnet_socket_and_extra_mounts():
    executor = DockerExecutor()
    args = executor._run_args(Path("/tmp/ws"), RUN_ID)

    assert "--privileged" not in args
    assert "host" not in _values(args, "--network")
    assert "/var/run/docker.sock" not in args
    assert "/var/run" not in args
    # exactly one bind mount
    assert len(_values(args, "-v")) == 1
    # no --cap-add
    assert "--cap-add" not in args


def test_exec_args_run_bash_lc_command_in_workspace():
    executor = DockerExecutor()
    executor._container_id = "cid123"
    args = executor._exec_args("echo hello")

    assert args[:2] == ["docker", "exec"]
    assert _value(args, "-w") == WORKSPACE_TARGET
    assert args[4] == "cid123"
    assert args[5:] == ["/bin/bash", "-lc", "echo hello"]


def test_exec_args_pass_command_verbatim_to_bash():
    executor = DockerExecutor()
    executor._container_id = "cid"
    tricky = 'echo "hello $world"; rm -rf /; exit 42 # $(whoami)'
    args = executor._exec_args(tricky)

    # The command is the final element — never shell-interpolated by the host.
    assert args[-1] == tricky
    assert args[-2] == "-lc"


def test_cleanup_is_noop_when_not_prepared():
    executor = DockerExecutor()
    executor.cleanup(RUN_ID)  # must not raise


# ---------------------------------------------------------------------------
# Unit tests — run id trust boundary (no Docker required)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_id",
    ["", "FA/../escape", "FA/x", "FA:x", "FA x", "FA@sha", ".lead", "-lead", "FA..x"],
)
def test_prepare_rejects_unsafe_run_id_before_docker_calls(monkeypatch, bad_id):
    def fail(*args, **kwargs):
        raise AssertionError(f"unexpected docker call: {args}")

    monkeypatch.setattr(subprocess, "run", fail)
    monkeypatch.setattr(subprocess, "Popen", fail)
    executor = DockerExecutor()
    with pytest.raises(SandboxError, match="invalid run id"):
        executor.prepare(Path("/tmp/ws"), bad_id)
    assert executor._container_id is None
    assert executor._container_name is None


def test_prepare_accepts_safe_run_ids_without_docker_side_effects(monkeypatch):
    executor = DockerExecutor()
    executor.docker_bin = "/nonexistent-flagagent-docker"
    with pytest.raises(SandboxError, match="docker CLI not found"):
        executor.prepare(Path("/tmp/ws"), "FA-20260814T161530Z-a13f4c2d")
    assert executor._container_name == "flagagent-agent-FA-20260814T161530Z-a13f4c2d"


# ---------------------------------------------------------------------------
# Unit tests — error mapping (no Docker required)
# ---------------------------------------------------------------------------


def test_execute_before_prepare_raises_sandbox_error():
    executor = DockerExecutor()
    with pytest.raises(SandboxError):
        executor.execute("echo hello", 10)


def _inspect_running(stdout="true\n"):
    def fake_run(args, **kwargs):
        assert args[:2] == ["docker", "inspect"], f"unexpected call: {args}"
        assert "{{.State.Running}}" in args
        return _FakeCompleted(stdout=stdout)

    return fake_run


def test_execute_raises_sandbox_error_when_container_stopped(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _inspect_running("false\n"))

    def fail_popen(*args, **kwargs):
        raise AssertionError("docker exec must not start when container is stopped")

    monkeypatch.setattr(subprocess, "Popen", fail_popen)
    executor = DockerExecutor()
    executor._container_id = "cid"
    with pytest.raises(SandboxError, match="not running"):
        executor.execute("echo hi", 10)


def test_execute_raises_sandbox_error_when_container_missing(monkeypatch):
    def fake_run(args, **kwargs):
        return _FakeCompleted(returncode=1, stderr="Error: No such container: cid")

    monkeypatch.setattr(subprocess, "run", fake_run)

    def fail_popen(*args, **kwargs):
        raise AssertionError("docker exec must not start when container is gone")

    monkeypatch.setattr(subprocess, "Popen", fail_popen)
    executor = DockerExecutor()
    executor._container_id = "cid"
    with pytest.raises(SandboxError, match="not running"):
        executor.execute("echo hi", 10)


def test_prepare_raises_sandbox_error_when_docker_missing(monkeypatch):
    def _not_found(*a, **kw):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(subprocess, "run", _not_found)
    executor = DockerExecutor()
    with pytest.raises(SandboxError):
        executor.prepare(Path("/tmp/ws"), RUN_ID)


def test_prepare_raises_sandbox_error_on_docker_run_failure(monkeypatch):
    class FakeResult:
        returncode = 1
        stdout = ""
        stderr = "Error: no such image"

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: FakeResult())
    executor = DockerExecutor()
    with pytest.raises(SandboxError, match="docker run failed"):
        executor.prepare(Path("/tmp/ws"), RUN_ID)


def test_prepare_rejects_double_prepare():
    executor = DockerExecutor()
    executor._container_id = "existing"
    with pytest.raises(SandboxError, match="already prepared"):
        executor.prepare(Path("/tmp/ws"), RUN_ID)


# ---------------------------------------------------------------------------
# Unit tests — timeout recovery boundary (AC-M1-05, no Docker required)
# ---------------------------------------------------------------------------


def _recovery_run(calls, failing=None):
    """Fake docker control: inspect says running; kill/start/probe may fail."""

    def fake_run(args, **kwargs):
        calls.append(list(args))
        sub = args[1]
        is_probe = sub == "exec" and "/bin/true" in args
        if failing == "probe" and is_probe:
            return _FakeCompleted(returncode=1, stderr="Error: unusable")
        if failing == sub:
            return _FakeCompleted(returncode=1, stderr=f"Error: {sub} failed")
        if sub == "inspect":
            return _FakeCompleted(stdout="true\n")
        return _FakeCompleted()

    return fake_run


def test_execute_timeout_kills_restarts_and_verifies_container(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", _recovery_run(calls))
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    executor = DockerExecutor()
    executor._container_id = "cid"
    monkeypatch.setattr(
        executor, "_collect", lambda process, deadline: (b"partial", b"", True, False)
    )

    result = executor.execute("sleep 1000", 2)

    assert result.timed_out is True
    assert result.exit_code is None
    assert result.stdout == "partial"
    # container identity is preserved through recovery
    assert executor._container_id == "cid"
    # recovery sequence: kill the whole owned container, restart it, probe it
    sequence = [(a[1], a[2]) for a in calls if a[1] in ("kill", "start", "exec")]
    assert sequence == [("kill", "cid"), ("start", "cid"), ("exec", "cid")]


@pytest.mark.parametrize("failing", ["kill", "start", "probe"])
def test_execute_timeout_recovery_failure_raises_sandbox_error(monkeypatch, failing):
    calls = []
    monkeypatch.setattr(subprocess, "run", _recovery_run(calls, failing=failing))
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    executor = DockerExecutor()
    executor._container_id = "cid"
    monkeypatch.setattr(
        executor, "_collect", lambda process, deadline: (b"", b"", True, False)
    )

    with pytest.raises(SandboxError, match="recovery failed"):
        executor.execute("sleep 1000", 2)


def test_execute_timeout_kills_host_exec_client(monkeypatch):
    """The host-side docker exec client is force-killed during recovery."""
    killed = []
    monkeypatch.setattr(subprocess, "run", _recovery_run([]))
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    monkeypatch.setattr("os.killpg", lambda pgid, sig: killed.append((pgid, sig)))
    monkeypatch.setattr("os.getpgid", lambda pid: 999)
    executor = DockerExecutor()
    executor._container_id = "cid"
    monkeypatch.setattr(
        executor, "_collect", lambda process, deadline: (b"", b"", True, False)
    )

    executor.execute("sleep 1000", 2)
    assert killed == [(999, 9)]  # SIGKILL to the exec client's process group


# ---------------------------------------------------------------------------
# Unit tests — docker exec control failure mapping (no Docker required)
# ---------------------------------------------------------------------------


def test_execute_maps_docker_exec_control_failure_to_sandbox_error(monkeypatch):
    popen_instances = []

    def fake_popen(args, **kwargs):
        process = _FakePopen(args, **kwargs)
        process.returncode = 125
        popen_instances.append(process)
        return process

    monkeypatch.setattr(subprocess, "run", _inspect_running())
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    executor = DockerExecutor()
    executor._container_id = "cid"
    monkeypatch.setattr(
        executor,
        "_collect",
        lambda process, deadline: (
            b"",
            b"Error response from daemon: No such container: cid\n",
            False,
            False,
        ),
    )

    with pytest.raises(SandboxError, match="control failure"):
        executor.execute("true", 10)


def test_execute_exit_125_with_ordinary_stderr_stays_normal_evidence(monkeypatch):
    """A command that itself exits 125 with ordinary stderr is not a
    sandbox control failure."""

    def fake_popen(args, **kwargs):
        process = _FakePopen(args, **kwargs)
        process.returncode = 125
        return process

    monkeypatch.setattr(subprocess, "run", _inspect_running())
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    executor = DockerExecutor()
    executor._container_id = "cid"
    monkeypatch.setattr(
        executor,
        "_collect",
        lambda process, deadline: (b"", b"command failed its own way\n", False, False),
    )

    result = executor.execute("exit 125", 10)
    assert result.exit_code == 125
    assert result.timed_out is False


# ---------------------------------------------------------------------------
# Unit tests — bounded output collection (no Docker required)
# ---------------------------------------------------------------------------


def _popen(script):
    return subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def test_collect_bounds_stdout_while_draining():
    executor = DockerExecutor()
    process = _popen("import sys; sys.stdout.write('A'*200_000); sys.stdout.flush()")
    deadline = time.monotonic() + 30
    stdout, stderr, timed_out, truncated = executor._collect(process, deadline)
    process.wait(timeout=5)

    # bounded prefix + rolling suffix, never the full stream
    assert len(stdout) <= 2 * LOGGED_TOOL_OUTPUT_BYTES
    assert stdout == b"A" * len(stdout)
    assert truncated is True
    assert timed_out is False
    assert stderr == b""


def test_collect_bounds_stderr_while_draining():
    executor = DockerExecutor()
    process = _popen("import sys; sys.stderr.write('B'*200_000); sys.stderr.flush()")
    deadline = time.monotonic() + 30
    _, stderr, timed_out, truncated = executor._collect(process, deadline)
    process.wait(timeout=5)

    assert len(stderr) <= 2 * LOGGED_TOOL_OUTPUT_BYTES
    assert truncated is True
    assert timed_out is False


def test_collect_retains_bounded_prefix_and_rolling_suffix():
    """Beyond 64 KiB the retained view keeps the head *and* the tail."""
    executor = DockerExecutor()
    script = (
        "import sys\n"
        "head = b'HEAD_SENTINEL_7f3a'\n"
        "tail = b'TAIL_SENTINEL_91c4'\n"
        "sys.stdout.buffer.write(head + b'x'*200_000 + tail)\n"
        "sys.stdout.flush()\n"
    )
    process = _popen(script)
    deadline = time.monotonic() + 30
    stdout, _, timed_out, truncated = executor._collect(process, deadline)
    process.wait(timeout=5)

    assert timed_out is False
    assert truncated is True
    assert stdout.startswith(b"HEAD_SENTINEL_7f3a")
    assert stdout.endswith(b"TAIL_SENTINEL_91c4")
    assert LOGGED_TOOL_OUTPUT_BYTES < len(stdout) <= 2 * LOGGED_TOOL_OUTPUT_BYTES


def test_collect_retained_view_normalizes_to_m0_head_tail_result():
    """normalize_shell_result renders the canonical M0 head+tail truncation
    from the bounded prefix+suffix view, with both sentinels preserved."""
    executor = DockerExecutor()
    script = (
        "import sys\n"
        "head = b'HEAD_SENTINEL_7f3a'\n"
        "tail = b'TAIL_SENTINEL_91c4'\n"
        "sys.stdout.buffer.write(head + b'x'*200_000 + tail)\n"
        "sys.stderr.buffer.write(head + b'y'*200_000 + tail)\n"
        "sys.stdout.flush()\n"
        "sys.stderr.flush()\n"
    )
    process = _popen(script)
    deadline = time.monotonic() + 30
    stdout, stderr, _, truncated = executor._collect(process, deadline)
    process.wait(timeout=5)

    raw = ShellResult(
        stdout.decode("utf-8", errors="ignore"),
        stderr.decode("utf-8", errors="ignore"),
        0,
        False,
        truncated,
    )
    model, logged = normalize_shell_result(raw)

    for view in (model, logged):
        assert view.stdout.startswith("HEAD_SENTINEL_7f3a")
        assert view.stdout.endswith("TAIL_SENTINEL_91c4")
        assert TRUNCATION_MARKER in view.stdout
        assert view.stderr.startswith("HEAD_SENTINEL_7f3a")
        assert view.stderr.endswith("TAIL_SENTINEL_91c4")
    assert len(model.stdout.encode()) <= MODEL_TOOL_OUTPUT_BYTES
    assert len(logged.stdout.encode()) <= LOGGED_TOOL_OUTPUT_BYTES
    assert model.truncated is True
    assert logged.truncated is True


def test_collect_preserves_short_output_without_truncation():
    executor = DockerExecutor()
    process = _popen(
        "import sys; sys.stdout.write('short-out'); sys.stderr.write('short-err')"
    )
    deadline = time.monotonic() + 30
    stdout, stderr, timed_out, truncated = executor._collect(process, deadline)
    process.wait(timeout=5)

    assert stdout == b"short-out"
    assert stderr == b"short-err"
    assert truncated is False
    assert timed_out is False


def test_collect_keeps_stdout_and_stderr_separate():
    executor = DockerExecutor()
    process = _popen("import sys; sys.stdout.write('OUT'); sys.stderr.write('ERR')")
    deadline = time.monotonic() + 30
    stdout, stderr, _, _ = executor._collect(process, deadline)
    process.wait(timeout=5)

    assert b"OUT" in stdout
    assert b"ERR" not in stdout
    assert b"ERR" in stderr
    assert b"OUT" not in stderr


def test_collect_returns_timeout_when_deadline_exceeded():
    executor = DockerExecutor()
    process = _popen("import time; time.sleep(30)")
    deadline = time.monotonic() + 0.3
    _, _, timed_out, _ = executor._collect(process, deadline)

    assert timed_out is True
    # clean up the still-running subprocess
    process.kill()
    process.wait(timeout=5)


# ---------------------------------------------------------------------------
# Docker integration tests
# ---------------------------------------------------------------------------

docker = pytest.mark.docker


def _docker_inspect(container_id, fmt):
    result = subprocess.run(
        ["docker", "inspect", "--format", fmt, container_id],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, f"docker inspect failed: {result.stderr}"
    return result.stdout.strip()


def _docker_inspect_json(container_id, fmt):
    return json.loads(_docker_inspect(container_id, fmt))


@pytest.fixture
def docker_exec(tmp_path, sandbox_image):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_id = f"FA-TEST-{secrets.token_hex(4)}"
    executor = DockerExecutor(image=sandbox_image)
    executor.prepare(workspace, run_id)
    yield executor, workspace, run_id
    with contextlib.suppress(Exception):
        executor.cleanup(run_id)


@docker
def test_prepare_creates_run_scoped_container(docker_exec):
    executor, _, run_id = docker_exec
    cid = executor._container_id
    assert cid is not None

    name = _docker_inspect(cid, "{{.Name}}")
    assert name == f"/{DockerExecutor._container_name_for(run_id)}"


@docker
def test_container_has_required_labels(docker_exec):
    executor, _, run_id = docker_exec
    labels = _docker_inspect_json(executor._container_id, "{{json .Config.Labels}}")
    assert labels["flagagent.managed"] == "true"
    assert labels["flagagent.run_id"] == run_id
    assert labels["flagagent.role"] == "agent"
    assert labels["flagagent.version"] == "0.1.0"


@docker
def test_container_has_resource_limits(docker_exec):
    executor, _, _ = docker_exec
    cid = executor._container_id

    memory = int(_docker_inspect(cid, "{{.HostConfig.Memory}}"))
    nano_cpus = int(_docker_inspect(cid, "{{.HostConfig.NanoCpus}}"))
    pids = int(_docker_inspect(cid, "{{.HostConfig.PidsLimit}}"))

    assert memory == 2 * 1024**3  # 2 GiB
    assert nano_cpus == 2_000_000_000  # 2 CPUs
    assert pids == 256


@docker
def test_container_has_security_posture(docker_exec):
    executor, _, _ = docker_exec
    cid = executor._container_id

    privileged = _docker_inspect(cid, "{{.HostConfig.Privileged}}")
    user = _docker_inspect(cid, "{{.Config.User}}")
    security_opt = _docker_inspect_json(cid, "{{json .HostConfig.SecurityOpt}}")
    cap_drop = _docker_inspect_json(cid, "{{json .HostConfig.CapDrop}}")
    cap_add = _docker_inspect_json(cid, "{{json .HostConfig.CapAdd}}")

    assert privileged == "false"
    assert user == AGENT_USER
    assert "no-new-privileges" in security_opt
    assert "ALL" in cap_drop
    assert cap_add == [] or cap_add is None
    # seccomp is not disabled: no profile override, Docker default stays active
    assert not [opt for opt in security_opt if "seccomp" in opt.lower()]


@docker
def test_container_network_is_none(docker_exec):
    executor, _, _ = docker_exec
    mode = _docker_inspect(executor._container_id, "{{.HostConfig.NetworkMode}}")
    assert mode == "none"


@docker
def test_container_mount_boundary(docker_exec):
    executor, workspace, _ = docker_exec
    mounts = _docker_inspect_json(executor._container_id, "{{json .Mounts}}")
    assert len(mounts) == 1
    mount = mounts[0]
    assert mount["Destination"] == WORKSPACE_TARGET
    assert mount["Source"] == str(workspace.resolve())
    # no docker socket or unrelated host paths
    for m in mounts:
        assert "docker.sock" not in m["Source"]
        assert ".git" not in m["Source"]


@docker
def test_container_has_no_docker_socket_mount(docker_exec):
    executor, _, _ = docker_exec
    mounts = _docker_inspect_json(executor._container_id, "{{json .Mounts}}")
    sources = [m["Source"] for m in mounts]
    assert not any("docker.sock" in s or "/var/run" in s for s in sources)


@docker
def test_shell_executes_in_workspace(docker_exec):
    executor, _, _ = docker_exec
    result = executor.execute("pwd", 10)
    assert result.exit_code == 0
    assert result.stdout.strip() == WORKSPACE_TARGET
    assert result.timed_out is False


@docker
def test_shell_stdout_stderr_separate(docker_exec):
    executor, _, _ = docker_exec
    result = executor.execute("echo OUT; echo ERR >&2", 10)
    assert result.exit_code == 0
    assert "OUT" in result.stdout
    assert "ERR" not in result.stdout
    assert "ERR" in result.stderr
    assert "OUT" not in result.stderr


@docker
def test_nonzero_exit_is_normal_evidence(docker_exec):
    executor, _, _ = docker_exec
    result = executor.execute("exit 7", 10)
    assert result.exit_code == 7
    assert result.timed_out is False


@docker
def test_filesystem_persists_across_calls(docker_exec):
    executor, _, _ = docker_exec
    executor.execute("echo persisted > marker.txt", 10)
    result = executor.execute("cat marker.txt", 10)
    assert result.stdout.strip() == "persisted"


@docker
def test_shell_state_does_not_persist(docker_exec):
    executor, _, _ = docker_exec
    executor.execute("cd /tmp && export FOO=bar", 10)
    result = executor.execute("pwd; echo $FOO", 10)
    assert result.stdout.strip() == WORKSPACE_TARGET


@docker
def test_non_root_user(docker_exec):
    executor, _, _ = docker_exec
    result = executor.execute("id -u", 10)
    assert result.exit_code == 0
    assert result.stdout.strip() != "0"


@docker
def test_none_mode_blocks_non_loopback_connectivity(docker_exec):
    """AC-M1-11: ``none`` mode leaves no intended non-loopback connectivity."""
    executor, _, _ = docker_exec
    result = executor.execute(
        'timeout 5 bash -c "echo > /dev/tcp/1.1.1.1/80" 2>/dev/null '
        "&& echo REACHABLE || echo BLOCKED",
        15,
    )
    assert result.exit_code == 0
    assert "BLOCKED" in result.stdout
    assert "REACHABLE" not in result.stdout


@docker
def test_control_secret_and_host_marker_absent(docker_exec, tmp_path, monkeypatch):
    """AC-M1-09/10 evidence using throwaway fixtures only — never real secrets.

    A dummy control secret in the harness environment plus a dummy secret
    file and unrelated host marker next to (not inside) the workspace must
    be invisible to the Agent container.
    """
    executor, _, _ = docker_exec
    secret_value = f"FAKE-CONTROL-SECRET-{secrets.token_hex(8)}"
    monkeypatch.setenv("FLAGAGENT_CONTROL_SECRET", secret_value)
    secret_file = tmp_path / "control-secret.env"
    secret_file.write_text(secret_value)
    marker_dir = tmp_path / "unrelated-host-marker"
    marker_dir.mkdir()
    marker = marker_dir / "marker.txt"
    marker.write_text("host-only-marker")

    command = (
        "printenv; "
        f"test -e '{secret_file}' && echo SECRET_FILE_PRESENT "
        "|| echo SECRET_FILE_ABSENT; "
        f"test -e '{marker}' && echo MARKER_PRESENT || echo MARKER_ABSENT; "
        f"grep -rF '{secret_value}' /workspace >/dev/null 2>&1 "
        "&& echo SECRET_LEAKED || echo SECRET_NOT_LEAKED"
    )
    result = executor.execute(command, 20)

    assert result.exit_code == 0
    # no wholesale environment inheritance into the container
    assert secret_value not in result.stdout
    assert "SECRET_FILE_ABSENT" in result.stdout
    assert "SECRET_FILE_PRESENT" not in result.stdout
    # no unrelated host paths leak into the container
    assert "MARKER_ABSENT" in result.stdout
    assert "MARKER_PRESENT" not in result.stdout
    assert "SECRET_NOT_LEAKED" in result.stdout
    assert "SECRET_LEAKED" not in result.stdout


@docker
def test_timeout_kills_invocation_processes_and_recovers_container(docker_exec):
    """AC-M1-05: timeout leaves no invocation process running; the same
    container is restarted and remains usable with workspace intact."""
    executor, _, _ = docker_exec
    cid = executor._container_id

    executor.execute("echo survived > marker.txt", 10)
    result = executor.execute("sleep 1000", 3)

    assert result.timed_out is True
    assert result.exit_code is None

    # container identity is preserved through kill + restart
    assert executor._container_id == cid
    follow_up = executor.execute("cat marker.txt", 10)
    assert follow_up.exit_code == 0
    assert follow_up.stdout.strip() == "survived"

    # no process from the timed-out invocation remains (only `sleep infinity`)
    check = subprocess.run(
        [
            "docker",
            "exec",
            cid,
            "bash",
            "-c",
            "ps -eo args --no-headers | grep -F 'sleep 1000' | grep -v grep || true",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert check.stdout.strip() == ""


@docker
def test_timeout_kills_detached_child_that_escapes_process_group(docker_exec):
    """AC-M1-05: a double-fork/session-escaping child cannot survive timeout.

    The detached ``setsid`` grandchild escapes both the exec'd process group
    and any recorded-PGID scheme; container-granular killing still reaches it.
    """
    executor, _, _ = docker_exec
    cid = executor._container_id

    result = executor.execute(
        "setsid sh -c 'sleep 1000' >/dev/null 2>&1 < /dev/null & sleep 1000", 3
    )
    assert result.timed_out is True

    check = subprocess.run(
        [
            "docker",
            "exec",
            cid,
            "bash",
            "-c",
            "ps -eo args --no-headers | grep -F 'sleep 1000' | grep -v grep || true",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert check.stdout.strip() == ""

    # the same container was restarted and is usable again
    assert executor.execute("echo alive", 10).stdout.strip() == "alive"


@docker
def test_bounded_output_truncates(docker_exec):
    executor, _, _ = docker_exec
    result = executor.execute(
        "printf 'HEAD_MARK'; yes x | head -c 200000; printf 'TAIL_MARK'", 15
    )
    assert result.truncated is True
    # collection stays bounded: retained prefix + rolling suffix only
    assert len(result.stdout.encode("utf-8")) <= 2 * LOGGED_TOOL_OUTPUT_BYTES
    # M0 normalization produces the canonical bounded head+tail result
    model, logged = normalize_shell_result(result)
    assert len(model.stdout.encode()) <= MODEL_TOOL_OUTPUT_BYTES
    assert len(logged.stdout.encode()) <= LOGGED_TOOL_OUTPUT_BYTES
    assert model.stdout.startswith("HEAD_MARK")
    assert model.stdout.endswith("TAIL_MARK")
    assert TRUNCATION_MARKER in model.stdout


@docker
def test_bounded_output_truncates_stderr(docker_exec):
    executor, _, _ = docker_exec
    result = executor.execute("yes x | head -c 200000 >&2", 10)
    assert result.truncated is True
    assert len(result.stderr.encode("utf-8")) <= 2 * LOGGED_TOOL_OUTPUT_BYTES
    _, logged = normalize_shell_result(result)
    assert len(logged.stderr.encode()) <= LOGGED_TOOL_OUTPUT_BYTES


@docker
@pytest.mark.parametrize("sabotage", ["rm", "stop"])
def test_loop_reports_sandbox_error_when_container_lost_between_calls(
    tmp_path, sandbox_image, sabotage
):
    """AC-M1-14 regression: a removed/stopped owned container between shell
    calls becomes error:sandbox_error, not a normal non-zero ShellResult."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_id = f"FA-GONE-{secrets.token_hex(4)}"
    executor = DockerExecutor(image=sandbox_image)

    def sabotage_container():
        cid = executor._container_id
        args = (
            ["docker", "rm", "-f", cid] if sabotage == "rm" else ["docker", "stop", cid]
        )
        subprocess.run(args, capture_output=True, timeout=30, check=False)

    class SabotageModel(ScriptedModel):
        def generate(self, messages, tools):
            if self.calls:
                sabotage_container()
            return super().generate(messages, tools)

    model = SabotageModel(
        [
            ModelResponse(tool_calls=(ToolCall("a", "shell", {"command": "echo ok"}),)),
            ModelResponse(
                tool_calls=(ToolCall("b", "shell", {"command": "echo again"}),)
            ),
        ]
    )
    loop = AgentLoop(
        model=model,
        executor=executor,
        verifier=ExactStringVerifier("Flag{never}"),
        challenge=ChallengeInput("fixture", "solve it"),
        limits=Limits(
            max_model_turns=5, wall_timeout_seconds=120, command_timeout_seconds=30
        ),
        runs_root=tmp_path,
        monotonic=lambda: 0.0,
        utc_now=lambda: datetime.now(UTC),
        run_id=run_id,
    )

    result = loop.run()
    assert result["status:reason"] == "error:sandbox_error"
    # the first shell call succeeded before the container was lost
    assert result["tool_calls"] == 2


@docker
def test_cleanup_removes_container(docker_exec):
    executor, _, run_id = docker_exec
    cid = executor._container_id

    executor.cleanup(run_id)
    assert executor._container_id is None

    import subprocess as sp

    check = sp.run(
        ["docker", "inspect", "--format", "{{.Id}}", cid],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert check.returncode != 0  # container is gone


@docker
def test_container_cannot_reach_docker_socket(docker_exec):
    """The Agent container must not have the Docker socket mounted."""
    executor, _, _ = docker_exec
    result = executor.execute(
        "ls /var/run/docker.sock 2>/dev/null && echo FOUND || echo ABSENT", 10
    )
    assert "ABSENT" in result.stdout
