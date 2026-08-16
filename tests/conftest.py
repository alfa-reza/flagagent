"""Shared pytest configuration for FlagAgent tests.

Registers the ``docker`` marker and skips Docker integration tests when
Docker Engine is unavailable during ordinary development.  The authoritative
M1 gate must run with Docker available.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

SANDBOX_IMAGE = "flagagent-sandbox:dev"
TARGET_IMAGE = "flagagent-target:dev"
DOCKERFILE_DIR = Path(__file__).resolve().parent.parent / "images" / "sandbox"
TARGET_DOCKERFILE_DIR = Path(__file__).resolve().parent.parent / "images" / "target"


def _docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        result = subprocess.run(
            ["docker", "info"], capture_output=True, timeout=5, check=False
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return result.returncode == 0


def pytest_collection_modifyitems(config, items):
    """Skip ``@pytest.mark.docker`` tests when Docker Engine is unavailable."""
    if not _docker_available():
        skip = pytest.mark.skip(reason="Docker Engine not available")
        for item in items:
            if "docker" in item.keywords:
                item.add_marker(skip)


@pytest.fixture(scope="session")
def sandbox_image():
    """Ensure ``flagagent-sandbox:dev`` exists, building it from the project Dockerfile."""
    result = subprocess.run(
        ["docker", "images", "-q", SANDBOX_IMAGE],
        capture_output=True,
        timeout=10,
        check=False,
    )
    if not result.stdout.strip():
        build = subprocess.run(
            ["docker", "build", "-t", SANDBOX_IMAGE, str(DOCKERFILE_DIR)],
            capture_output=True,
            timeout=180,
            check=False,
        )
        if build.returncode != 0:
            pytest.fail(
                f"failed to build {SANDBOX_IMAGE}: "
                f"{build.stderr.decode(errors='replace').strip()}"
            )
    return SANDBOX_IMAGE


@pytest.fixture(scope="session")
def target_image():
    """Ensure ``flagagent-target:dev`` exists, building it from the project Dockerfile."""
    result = subprocess.run(
        ["docker", "images", "-q", TARGET_IMAGE],
        capture_output=True,
        timeout=10,
        check=False,
    )
    if not result.stdout.strip():
        build = subprocess.run(
            ["docker", "build", "-t", TARGET_IMAGE, str(TARGET_DOCKERFILE_DIR)],
            capture_output=True,
            timeout=180,
            check=False,
        )
        if build.returncode != 0:
            pytest.fail(
                f"failed to build {TARGET_IMAGE}: "
                f"{build.stderr.decode(errors='replace').strip()}"
            )
    return TARGET_IMAGE
