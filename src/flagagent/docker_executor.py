"""M1 Docker CLI executor for contained shell execution.

One ``DockerExecutor`` owns one Run-scoped Agent container created in
``prepare`` and removed in ``cleanup``.  Each ``execute`` call first
verifies the owned container is running, then runs ``/bin/bash -lc
<command>`` as a fresh non-interactive process inside that container via
``docker exec``.

Networking (PRD-M1 section 9):

- ``network_mode='none'`` (default): the Agent runs with Docker no-network
  mode and has no intended non-loopback connectivity.  This is the preserved
  M0/M1 baseline.
- ``network_mode='local'``: creates one Run-scoped user-defined **internal**
  bridge, starts one tiny project-owned non-root bounded TCP Target fixture
  (alias ``target``, deterministic marker ``flagagent-target-ok``) with no
  host port publishing, waits for bounded deterministic Target readiness, then
  creates the Agent attached to that network.  ``host``, ``external``,
  ``container:<id>`` and arbitrary names are rejected at construction.

Design constraints (PRD-M1):

- Docker CLI argument vectors only — no Docker SDK, no ``shell=True``.
- Run IDs are validated with :func:`flagagent.artifacts.validate_run_id`
  before any Docker call; they become container/network names and labels.
- One persistent Agent container per Run; filesystem state persists across
  calls but shell-local state (``cd``, exports, jobs) does not.
- Explicit resource limits: memory 2g, cpus 2, pids 256 (Agent); the Target
  uses smaller bounded limits (256m / 0.5 cpu / 64 pids).
- Security defaults: non-root, ``--cap-drop ALL``, ``--security-opt
  no-new-privileges``, no privileged/host network/socket/extra mounts.
- stdout/stderr collected separately.  Each stream retains a bounded prefix
  plus a rolling suffix (``output_limit_bytes`` each) while excess output is
  drained, so collection never buffers unbounded output and M0's
  ``normalize_shell_result`` still renders the canonical head+tail
  truncation for the model and the logs.
- Non-zero exit and command timeout are normal ``ShellResult`` evidence.
  A missing/stopped owned container, Docker exec control failure, and
  preparation/recovery failures raise ``SandboxError``.
- Timeout boundary (AC-M1-05): the entire owned Agent container is killed —
  every process in its PID namespace dies, so untrusted commands cannot
  evade via double-fork/session escapes — the host exec client is waited
  for, then the *same* container is restarted and probed for usability so
  container identity and workspace state survive.  A timed-out invocation
  returns normal ``timed_out`` evidence only when recovery succeeds;
  otherwise ``SandboxError`` is raised.
- Cleanup removes only known Run-owned Agent/Target/network resources (by
  recorded owned IDs) in order; orphan discovery is report-only and never
  deletes or prunes.
"""

import json
import os
import select
import signal
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from flagagent.artifacts import validate_run_id
from flagagent.tools import LOGGED_TOOL_OUTPUT_BYTES, SandboxError, ShellResult

SANDBOX_IMAGE = "flagagent-sandbox:dev"
WORKSPACE_TARGET = "/workspace"
# Retained as the image's named default and for backwards compatibility.
# Runtime containers use the invoking process's numeric UID/GID instead.
AGENT_USER = "agent"
KEEPALIVE_COMMAND = "sleep infinity"
_VERSION = "0.1.0"


def _runtime_user() -> str:
    """Return the invoking process identity for Docker's numeric user flag."""
    return f"{os.getuid()}:{os.getgid()}"


# -- local networking target fixture --------------------------------------
TARGET_IMAGE = "flagagent-target:dev"
TARGET_PORT = 9999
TARGET_MARKER = "flagagent-target-ok"
TARGET_ALIAS = "target"
TARGET_USER = "target"
TARGET_MEMORY = "256m"
TARGET_CPUS = "0.5"
TARGET_PIDS_LIMIT = 64

_NETWORK_DRIVER = "bridge"
_OWNED_LABEL = "flagagent.managed=true"

# ``docker exec`` reuses the command's exit codes and stderr, so exit code 125
# plus a Docker-looking message is only a heuristic trigger.  Before mapping
# it to ``SandboxError``, ``execute`` verifies the owned container and a
# host-side ``docker exec ... /bin/true`` probe.
_DOCKER_CONTROL_ERROR_MARKERS = (
    "Error response from daemon",
    "Cannot connect to the Docker daemon",
    "No such container",
    "is not running",
    "rpc error",
)

# Readiness probe run inside the Target container via ``docker exec``: open a
# short-lived TCP connection to the local Target port and write the marker to
# stdout.  Non-zero exit (connect failure) means not-yet-ready.
_TARGET_READY_PROBE = (
    "import socket, sys\n"
    "try:\n"
    f"    s = socket.create_connection(('127.0.0.1', {TARGET_PORT}), 2)\n"
    "    sys.stdout.write(s.recv(64).decode(errors='ignore'))\n"
    "    s.close()\n"
    "except Exception:\n"
    "    sys.exit(1)\n"
)


@dataclass
class DockerExecutor:
    """Docker CLI executor that owns one Run-scoped Agent sandbox.

    For ``local`` networking it also owns one Run-scoped internal bridge and
    one Target fixture.  ``AgentLoop`` remains Docker-agnostic: it only calls
    ``prepare``/``execute``/``cleanup``.
    """

    image: str = SANDBOX_IMAGE
    docker_bin: str = "docker"
    memory: str = "2g"
    cpus: str = "2"
    pids_limit: int = 256
    output_limit_bytes: int = LOGGED_TOOL_OUTPUT_BYTES
    network_mode: str = "none"
    target_image: str = TARGET_IMAGE
    target_memory: str = TARGET_MEMORY
    target_cpus: str = TARGET_CPUS
    target_pids_limit: int = TARGET_PIDS_LIMIT
    readiness_attempts: int = 30
    readiness_interval: float = 0.5
    monotonic: Callable[[], float] = field(default=time.monotonic, repr=False)
    _container_id: str | None = field(default=None, init=False, repr=False)
    _container_name: str | None = field(default=None, init=False, repr=False)
    _target_id: str | None = field(default=None, init=False, repr=False)
    _target_name: str | None = field(default=None, init=False, repr=False)
    _network_id: str | None = field(default=None, init=False, repr=False)
    _network_name: str | None = field(default=None, init=False, repr=False)
    _resolved_image_id: str | None = field(default=None, init=False, repr=False)
    _preparation_remaining: float | None = field(default=None, init=False, repr=False)
    _preparation_deadline: float | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.network_mode not in ("none", "local"):
            raise ValueError(
                f"unsupported network_mode: {self.network_mode!r}; "
                "only 'none' and 'local' are allowed"
            )

    def set_remaining(self, remaining: float) -> None:
        """Store the Run wall-budget remaining for preparation-time timeouts.

        ``AgentLoop`` calls this (when available) right before ``prepare``
        so that each blocking Docker operation reachable from ``prepare``
        is bounded by the shared Run wall deadline instead of only its own
        fixed default.  A subsequent ``prepare`` consumes and clears the
        value; calling ``prepare`` without ``set_remaining`` keeps the
        fixed default timeouts.
        """
        self._preparation_remaining = max(0.0, float(remaining))

    def _preparation_timeout(self, fixed: float) -> float:
        """Bound a fixed preparation timeout by the Run wall deadline.

        When no preparation deadline is set (``set_remaining`` was not
        called) the fixed default is returned unchanged.  Otherwise the
        timeout is clamped to the remaining budget, and an exhausted
        budget raises ``SandboxError`` before any Docker work.
        """
        deadline = self._preparation_deadline
        if deadline is None:
            return fixed
        remaining = deadline - self.monotonic()
        if remaining <= 0:
            raise SandboxError("preparation budget exhausted")
        return min(fixed, remaining)

    # -- lifecycle -----------------------------------------------------------

    def prepare(self, workspace: Path, run_id: str) -> None:
        """Create and start the Run-scoped Agent sandbox.

        For ``local`` mode this also creates the Run-scoped internal network
        and Target fixture and waits for Target readiness before creating the
        Agent.  Any failure cleans up partially-created owned resources and
        re-raises ``SandboxError``.
        """
        if self._container_id is not None:
            raise SandboxError("sandbox already prepared for this executor")
        try:
            validate_run_id(run_id)
        except ValueError as error:
            raise SandboxError(f"invalid run id: {run_id!r}") from error
        if os.getuid() == 0:
            raise SandboxError(
                "running the Docker sandbox as root is unsupported; "
                "invoke FlagAgent as a non-root user"
            )
        self._container_name = self._container_name_for(run_id)
        remaining = self._preparation_remaining
        self._preparation_remaining = None
        if remaining is not None:
            if remaining <= 0:
                raise SandboxError("preparation budget exhausted")
            self._preparation_deadline = self.monotonic() + remaining
        try:
            if self.network_mode == "local":
                self._prepare_local(workspace, run_id)
            else:
                self._prepare_none(workspace, run_id)
        finally:
            self._preparation_deadline = None

    def execute(self, command: str, timeout_seconds: float) -> ShellResult:
        """Run ``command`` as a fresh process in the Agent container."""
        if self._container_id is None:
            raise SandboxError("agent container is not prepared")
        if not self._is_container_running(self._container_id):
            raise SandboxError("agent container is not running")
        args = self._exec_args(command)
        try:
            process = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except FileNotFoundError as error:
            raise SandboxError("docker CLI not found") from error
        except OSError as error:
            raise SandboxError("docker exec failed to start") from error
        deadline = self.monotonic() + timeout_seconds
        stdout_b, stderr_b, timed_out, truncated = self._collect(process, deadline)
        stdout_text = stdout_b.decode("utf-8", errors="ignore")
        stderr_text = stderr_b.decode("utf-8", errors="ignore")
        if timed_out:
            self._recover_after_timeout(process)
            return ShellResult(stdout_text, stderr_text, None, True, truncated)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._recover_after_timeout(process)
            return ShellResult(stdout_text, stderr_text, None, True, truncated)
        if self._is_control_failure(process.returncode, stderr_text) and (
            not self._is_container_running(self._container_id)
            or not self._docker_ok(
                [self.docker_bin, "exec", self._container_id, "/bin/true"], timeout=10
            )
        ):
            raise SandboxError(f"docker exec control failure: {stderr_text.strip()}")
        return ShellResult(
            stdout_text, stderr_text, process.returncode, False, truncated
        )

    @staticmethod
    def _is_control_failure(exit_code: int | None, stderr_text: str) -> bool:
        return exit_code == 125 and any(
            marker in stderr_text for marker in _DOCKER_CONTROL_ERROR_MARKERS
        )

    def cleanup(self, run_id: str) -> None:
        """Remove all known Run-owned resources (Agent, Target, network).

        Best-effort per resource: each is attempted even if an earlier removal
        fails, so a single failure does not leak the remaining resources.
        Raises ``SandboxError`` if any removal failed.  A cleanup failure after
        a result is committed does not rewrite that result; AgentLoop records
        it separately.
        """
        errors = self._remove_owned()
        if errors:
            raise SandboxError("cleanup failed: " + "; ".join(errors))

    # -- provenance ----------------------------------------------------------

    def sandbox_provenance(self) -> dict[str, Any]:
        """Normalized sandbox configuration for run.json provenance.

        Returns the *static* containment configuration (PRD-M1 section 12):
        backend, image reference, network mode, resource limits, container
        user, security relaxations (normally empty), and a best-effort Docker
        Engine/rootful observation.  Called before ``RunArtifacts.create``.
        """
        engine = self._docker_engine_info()
        return {
            "backend": "docker",
            "image": self.image,
            "network_mode": self.network_mode,
            "memory": self.memory,
            "cpus": self.cpus,
            "pids_limit": self.pids_limit,
            "container_user": _runtime_user(),
            "security_relaxations": [],
            "docker_engine": engine["version"],
            "rootless": engine["rootless"],
        }

    def sandbox_lifecycle(self) -> dict[str, Any]:
        """Resolved runtime IDs for the sandbox lifecycle event.

        Returns a compact dict of Agent/Target/network IDs and the resolved
        image ID (PRD-M1 section 12).  Called after ``prepare`` resolves IDs.
        Does not dump full ``docker inspect``.
        """
        info: dict[str, Any] = {}
        if self._container_id is not None:
            info["agent_container_id"] = self._container_id
        if self._target_id is not None:
            info["target_container_id"] = self._target_id
        if self._network_id is not None:
            info["network_id"] = self._network_id
        if self._network_name is not None:
            info["network_name"] = self._network_name
        if self._resolved_image_id is not None:
            info["image_id"] = self._resolved_image_id
        return info

    def _docker_engine_info(self) -> dict[str, str | bool | None]:
        """Best-effort Docker Engine version and rootful/rootless observation."""
        try:
            result = subprocess.run(
                [self.docker_bin, "info"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return {"version": None, "rootless": None}
        if result.returncode != 0:
            return {"version": None, "rootless": None}
        output = result.stdout
        version: str | None = None
        for line in output.splitlines():
            stripped = line.strip()
            if stripped.startswith("Server Version:"):
                version = stripped.split(":", 1)[1].strip() or None
                break
        rootless = "rootless" in output.lower()
        return {"version": version, "rootless": rootless}

    # -- prepare paths -------------------------------------------------------

    def _prepare_none(self, workspace: Path, run_id: str) -> None:
        self._create_agent(workspace, run_id)

    def _prepare_local(self, workspace: Path, run_id: str) -> None:
        self._network_name = self._network_name_for(run_id)
        self._target_name = self._target_name_for(run_id)
        try:
            self._create_network(run_id)
            self._create_target(run_id)
            self._wait_target_ready()
            self._create_agent(workspace, run_id)
        except SandboxError:
            # Best-effort cleanup of anything partially created, then
            # propagate the original sandbox failure.  When the shared
            # preparation deadline is exhausted, skip the synchronous
            # removal: _remove_owned uses fresh fixed 30s timeouts that
            # would block past the wall deadline.  Ownership stays
            # recorded so AgentLoop's final cleanup path can still
            # remove the partial resources.
            if (
                self._preparation_deadline is not None
                and self.monotonic() >= self._preparation_deadline
            ):
                raise
            self._remove_owned()
            raise

    def _create_agent(self, workspace: Path, run_id: str) -> None:
        args = self._run_args(workspace, run_id)
        try:
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=self._preparation_timeout(60),
                check=False,
            )
        except FileNotFoundError as error:
            raise SandboxError("docker CLI not found") from error
        except subprocess.TimeoutExpired as error:
            raise SandboxError("docker run timed out") from error
        except OSError as error:
            raise SandboxError("docker run failed to start") from error
        if result.returncode != 0:
            raise SandboxError(f"docker run failed: {result.stderr.strip()}")
        container_id = result.stdout.strip()
        if not container_id:
            raise SandboxError("docker run returned no container id")
        self._container_id = container_id
        if not self._is_container_running(self._container_id):
            self._force_remove(self._container_id)
            self._container_id = None
            raise SandboxError("agent container is not running")
        # AC-M1-18: the resolved image identity is required evidence.  If
        # inspect cannot resolve it, remove the owned container and fail
        # preparation before any model execution.
        image_id = self._resolve_image_id(self._container_id)
        if image_id is None:
            self._force_remove(self._container_id)
            self._container_id = None
            raise SandboxError("resolved image identity unavailable")
        self._resolved_image_id = image_id

    def _create_network(self, run_id: str) -> None:
        args = self._network_create_args(run_id)
        try:
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=self._preparation_timeout(30),
                check=False,
            )
        except FileNotFoundError as error:
            raise SandboxError("docker CLI not found") from error
        except subprocess.TimeoutExpired as error:
            raise SandboxError("docker network create timed out") from error
        except OSError as error:
            raise SandboxError("docker network create failed to start") from error
        if result.returncode != 0:
            raise SandboxError(f"docker network create failed: {result.stderr.strip()}")
        network_id = result.stdout.strip()
        if not network_id:
            raise SandboxError("docker network create returned no network id")
        self._network_id = network_id

    def _create_target(self, run_id: str) -> None:
        args = self._target_run_args(run_id)
        try:
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=self._preparation_timeout(60),
                check=False,
            )
        except FileNotFoundError as error:
            raise SandboxError("docker CLI not found") from error
        except subprocess.TimeoutExpired as error:
            raise SandboxError("docker target run timed out") from error
        except OSError as error:
            raise SandboxError("docker target run failed to start") from error
        if result.returncode != 0:
            raise SandboxError(f"docker target run failed: {result.stderr.strip()}")
        target_id = result.stdout.strip()
        if not target_id:
            raise SandboxError("docker target run returned no container id")
        self._target_id = target_id
        if not self._is_container_running(self._target_id):
            self._force_remove(self._target_id)
            self._target_id = None
            raise SandboxError("target container is not running")

    def _wait_target_ready(self) -> None:
        """Bounded deterministic readiness poll for the Target fixture."""
        deadline = self._preparation_deadline
        for _ in range(self.readiness_attempts):
            if deadline is not None and self.monotonic() >= deadline:
                raise SandboxError("preparation budget exhausted")
            if self._target_ready():
                return
            interval = self.readiness_interval
            if deadline is not None:
                interval = min(interval, max(0.0, deadline - self.monotonic()))
            time.sleep(interval)
        raise SandboxError("target readiness check failed")

    def _target_ready(self) -> bool:
        args = [
            self.docker_bin,
            "exec",
            self._target_id,
            "python3",
            "-c",
            _TARGET_READY_PROBE,
        ]
        try:
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=self._preparation_timeout(5),
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return False
        if result.returncode != 0:
            return False
        return TARGET_MARKER in result.stdout

    # -- argument vectors ----------------------------------------------------

    @staticmethod
    def _container_name_for(run_id: str) -> str:
        return f"flagagent-agent-{run_id}"

    @staticmethod
    def _network_name_for(run_id: str) -> str:
        return f"flagagent-net-{run_id}"

    @staticmethod
    def _target_name_for(run_id: str) -> str:
        return f"flagagent-target-{run_id}"

    def _run_args(self, workspace: Path, run_id: str) -> list[str]:
        network = (
            "none" if self.network_mode == "none" else self._network_name_for(run_id)
        )
        return [
            self.docker_bin,
            "run",
            "-d",
            "--name",
            self._container_name_for(run_id),
            "--init",
            "--user",
            _runtime_user(),
            "-w",
            WORKSPACE_TARGET,
            "-v",
            f"{workspace}:{WORKSPACE_TARGET}",
            "--memory",
            self.memory,
            "--cpus",
            self.cpus,
            "--pids-limit",
            str(self.pids_limit),
            "--security-opt",
            "no-new-privileges",
            "--cap-drop",
            "ALL",
            "--network",
            network,
            "--label",
            "flagagent.managed=true",
            "--label",
            f"flagagent.run_id={run_id}",
            "--label",
            "flagagent.role=agent",
            "--label",
            f"flagagent.version={_VERSION}",
            self.image,
            *KEEPALIVE_COMMAND.split(),
        ]

    def _network_create_args(self, run_id: str) -> list[str]:
        return [
            self.docker_bin,
            "network",
            "create",
            "--driver",
            _NETWORK_DRIVER,
            "--internal",
            "--label",
            "flagagent.managed=true",
            "--label",
            f"flagagent.run_id={run_id}",
            "--label",
            "flagagent.role=network",
            "--label",
            f"flagagent.version={_VERSION}",
            self._network_name_for(run_id),
        ]

    def _target_run_args(self, run_id: str) -> list[str]:
        return [
            self.docker_bin,
            "run",
            "-d",
            "--name",
            self._target_name_for(run_id),
            "--network",
            self._network_name_for(run_id),
            "--network-alias",
            TARGET_ALIAS,
            "--user",
            TARGET_USER,
            "--init",
            "--memory",
            self.target_memory,
            "--cpus",
            self.target_cpus,
            "--pids-limit",
            str(self.target_pids_limit),
            "--security-opt",
            "no-new-privileges",
            "--cap-drop",
            "ALL",
            "--label",
            "flagagent.managed=true",
            "--label",
            f"flagagent.run_id={run_id}",
            "--label",
            "flagagent.role=target",
            "--label",
            f"flagagent.version={_VERSION}",
            self.target_image,
        ]

    def _exec_args(self, command: str) -> list[str]:
        return [
            self.docker_bin,
            "exec",
            "-w",
            WORKSPACE_TARGET,
            self._container_id,
            "/bin/bash",
            "-lc",
            command,
        ]

    # -- bounded output collection ------------------------------------------

    def _collect(
        self, process: subprocess.Popen, deadline: float
    ) -> tuple[bytearray, bytearray, bool, bool]:
        """Read stdout/stderr keeping a bounded prefix and rolling suffix.

        Each stream retains at most ``output_limit_bytes`` of prefix plus
        ``output_limit_bytes`` of rolling suffix; excess output is drained and
        discarded so the process never blocks on a full pipe and host memory
        stays bounded.  The retained head+tail view is exactly what M0's
        ``normalize_shell_result`` needs to render the canonical head+tail
        truncation for the model and the logs.  Returns ``(stdout, stderr,
        timed_out, truncated)``.
        """
        limit = self.output_limit_bytes
        stdout = bytearray()
        stderr = bytearray()
        stdout_tail = bytearray()
        stderr_tail = bytearray()
        truncated = False
        try:
            stdout_fd = process.stdout.fileno()
            stderr_fd = process.stderr.fileno()
        except (AttributeError, OSError, ValueError):
            return stdout, stderr, False, truncated
        prefix = {stdout_fd: stdout, stderr_fd: stderr}
        rolling = {stdout_fd: stdout_tail, stderr_fd: stderr_tail}
        observed = {stdout_fd: 0, stderr_fd: 0}

        def view(fd: int) -> bytearray:
            # Exactly the observed bytes when nothing was dropped; otherwise
            # prefix + rolling suffix, which preserves both the true head and
            # the true tail of the stream.
            if observed[fd] <= limit:
                return prefix[fd]
            return prefix[fd] + rolling[fd]

        open_fds = list(prefix)
        while open_fds:
            now = self.monotonic()
            if now >= deadline:
                return view(stdout_fd), view(stderr_fd), True, truncated
            timeout = min(0.2, max(0.0, deadline - now))
            try:
                ready, _, _ = select.select(open_fds, [], [], timeout)
            except (OSError, ValueError):
                break
            if not ready:
                continue
            for fd in ready:
                try:
                    chunk = os.read(fd, 8192)
                except OSError:
                    if fd in open_fds:
                        open_fds.remove(fd)
                    continue
                if not chunk:
                    if fd in open_fds:
                        open_fds.remove(fd)
                    continue
                observed[fd] += len(chunk)
                if observed[fd] > limit:
                    truncated = True
                buf = prefix[fd]
                if len(buf) < limit:
                    buf.extend(chunk[: limit - len(buf)])
                tail = rolling[fd]
                tail.extend(chunk)
                if len(tail) > limit:
                    del tail[: len(tail) - limit]
        return view(stdout_fd), view(stderr_fd), False, truncated

    # -- timeout recovery ----------------------------------------------------

    def _recover_after_timeout(self, process: subprocess.Popen) -> None:
        """Enforce the AC-M1-05 timeout boundary at container granularity.

        Kill the entire owned Agent container (SIGKILL reaches every process
        in its PID namespace — untrusted commands cannot evade their
        namespace), reap the host-side exec client, restart the *same*
        container (preserving container identity, workspace, and mounts), and
        probe that it is usable again.  Raise ``SandboxError`` on any failure
        so timed-out evidence is only returned for a proven-healthy sandbox.
        """
        cid = self._container_id
        if cid is None:
            raise SandboxError("timeout recovery failed: no owned agent container")
        if not self._docker_ok([self.docker_bin, "kill", cid], timeout=30):
            raise SandboxError(
                "timeout recovery failed: could not kill agent container"
            )
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired as error:
            raise SandboxError(
                "timeout recovery failed: exec client did not exit"
            ) from error
        if not self._docker_ok([self.docker_bin, "start", cid], timeout=60):
            raise SandboxError(
                "timeout recovery failed: could not restart agent container"
            )
        if not self._is_container_running(cid):
            raise SandboxError(
                "timeout recovery failed: agent container not running after restart"
            )
        if not self._docker_ok([self.docker_bin, "exec", cid, "/bin/true"], timeout=30):
            raise SandboxError(
                "timeout recovery failed: agent container not usable after restart"
            )

    def _docker_ok(self, args: list[str], timeout: float) -> bool:
        try:
            result = subprocess.run(
                args, capture_output=True, timeout=timeout, check=False
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return False
        return result.returncode == 0

    # -- removal + discovery ------------------------------------------------

    def _remove_owned(self) -> list[str]:
        """Best-effort remove all owned resources in order.

        Order is Agent, then Target, then network (a network cannot be removed
        while containers are attached).  State is cleared before each removal
        so a later failure does not double-remove.  Returns a list of error
        strings (empty when fully successful); never raises.
        """
        errors: list[str] = []
        if self._container_id is not None:
            cid = self._container_id
            self._container_id = None
            self._container_name = None
            self._resolved_image_id = None
            err = self._remove_container(cid)
            if err:
                errors.append(f"agent({cid}): {err}")
        else:
            self._container_name = None
            self._resolved_image_id = None
        if self._target_id is not None:
            tid = self._target_id
            self._target_id = None
            self._target_name = None
            err = self._remove_container(tid)
            if err:
                errors.append(f"target({tid}): {err}")
        else:
            self._target_name = None
        if self._network_id is not None:
            network_id = self._network_id
            network_name = self._network_name or network_id
            self._network_name = None
            self._network_id = None
            err = self._remove_network(network_id)
            if err:
                errors.append(f"network({network_name}): {err}")
        else:
            self._network_name = None
        return errors

    def _remove_container(self, container_id: str) -> str | None:
        args = [self.docker_bin, "rm", "-f", container_id]
        try:
            result = subprocess.run(
                args, capture_output=True, text=True, timeout=30, check=False
            )
        except FileNotFoundError:
            return "docker CLI not found"
        except subprocess.TimeoutExpired:
            return "docker rm timed out"
        except OSError:
            return "docker rm failed"
        if result.returncode != 0:
            stderr = result.stderr.strip()
            if "No such container" in stderr:
                return None
            return f"docker rm failed: {stderr}"
        return None

    def _remove_network(self, network_name: str) -> str | None:
        args = [self.docker_bin, "network", "rm", network_name]
        try:
            result = subprocess.run(
                args, capture_output=True, text=True, timeout=30, check=False
            )
        except FileNotFoundError:
            return "docker CLI not found"
        except subprocess.TimeoutExpired:
            return "docker network rm timed out"
        except OSError:
            return "docker network rm failed"
        if result.returncode != 0:
            stderr = result.stderr.strip()
            if "No such network" in stderr or "not found" in stderr.lower():
                return None
            return f"docker network rm failed: {stderr}"
        return None

    def discover_owned(self) -> dict[str, list[dict[str, Any]]]:
        """Report-only discovery of flagagent-managed Docker resources.

        Lists containers and networks carrying the ``flagagent.managed=true``
        ownership label, with parsed labels.  Does NOT delete, stop, or prune
        anything.  Raises ``SandboxError`` on Docker CLI failure.
        """
        return {
            "containers": self._list_owned_containers(),
            "networks": self._list_owned_networks(),
        }

    def _list_owned_containers(self) -> list[dict[str, Any]]:
        ids = self._list_ids(
            [
                self.docker_bin,
                "ps",
                "-a",
                "--filter",
                f"label={_OWNED_LABEL}",
                "-q",
            ]
        )
        if not ids:
            return []
        return self._inspect_labeled(ids, network=False)

    def _list_owned_networks(self) -> list[dict[str, Any]]:
        ids = self._list_ids(
            [
                self.docker_bin,
                "network",
                "ls",
                "--filter",
                f"label={_OWNED_LABEL}",
                "-q",
            ]
        )
        if not ids:
            return []
        return self._inspect_labeled(ids, network=True)

    def _list_ids(self, args: list[str]) -> list[str]:
        try:
            result = subprocess.run(
                args, capture_output=True, text=True, timeout=30, check=False
            )
        except FileNotFoundError as error:
            raise SandboxError("docker CLI not found") from error
        except subprocess.TimeoutExpired as error:
            raise SandboxError("docker list timed out") from error
        except OSError as error:
            raise SandboxError("docker list failed") from error
        if result.returncode != 0:
            raise SandboxError(f"docker list failed: {result.stderr.strip()}")
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def _inspect_labeled(
        self, ids: list[str], *, network: bool
    ) -> list[dict[str, Any]]:
        if network:
            args = [
                self.docker_bin,
                "network",
                "inspect",
                "--format",
                "{{.Id}}\t{{.Name}}\t{{json .Labels}}",
                *ids,
            ]
        else:
            args = [
                self.docker_bin,
                "inspect",
                "--format",
                "{{.Id}}\t{{.Name}}\t{{json .Config.Labels}}",
                *ids,
            ]
        try:
            result = subprocess.run(
                args, capture_output=True, text=True, timeout=30, check=False
            )
        except FileNotFoundError as error:
            raise SandboxError("docker CLI not found") from error
        except subprocess.TimeoutExpired as error:
            raise SandboxError("docker inspect timed out") from error
        except OSError as error:
            raise SandboxError("docker inspect failed") from error
        if result.returncode != 0:
            raise SandboxError(f"docker inspect failed: {result.stderr.strip()}")
        resources: list[dict[str, Any]] = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split("\t", 2)
            if len(parts) != 3:
                continue
            rid, name, labels_json = parts
            try:
                labels = json.loads(labels_json)
            except (ValueError, TypeError):
                labels = {}
            if not isinstance(labels, dict):
                labels = {}
            if not network and name.startswith("/"):
                name = name[1:]
            resources.append({"id": rid, "name": name, "labels": labels})
        return resources

    # -- helpers -------------------------------------------------------------

    def _is_container_running(self, container_id: str | None) -> bool:
        if container_id is None:
            return False
        args = [
            self.docker_bin,
            "inspect",
            "--format",
            "{{.State.Running}}",
            container_id,
        ]
        try:
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=self._preparation_timeout(10),
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return False
        return result.returncode == 0 and result.stdout.strip() == "true"

    def _container_running(self) -> bool:
        return self._is_container_running(self._container_id)

    def _resolve_image_id(self, container_id: str) -> str | None:
        """Best-effort resolved image ID for the sandbox lifecycle event."""
        args = [
            self.docker_bin,
            "inspect",
            "--format",
            "{{.Image}}",
            container_id,
        ]
        try:
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=self._preparation_timeout(10),
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return None
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None

    def _force_remove(self, container_id: str | None = None) -> None:
        cid = container_id if container_id is not None else self._container_id
        if cid is None:
            return
        args = [self.docker_bin, "rm", "-f", cid]
        try:
            subprocess.run(args, capture_output=True, timeout=30, check=False)
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            pass
