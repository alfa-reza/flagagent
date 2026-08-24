import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
_RE = re.compile(r"^FROM\s+\S+:.*@sha256:[0-9a-f]{64}\s*$")


def _first_from(path: Path) -> str:
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("FROM "):
            return stripped
    raise AssertionError(f"no FROM in {path}")


def test_sandbox_base_is_pinned():
    frm = _first_from(ROOT / "images/sandbox/Dockerfile")
    assert (
        frm
        == "FROM ubuntu:24.04@sha256:33ceb71981b602c1a7443a53469e4dba065f7503eab3078a2d7a57a2ab987517"
    )
    assert _RE.match(frm)


def test_target_base_is_pinned():
    frm = _first_from(ROOT / "images/target/Dockerfile")
    assert (
        frm
        == "FROM python:3.12-slim@sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a"
    )
    assert _RE.match(frm)
