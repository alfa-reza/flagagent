import json
from pathlib import Path

import pytest

from flagagent.cli import build_parser, load_challenge, main


def write_challenge(root: Path, **overrides):
    payload = {
        "identity": "fixture",
        "description": "solve it",
        "expected_flag": "Flag{control}",
        "network_mode": "none",
    }
    payload.update(overrides)
    root.mkdir(parents=True, exist_ok=True)
    (root / "challenge.json").write_text(json.dumps(payload))
    return root


def test_parser_exposes_run_command():
    parser = build_parser()

    args = parser.parse_args(
        [
            "run",
            "--challenge",
            "challenge",
            "--protocol",
            "openai-chat",
            "--model",
            "model",
        ]
    )

    assert args.command == "run"
    assert args.protocol == "openai-chat"


def test_load_challenge_rejects_descriptor_symlink(tmp_path):
    root = tmp_path / "challenge"
    root.mkdir()
    target = tmp_path / "descriptor.json"
    target.write_text("{}")
    (root / "challenge.json").symlink_to(target)

    with pytest.raises(ValueError, match="regular file"):
        load_challenge(root)


def test_load_challenge_rejects_files_symlink(tmp_path):
    root = write_challenge(tmp_path / "challenge")
    target = tmp_path / "real_files"
    target.mkdir()
    (root / "files").symlink_to(target)

    with pytest.raises(ValueError, match="files must be a directory"):
        load_challenge(root)


def test_load_challenge_rejects_broken_files_symlink(tmp_path):
    root = write_challenge(tmp_path / "challenge")
    (root / "files").symlink_to(tmp_path / "missing_target")

    with pytest.raises(ValueError, match="files must be a directory"):
        load_challenge(root)


def test_load_challenge_accepts_absent_files(tmp_path):
    root = write_challenge(tmp_path / "challenge")

    challenge, _ = load_challenge(root)

    assert challenge.source_dir is None


def test_load_challenge_accepts_real_files_directory(tmp_path):
    root = write_challenge(tmp_path / "challenge")
    (root / "files").mkdir()

    challenge, _ = load_challenge(root)

    assert challenge.source_dir == root / "files"


def test_load_challenge_preserves_network_mode(tmp_path):
    root = write_challenge(tmp_path / "challenge", network_mode="local")

    challenge, _ = load_challenge(root)

    assert challenge.network_mode == "local"


def test_load_challenge_returns_control_data_without_expected_flag_in_input(tmp_path):
    root = write_challenge(tmp_path / "challenge", target_context="target:9999")

    challenge, expected = load_challenge(root)

    assert challenge.identity == "fixture"
    assert challenge.target_context == "target:9999"
    assert expected == "Flag{control}"


@pytest.mark.parametrize(
    "payload",
    [
        {"description": "missing identity"},
        {
            "identity": "x",
            "description": "x",
            "expected_flag": "x",
            "network_mode": "bad",
        },
        {
            "identity": "x",
            "description": "x",
            "expected_flag": "x",
            "network_mode": "none",
            "extra": 1,
        },
    ],
)
def test_invalid_descriptor_fails_before_model(tmp_path, payload):
    root = tmp_path / "challenge"
    root.mkdir()
    (root / "challenge.json").write_text(json.dumps(payload))

    with pytest.raises(ValueError):
        load_challenge(root)


def test_main_rejects_api_base_credentials_before_model(monkeypatch, tmp_path, capsys):
    root = write_challenge(tmp_path / "challenge")
    monkeypatch.setenv("OPENAI_API_KEY", "secret")

    exit_code = main(
        [
            "run",
            "--challenge",
            str(root),
            "--protocol",
            "openai-chat",
            "--model",
            "model",
            "--api-base",
            "https://user:pass@example.test/v1",
        ]
    )

    assert exit_code == 2
    assert "credentials" in capsys.readouterr().err


def test_main_rejects_missing_api_key_before_model(monkeypatch, tmp_path, capsys):
    root = write_challenge(tmp_path / "challenge")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    exit_code = main(
        [
            "run",
            "--challenge",
            str(root),
            "--protocol",
            "openai-chat",
            "--model",
            "model",
        ]
    )

    assert exit_code != 0
    assert "OPENAI_API_KEY" in capsys.readouterr().err


def test_main_uses_explicit_protocol_and_key_env(monkeypatch, tmp_path):
    root = write_challenge(tmp_path / "challenge")
    monkeypatch.setenv("TEST_KEY", "secret")
    captured = {}

    class FakeModel:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def generate(self, messages, tools):
            from flagagent.model import ModelResponse

            return ModelResponse(content="stop")

    monkeypatch.setattr("flagagent.cli.ChatCompletionsModel", FakeModel)
    monkeypatch.setattr(
        "flagagent.cli.DockerExecutor",
        lambda **kwargs: __import__(
            "flagagent.tools", fromlist=["FakeExecutor"]
        ).FakeExecutor([]),
    )

    exit_code = main(
        [
            "run",
            "--challenge",
            str(root),
            "--protocol",
            "openai-chat",
            "--model",
            "model",
            "--api-key-env",
            "TEST_KEY",
            "--runs-root",
            str(tmp_path / "runs"),
        ]
    )

    assert exit_code == 0
    assert captured == {"model": "model", "api_key": "secret", "base_url": None}
