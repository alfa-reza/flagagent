import os
import stat
import subprocess

import pytest

from flagagent.loop import (
    InvalidChallengeSourceError,
    _snapshot_source_files,
    _stage_source_files,
)


def test_executable_snapshot_and_stage_can_be_invoked(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    helper = source / "runme"
    helper.write_text("#!/bin/sh\necho ok\n")
    os.chmod(helper, 0o755)
    files, _, tmp = _snapshot_source_files(source)
    try:
        assert files[0][0].stat().st_mode & 0o111 == 0o111
        workspace = tmp_path / "ws"
        workspace.mkdir()
        _stage_source_files(workspace, files)
        staged = workspace / "runme"
        assert staged.stat().st_mode & 0o111 == 0o111
        assert staged.stat().st_mode & stat.S_IXUSR
        out = subprocess.check_output([str(staged)], text=True)
        assert out.strip() == "ok"
    finally:
        tmp.cleanup()


def test_non_executable_not_become_executable(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    plain = source / "plain.txt"
    plain.write_text("hello")
    os.chmod(plain, 0o644)
    files, _, tmp = _snapshot_source_files(source)
    try:
        assert files[0][0].stat().st_mode & 0o111 == 0
        workspace = tmp_path / "ws"
        workspace.mkdir()
        _stage_source_files(workspace, files)
        staged = workspace / "plain.txt"
        assert staged.stat().st_mode & 0o111 == 0
    finally:
        tmp.cleanup()


def test_setuid_setgid_not_propagated(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    suid = source / "suid"
    suid.write_text("x")
    os.chmod(suid, 0o4755)
    sgid = source / "sgid"
    sgid.write_text("y")
    os.chmod(sgid, 0o2755)
    files, _, tmp = _snapshot_source_files(source)
    try:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        _stage_source_files(workspace, files)
        for name in ("suid", "sgid"):
            mode = (workspace / name).stat().st_mode
            assert not mode & stat.S_ISUID
            assert not mode & stat.S_ISGID
            assert mode & 0o111
    finally:
        tmp.cleanup()


def test_exec_bits_granularity(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    f = source / "tool"
    f.write_text("x")
    os.chmod(f, 0o750)
    files, _, tmp = _snapshot_source_files(source)
    try:
        snap_mode = files[0][0].stat().st_mode & 0o777
        assert snap_mode & 0o111 == 0o110
        assert not snap_mode & stat.S_ISUID
        workspace = tmp_path / "ws"
        workspace.mkdir()
        _stage_source_files(workspace, files)
        dest_mode = (workspace / "tool").stat().st_mode & 0o777
        assert dest_mode & 0o111 == 0o110
    finally:
        tmp.cleanup()


def test_source_sha256_unchanged_by_exec_bits(tmp_path):
    a = tmp_path / "a"
    a.mkdir()
    (a / "file.txt").write_text("same")
    os.chmod(a / "file.txt", 0o644)
    b = tmp_path / "b"
    b.mkdir()
    (b / "file.txt").write_text("same")
    os.chmod(b / "file.txt", 0o755)
    _, digest_a, tmp_a = _snapshot_source_files(a)
    _, digest_b, tmp_b = _snapshot_source_files(b)
    try:
        assert digest_a == digest_b
    finally:
        tmp_a.cleanup()
        tmp_b.cleanup()


def test_symlink_still_rejected(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("secret")
    (source / "link").symlink_to(outside)
    with pytest.raises(InvalidChallengeSourceError, match="symlink"):
        _snapshot_source_files(source)


def test_special_file_still_rejected(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    os.mkfifo(source / "fifo")
    with pytest.raises(InvalidChallengeSourceError, match="special file"):
        _snapshot_source_files(source)


def test_source_limits_still_enforced(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "big.bin").write_bytes(b"x" * 11)
    from flagagent.loop import Limits

    limits = Limits(max_source_file_bytes=10)
    with pytest.raises(InvalidChallengeSourceError, match="file too large"):
        _snapshot_source_files(source, limits)
