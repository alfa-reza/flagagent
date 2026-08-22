"""M1 Docker networking and ownership slice.

Unit tests (no Docker required) verify:

- ``network_mode`` accepts only ``none`` and ``local`` and rejects ``host``,
  ``external``, ``container:<id>``, and arbitrary names;
- ``local`` argument vectors build a Run-scoped internal bridge, a bounded
  non-root Target with alias ``target`` and no host port publishing, and an
  Agent attached to that network;
- partial-failure paths map Docker/network/Target failures to ``SandboxError``
  and clean up owned resources;
- cleanup removes owned Agent/Target/network in order;
- orphan discovery is report-only and never deletes or prunes.

Docker integration tests (marked ``@pytest.mark.docker``) verify local target
reachability, internal network + no egress, Target posture, cleanup, and
orphan discovery.  Tests skip when Docker Engine is unavailable.
"""

import contextlib
import json
import os
import secrets
import subprocess
from pathlib import Path

import pytest

from flagagent.docker_executor import (
    _VERSION,
    TARGET_ALIAS,
    TARGET_IMAGE,
    TARGET_MARKER,
    TARGET_PORT,
    TARGET_USER,
    DockerExecutor,
)
from flagagent.tools import SandboxError

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


class _FakeRun:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# ---------------------------------------------------------------------------
# Unit tests — network_mode validation (no Docker required)
# ---------------------------------------------------------------------------


def test_network_mode_defaults_to_none():
    assert DockerExecutor().network_mode == "none"


@pytest.mark.parametrize(
    "mode", ["host", "external", "bridge", "mybridge", "container:abc", "none_net", ""]
)
def test_network_mode_rejects_unsupported(mode):
    with pytest.raises(ValueError):
        DockerExecutor(network_mode=mode)


def test_network_mode_accepts_none_and_local():
    assert DockerExecutor(network_mode="none").network_mode == "none"
    assert DockerExecutor(network_mode="local").network_mode == "local"


# ---------------------------------------------------------------------------
# Unit tests — local argument vectors (no Docker required)
# ---------------------------------------------------------------------------


def test_local_run_args_use_run_scoped_network_not_none():
    executor = DockerExecutor(network_mode="local")
    args = executor._run_args(Path("/tmp/ws"), RUN_ID)
    assert _value(args, "--network") == DockerExecutor._network_name_for(RUN_ID)
    assert "none" not in _values(args, "--network")


def test_local_run_args_preserve_security_and_resource_flags():
    executor = DockerExecutor(network_mode="local")
    args = executor._run_args(Path("/tmp/ws"), RUN_ID)
    assert _value(args, "--user") == f"{os.getuid()}:{os.getgid()}"
    assert _value(args, "--memory") == "2g"
    assert _value(args, "--cpus") == "2"
    assert _value(args, "--pids-limit") == "256"
    assert "no-new-privileges" in _values(args, "--security-opt")
    assert "ALL" in _values(args, "--cap-drop")
    assert "--privileged" not in args
    assert "host" not in _values(args, "--network")
    assert "/var/run/docker.sock" not in args


def test_local_run_args_keep_agent_labels():
    executor = DockerExecutor(network_mode="local")
    args = executor._run_args(Path("/tmp/ws"), RUN_ID)
    labels = _values(args, "--label")
    assert "flagagent.managed=true" in labels
    assert f"flagagent.run_id={RUN_ID}" in labels
    assert "flagagent.role=agent" in labels
    assert f"flagagent.version={_VERSION}" in labels


def test_network_create_args_are_internal_bridge_with_labels():
    executor = DockerExecutor(network_mode="local")
    args = executor._network_create_args(RUN_ID)
    assert args[:3] == ["docker", "network", "create"]
    assert _value(args, "--driver") == "bridge"
    assert "--internal" in args
    assert args[-1] == DockerExecutor._network_name_for(RUN_ID)
    labels = _values(args, "--label")
    assert "flagagent.managed=true" in labels
    assert f"flagagent.run_id={RUN_ID}" in labels
    assert "flagagent.role=network" in labels
    assert f"flagagent.version={_VERSION}" in labels


def test_target_run_args_have_alias_bounds_labels_and_no_publish():
    executor = DockerExecutor(network_mode="local")
    args = executor._target_run_args(RUN_ID)
    assert args[:2] == ["docker", "run"]
    assert "-d" in args
    assert _value(args, "--name") == DockerExecutor._target_name_for(RUN_ID)
    assert _value(args, "--network") == DockerExecutor._network_name_for(RUN_ID)
    assert _value(args, "--network-alias") == TARGET_ALIAS
    assert _value(args, "--user") == TARGET_USER
    assert "--init" in args
    assert _value(args, "--memory") == "256m"
    assert _value(args, "--cpus") == "0.5"
    assert _value(args, "--pids-limit") == "64"
    assert "no-new-privileges" in _values(args, "--security-opt")
    assert "ALL" in _values(args, "--cap-drop")
    # no host port publishing, no host mounts, no privileged, no socket
    assert "-p" not in args
    assert "--publish" not in args
    assert "-v" not in args
    assert "--volume" not in args
    assert "--privileged" not in args
    assert "/var/run/docker.sock" not in args
    labels = _values(args, "--label")
    assert "flagagent.managed=true" in labels
    assert f"flagagent.run_id={RUN_ID}" in labels
    assert "flagagent.role=target" in labels
    assert f"flagagent.version={_VERSION}" in labels
    assert args[-1] == TARGET_IMAGE


def test_network_and_target_names_are_run_scoped():
    assert DockerExecutor._network_name_for(RUN_ID) == f"flagagent-net-{RUN_ID}"
    assert DockerExecutor._target_name_for(RUN_ID) == f"flagagent-target-{RUN_ID}"
    assert DockerExecutor._container_name_for(RUN_ID) == f"flagagent-agent-{RUN_ID}"


# ---------------------------------------------------------------------------
# Unit tests — local prepare failure mapping + partial cleanup (no Docker)
# ---------------------------------------------------------------------------


def test_local_prepare_network_create_failure_raises_sandbox_error(monkeypatch):
    def fake_run(args, **kwargs):
        if args[1:3] == ["network", "create"]:
            return _FakeRun(returncode=1, stderr="Error: network exists")
        return _FakeRun()

    monkeypatch.setattr(subprocess, "run", fake_run)
    executor = DockerExecutor(network_mode="local")
    with pytest.raises(SandboxError, match="network create failed"):
        executor.prepare(Path("/tmp/ws"), RUN_ID)
    assert executor._network_id is None
    assert executor._network_name == DockerExecutor._network_name_for(RUN_ID)
    assert executor._pending_network is True
    assert executor._target_id is None
    assert executor._container_id is None


def test_local_prepare_target_create_failure_cleans_up_network(monkeypatch):
    state = {"network_removed": False}

    def fake_run(args, **kwargs):
        if args[1:3] == ["network", "create"]:
            return _FakeRun(stdout="netid\n")
        if args[1] == "run":  # target run fails
            return _FakeRun(returncode=1, stderr="Error: no such image")
        if args[1:3] == ["network", "rm"]:
            state["network_removed"] = True
            return _FakeRun()
        return _FakeRun()

    monkeypatch.setattr(subprocess, "run", fake_run)
    executor = DockerExecutor(network_mode="local")
    with pytest.raises(SandboxError, match="target run failed"):
        executor.prepare(Path("/tmp/ws"), RUN_ID)
    assert state["network_removed"] is True
    assert executor._network_name is None
    assert executor._target_id is None


def test_local_prepare_readiness_failure_raises_sandbox_error_and_cleans_up(
    monkeypatch,
):
    state = {
        "network_created": False,
        "network_removed": False,
        "target_removed": False,
    }

    def fake_run(args, **kwargs):
        if args[1:3] == ["network", "create"]:
            state["network_created"] = True
            return _FakeRun(stdout="netid\n")
        if args[1] == "run":  # target run succeeds
            return _FakeRun(stdout="targetcid\n")
        if args[1] == "inspect" and "{{.State.Running}}" in args:
            return _FakeRun(stdout="true\n")
        if args[1] == "exec":  # readiness probe always fails
            return _FakeRun(returncode=1, stderr="not ready")
        if args[1] == "rm":
            state["target_removed"] = True
            return _FakeRun()
        if args[1:3] == ["network", "rm"]:
            state["network_removed"] = True
            return _FakeRun()
        return _FakeRun()

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("time.sleep", lambda *_: None)
    executor = DockerExecutor(
        network_mode="local", readiness_attempts=3, readiness_interval=0.0
    )
    with pytest.raises(SandboxError, match="readiness"):
        executor.prepare(Path("/tmp/ws"), RUN_ID)
    assert state["network_created"] is True
    assert state["target_removed"] is True
    assert state["network_removed"] is True
    assert executor._network_name is None
    assert executor._target_id is None
    assert executor._container_id is None


# ---------------------------------------------------------------------------
# Unit tests — cleanup order (no Docker required)
# ---------------------------------------------------------------------------


def test_local_cleanup_removes_agent_target_network_in_order(monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        return _FakeRun()

    monkeypatch.setattr(subprocess, "run", fake_run)
    executor = DockerExecutor(network_mode="local")
    executor._container_id = "agent-cid"
    executor._container_name = "agent-name"
    executor._target_id = "target-cid"
    executor._target_name = "target-name"
    executor._network_name = "net-name"
    executor._network_id = "net-id"

    executor.cleanup(RUN_ID)

    assert executor._container_id is None
    assert executor._target_id is None
    assert executor._network_name is None
    assert executor._network_id is None

    rm_targets = [a for a in calls if a[1:3] == ["rm", "-f"]]
    assert rm_targets == [
        ["docker", "rm", "-f", "agent-cid"],
        ["docker", "rm", "-f", "target-cid"],
    ]
    # the network is removed by its recorded owned ID, not its mutable name
    net_rm = [a for a in calls if a[1:3] == ["network", "rm"]]
    assert net_rm == [["docker", "network", "rm", "net-id"]]
    assert "net-name" not in next(a for a in calls if a[1:3] == ["network", "rm"])
    # network removal happens after both container removals
    assert calls.index(rm_targets[1]) < calls.index(net_rm[0])


def test_local_cleanup_is_noop_when_not_prepared(monkeypatch):
    called = []

    def fake_run(args, **kwargs):
        called.append(args)
        return _FakeRun()

    monkeypatch.setattr(subprocess, "run", fake_run)
    executor = DockerExecutor(network_mode="local")
    executor.cleanup(RUN_ID)  # must not raise
    assert called == []


def test_local_cleanup_raises_on_failure_but_attempts_all(monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        # agent rm fails, others succeed
        if "agent-cid" in args:
            return _FakeRun(returncode=1, stderr="Error: rm failed")
        return _FakeRun()

    monkeypatch.setattr(subprocess, "run", fake_run)
    executor = DockerExecutor(network_mode="local")
    executor._container_id = "agent-cid"
    executor._target_id = "target-cid"
    executor._network_id = "net-id"

    with pytest.raises(SandboxError, match="cleanup failed"):
        executor.cleanup(RUN_ID)
    rm_cids = [a for a in calls if a[1:3] == ["rm", "-f"]]
    assert [a[-1] for a in rm_cids] == ["agent-cid", "target-cid"]
    assert ["docker", "network", "rm", "net-id"] in calls


# ---------------------------------------------------------------------------
# Unit tests — orphan discovery is report-only (no Docker required)
# ---------------------------------------------------------------------------


def test_discover_owned_returns_containers_and_networks(monkeypatch):
    container_line = "cid123\t/flagagent-agent-x\t" + json.dumps(
        {
            "flagagent.managed": "true",
            "flagagent.run_id": "x",
            "flagagent.role": "agent",
        }
    )
    network_line = "nid456\tflagagent-net-x\t" + json.dumps(
        {
            "flagagent.managed": "true",
            "flagagent.run_id": "x",
            "flagagent.role": "network",
        }
    )

    def fake_run(args, **kwargs):
        if args[1] == "ps":
            return _FakeRun(stdout="cid123\n")
        if args[1] == "network" and args[2] == "ls":
            return _FakeRun(stdout="nid456\n")
        if args[1] == "inspect":
            return _FakeRun(stdout=container_line + "\n")
        if args[1] == "network" and args[2] == "inspect":
            return _FakeRun(stdout=network_line + "\n")
        return _FakeRun()

    monkeypatch.setattr(subprocess, "run", fake_run)
    executor = DockerExecutor()
    report = executor.discover_owned()

    assert [c["name"] for c in report["containers"]] == ["flagagent-agent-x"]
    assert report["containers"][0]["labels"]["flagagent.role"] == "agent"
    assert report["containers"][0]["id"] == "cid123"
    assert [n["name"] for n in report["networks"]] == ["flagagent-net-x"]
    assert report["networks"][0]["labels"]["flagagent.role"] == "network"
    assert report["networks"][0]["id"] == "nid456"


def test_discover_owned_returns_empty_when_no_owned(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeRun(stdout=""))
    executor = DockerExecutor()
    report = executor.discover_owned()
    assert report == {"containers": [], "networks": []}


def test_discover_owned_never_deletes_or_prunes(monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        return _FakeRun(stdout="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    executor = DockerExecutor()
    executor.discover_owned()
    assert calls, "expected at least one docker call"
    for args in calls:
        sub = args[1]
        assert sub in ("ps", "inspect", "network"), f"unexpected subcommand: {sub}"
        if sub == "network":
            assert args[2] in ("ls", "inspect"), f"unexpected network sub: {args[2]}"
        assert "prune" not in args
        assert "rm" not in args
        assert "stop" not in args


# ---------------------------------------------------------------------------
# Docker integration tests
# ---------------------------------------------------------------------------

docker = pytest.mark.docker


def _inspect(resource_id, fmt, *, network=False):
    cmd = (
        ["docker", "network", "inspect", "--format", fmt, resource_id]
        if network
        else ["docker", "inspect", "--format", fmt, resource_id]
    )
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=10, check=False
    )
    assert result.returncode == 0, f"inspect failed: {result.stderr}"
    return result.stdout.strip()


def _inspect_json(resource_id, fmt, *, network=False):
    return json.loads(_inspect(resource_id, fmt, network=network))


def _gone(kind, identifier):
    cmd = (
        ["docker", "network", "inspect", "--format", "{{.Id}}", identifier]
        if kind == "network"
        else ["docker", "inspect", "--format", "{{.Id}}", identifier]
    )
    return (
        subprocess.run(
            cmd, capture_output=True, text=True, timeout=10, check=False
        ).returncode
        != 0
    )


@pytest.fixture
def docker_local(tmp_path, sandbox_image, target_image):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_id = f"FA-LOC-{secrets.token_hex(4)}"
    executor = DockerExecutor(
        image=sandbox_image, target_image=target_image, network_mode="local"
    )
    executor.prepare(workspace, run_id)
    yield executor, workspace, run_id
    with contextlib.suppress(Exception):
        executor.cleanup(run_id)


@docker
def test_local_creates_run_scoped_internal_network(docker_local):
    _, _, run_id = docker_local
    net_name = DockerExecutor._network_name_for(run_id)
    assert _inspect(net_name, "{{.Internal}}", network=True) == "true"
    assert _inspect(net_name, "{{.Driver}}", network=True) == "bridge"
    labels = _inspect_json(net_name, "{{json .Labels}}", network=True)
    assert labels["flagagent.managed"] == "true"
    assert labels["flagagent.run_id"] == run_id
    assert labels["flagagent.role"] == "network"
    assert labels["flagagent.version"] == "0.1.0"


@docker
def test_local_target_reachable_via_alias(docker_local):
    executor, _, _ = docker_local
    result = executor.execute(
        'read -t 3 line < /dev/tcp/target/9999 && printf "%s" "$line"', 15
    )
    assert result.exit_code == 0
    assert TARGET_MARKER in result.stdout


@docker
def test_local_network_blocks_external_egress(docker_local):
    executor, _, _ = docker_local
    # --internal must block outbound external connectivity.
    result = executor.execute(
        'timeout 3 bash -c "echo > /dev/tcp/1.1.1.1/80" 2>/dev/null '
        "&& echo REACHABLE || echo BLOCKED",
        15,
    )
    assert "BLOCKED" in result.stdout
    assert "REACHABLE" not in result.stdout


@docker
def test_local_target_posture(docker_local):
    executor, _, run_id = docker_local
    tid = executor._target_id
    assert tid is not None

    assert _inspect(tid, "{{.HostConfig.Privileged}}") == "false"
    assert _inspect(tid, "{{.Config.User}}") == TARGET_USER
    security_opt = _inspect_json(tid, "{{json .HostConfig.SecurityOpt}}")
    cap_drop = _inspect_json(tid, "{{json .HostConfig.CapDrop}}")
    cap_add = _inspect_json(tid, "{{json .HostConfig.CapAdd}}")
    assert "no-new-privileges" in security_opt
    assert "ALL" in cap_drop
    assert cap_add == [] or cap_add is None
    # seccomp is not disabled: no profile override, Docker default stays active
    assert not [opt for opt in security_opt if "seccomp" in opt.lower()]

    # bounded resources
    memory = int(_inspect(tid, "{{.HostConfig.Memory}}"))
    nano_cpus = int(_inspect(tid, "{{.HostConfig.NanoCpus}}"))
    pids = int(_inspect(tid, "{{.HostConfig.PidsLimit}}"))
    assert memory == 256 * 1024**2
    assert nano_cpus == 500_000_000
    assert pids == 64

    # no host mounts (implies no docker socket / host paths)
    mounts = _inspect_json(tid, "{{json .Mounts}}")
    assert mounts == [] or mounts is None

    # no host port publishing
    port_bindings = _inspect_json(tid, "{{json .HostConfig.PortBindings}}")
    assert port_bindings is None or port_bindings == {}

    # target joins only the run network, with alias 'target'
    nets = _inspect_json(tid, "{{json .NetworkSettings.Networks}}")
    assert len(nets) == 1
    net_name = DockerExecutor._network_name_for(run_id)
    assert net_name in nets
    aliases = nets[net_name].get("Aliases", [])
    assert TARGET_ALIAS in aliases

    # target carries ownership labels
    labels = _inspect_json(tid, "{{json .Config.Labels}}")
    assert labels["flagagent.managed"] == "true"
    assert labels["flagagent.run_id"] == run_id
    assert labels["flagagent.role"] == "target"
    assert labels["flagagent.version"] == "0.1.0"

    # deterministic marker is served
    probe = (
        "import socket,sys;"
        f"s=socket.create_connection(('127.0.0.1',{TARGET_PORT}),3);"
        "sys.stdout.write(s.recv(64).decode());s.close()"
    )
    out = subprocess.run(
        ["docker", "exec", tid, "python3", "-c", probe],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert out.returncode == 0
    assert TARGET_MARKER in out.stdout


@docker
def test_local_cleanup_removes_all_owned_resources(docker_local):
    executor, _, run_id = docker_local
    cid = executor._container_id
    tid = executor._target_id
    net_name = executor._network_name
    assert cid is not None
    assert tid is not None
    assert net_name is not None

    executor.cleanup(run_id)
    assert executor._container_id is None
    assert executor._target_id is None
    assert executor._network_name is None

    # agent gone
    assert _gone("container", cid)
    # target gone
    assert _gone("container", tid)
    # network gone (successful network removal implies containers were
    # detached/removed first)
    assert _gone("network", net_name)


@docker
def test_orphan_discovery_finds_labeled_leftover_without_deleting(sandbox_image):
    executor = DockerExecutor()
    run_id = f"FA-ORPHAN-{secrets.token_hex(4)}"
    net_name = f"flagagent-net-{run_id}"
    tgt_name = f"flagagent-target-{run_id}"
    try:
        # leftover network with ownership labels
        r = subprocess.run(
            [
                "docker",
                "network",
                "create",
                "--driver",
                "bridge",
                "--internal",
                "--label",
                "flagagent.managed=true",
                "--label",
                f"flagagent.run_id={run_id}",
                "--label",
                "flagagent.role=network",
                "--label",
                "flagagent.version=0.1.0",
                net_name,
            ],
            capture_output=True,
            timeout=30,
            check=False,
        )
        assert r.returncode == 0, r.stderr.decode(errors="replace")
        # leftover container with ownership labels
        r = subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                tgt_name,
                "--label",
                "flagagent.managed=true",
                "--label",
                f"flagagent.run_id={run_id}",
                "--label",
                "flagagent.role=target",
                "--label",
                "flagagent.version=0.1.0",
                sandbox_image,
                "sleep",
                "60",
            ],
            capture_output=True,
            timeout=60,
            check=False,
        )
        assert r.returncode == 0, r.stderr.decode(errors="replace")

        report = executor.discover_owned()
        net_names = [n["name"] for n in report["networks"]]
        con_names = [c["name"] for c in report["containers"]]
        assert net_name in net_names
        assert tgt_name in con_names
        net_leftover = next(n for n in report["networks"] if n["name"] == net_name)
        assert net_leftover["labels"]["flagagent.run_id"] == run_id
        assert net_leftover["labels"]["flagagent.role"] == "network"
        con_leftover = next(c for c in report["containers"] if c["name"] == tgt_name)
        assert con_leftover["labels"]["flagagent.role"] == "target"

        # discovery must not delete anything
        assert not _gone("network", net_name)
        assert not _gone("container", tgt_name)
    finally:
        subprocess.run(
            ["docker", "rm", "-f", tgt_name],
            capture_output=True,
            timeout=30,
            check=False,
        )
        subprocess.run(
            ["docker", "network", "rm", net_name],
            capture_output=True,
            timeout=30,
            check=False,
        )
