import hashlib
import json
import os
import types
from datetime import UTC, datetime

from flagagent.anthropic_messages import AnthropicMessagesModel
from flagagent.loop import AgentLoop, ChallengeInput, Limits
from flagagent.model import ModelResponse, ScriptedModel, ToolCall
from flagagent.prompt import (
    SOLVER_PROMPT,
    SOLVER_PROMPT_SHA256,
    SOLVER_PROMPT_VERSION,
)
from flagagent.providers import ChatCompletionsModel
from flagagent.responses import ResponsesModel
from flagagent.tools import (
    TOOL_DEFINITIONS,
    ExactStringVerifier,
    FakeExecutor,
    ShellResult,
)

NOW = datetime(2026, 8, 17, tzinfo=UTC)


class Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value


class PreparingExecutor(FakeExecutor):
    def __init__(self, script):
        super().__init__(script)
        self.workspace_seen = None

    def prepare(self, workspace, run_id):
        self.workspace_seen = workspace
        super().prepare(workspace, run_id)


def make_loop(tmp_path, model, executor=None, challenge=None, **kwargs):
    return AgentLoop(
        model=model,
        executor=executor or FakeExecutor([]),
        verifier=ExactStringVerifier("Flag{control-only}"),
        challenge=challenge or ChallengeInput("fixture", "solve it"),
        limits=Limits(
            max_model_turns=5, wall_timeout_seconds=100, command_timeout_seconds=10
        ),
        runs_root=tmp_path,
        monotonic=Clock(),
        utc_now=lambda: NOW,
        run_id="FA-20260817T000000Z-12345678",
        **kwargs,
    )


def test_solver_prompt_is_versioned_and_hashed():
    assert SOLVER_PROMPT_VERSION == "0.1.0"
    assert SOLVER_PROMPT_SHA256 == hashlib.sha256(SOLVER_PROMPT.encode()).hexdigest()
    for term in ("authorized", "shell", "submit_flag", "non-interactive", "verifier"):
        assert term in SOLVER_PROMPT.lower()


def test_default_run_id_creates_committed_artifacts(tmp_path):
    loop = make_loop(tmp_path, ScriptedModel([ModelResponse(content="stop")]))

    result = loop.run()

    assert result["status:reason"] == "unsolved:model_stop"
    assert loop.artifacts.run_path.exists()
    assert loop.artifacts.result_path.exists()


def test_prompt_hash_must_match_prompt_text(tmp_path):
    import pytest

    with pytest.raises(ValueError, match="does not match"):
        make_loop(
            tmp_path,
            ScriptedModel([ModelResponse(content="stop")]),
            system_prompt=SOLVER_PROMPT,
            prompt_version=SOLVER_PROMPT_VERSION,
            prompt_sha256="0" * 64,
        )


def test_prompt_metadata_cannot_be_partial(tmp_path):
    import pytest

    with pytest.raises(ValueError, match="requires a system prompt"):
        make_loop(
            tmp_path,
            ScriptedModel([ModelResponse(content="stop")]),
            prompt_version=SOLVER_PROMPT_VERSION,
        )


def test_prompt_and_model_provenance_precede_user_context_and_usage_is_recorded(
    tmp_path,
):
    model = ScriptedModel(
        [ModelResponse(content="stop", usage={"input_tokens": 3, "output_tokens": 2})]
    )
    loop = make_loop(
        tmp_path,
        model,
        system_prompt=SOLVER_PROMPT,
        prompt_version=SOLVER_PROMPT_VERSION,
        prompt_sha256=SOLVER_PROMPT_SHA256,
        model_identity="test-model",
        protocol="openai-chat",
        api_base="https://example.test/v1",
    )

    result = loop.run()

    assert [message["role"] for message in model.calls[0][0][:2]] == ["system", "user"]
    metadata = json.loads(loop.artifacts.run_path.read_text())
    assert metadata["prompt"] == {
        "version": SOLVER_PROMPT_VERSION,
        "sha256": SOLVER_PROMPT_SHA256,
    }
    assert metadata["model"] == {
        "name": "test-model",
        "protocol": "openai-chat",
        "base_url": "https://example.test/v1",
    }
    assert result["input_tokens"] == 3
    assert result["output_tokens"] == 2
    assert "Flag{control-only}" not in loop.artifacts.run_path.read_text()


def test_usage_totals_sum_only_present_fields(tmp_path):
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=(ToolCall("s", "shell", {"command": "true"}),),
                usage={"input_tokens": 2},
            ),
            ModelResponse(content="stop", usage={"output_tokens": 5}),
        ]
    )
    loop = make_loop(
        tmp_path,
        model,
        executor=FakeExecutor([ShellResult("", "", 0, False)]),
    )

    result = loop.run()

    assert result["input_tokens"] == 2
    assert result["output_tokens"] == 5


def test_challenge_files_hash_and_stage_before_prepare(tmp_path):
    source = tmp_path / "source"
    (source / "nested").mkdir(parents=True)
    (source / "data.txt").write_text("data")
    (source / "nested" / "other.txt").write_text("other")
    executor = PreparingExecutor([ShellResult("ok", "", 0, False)])
    model = ScriptedModel([ModelResponse(content="stop")])
    loop = make_loop(
        tmp_path / "runs",
        model,
        executor=executor,
        challenge=ChallengeInput("files", "inspect files", source_dir=source),
    )

    loop.run()

    assert executor.workspace_seen is not None
    assert (executor.workspace_seen / "data.txt").read_text() == "data"
    assert (executor.workspace_seen / "nested" / "other.txt").read_text() == "other"
    metadata = json.loads(loop.artifacts.run_path.read_text())
    assert len(metadata["challenge"]["source_sha256"]) == 64
    assert "expected_flag" not in metadata["challenge"]


def test_source_staging_failure_does_not_prepare_executor(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    file_path = source / "data.txt"
    file_path.write_text("data")
    executor = PreparingExecutor([])

    def fail_stage(workspace, files):
        raise ValueError("source changed during staging")

    monkeypatch.setattr("flagagent.loop._stage_source_files", fail_stage)
    loop = make_loop(
        tmp_path / "runs",
        ScriptedModel([ModelResponse(content="stop")]),
        executor=executor,
        challenge=ChallengeInput("files", "inspect files", source_dir=source),
    )
    result = loop.run()

    assert result["status:reason"] == "error:serialization_error"
    assert executor.prepared == []


def test_symlinked_challenge_file_is_rejected_before_prepare(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("secret")
    (source / "link").symlink_to(outside)
    executor = PreparingExecutor([])
    loop = make_loop(
        tmp_path / "runs",
        ScriptedModel([ModelResponse(content="stop")]),
        executor=executor,
        challenge=ChallengeInput("files", "inspect files", source_dir=source),
    )

    result = loop.run()

    assert result["status:reason"] == "error:invalid_challenge_source"
    assert executor.prepared == []
    assert loop.artifacts.result_path.exists()


def test_special_file_challenge_source_is_rejected_before_prepare(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    pipe = source / "fifo"
    os.mkfifo(pipe)
    executor = PreparingExecutor([])
    loop = make_loop(
        tmp_path / "runs",
        ScriptedModel([ModelResponse(content="stop")]),
        executor=executor,
        challenge=ChallengeInput("files", "inspect files", source_dir=source),
    )

    result = loop.run()

    assert result["status:reason"] == "error:invalid_challenge_source"
    assert executor.prepared == []
    assert loop.artifacts.result_path.exists()


def _provider_response(content="ok"):
    return types.SimpleNamespace(
        choices=[
            types.SimpleNamespace(
                message=types.SimpleNamespace(content=content, tool_calls=None),
                finish_reason="stop",
            )
        ],
        usage=None,
    )


def test_chat_adapter_translates_system_message():
    calls = []
    client = types.SimpleNamespace(
        chat=types.SimpleNamespace(
            completions=types.SimpleNamespace(
                create=lambda **kwargs: calls.append(kwargs) or _provider_response()
            )
        )
    )
    ChatCompletionsModel("model", "key", client=client).generate(
        [{"role": "system", "content": "rules"}, {"role": "user", "content": "task"}],
        TOOL_DEFINITIONS,
    )

    assert calls[0]["messages"][0] == {"role": "system", "content": "rules"}


def test_responses_adapter_translates_system_message():
    calls = []
    client = types.SimpleNamespace(
        responses=types.SimpleNamespace(
            create=lambda **kwargs: (
                calls.append(kwargs)
                or types.SimpleNamespace(
                    output=[
                        {
                            "type": "message",
                            "id": "m",
                            "role": "assistant",
                            "status": "completed",
                            "content": [{"type": "output_text", "text": "ok"}],
                        }
                    ],
                    usage=None,
                )
            )
        )
    )
    ResponsesModel("model", "key", client=client).generate(
        [{"role": "system", "content": "rules"}, {"role": "user", "content": "task"}],
        TOOL_DEFINITIONS,
    )

    assert calls[0]["instructions"] == "rules"
    assert calls[0]["input"] == [{"role": "user", "content": "task"}]


def test_anthropic_adapter_translates_system_message():
    calls = []
    client = types.SimpleNamespace(
        messages=types.SimpleNamespace(
            create=lambda **kwargs: (
                calls.append(kwargs)
                or types.SimpleNamespace(
                    content=[types.SimpleNamespace(type="text", text="ok")],
                    usage=None,
                    stop_reason="end_turn",
                )
            )
        )
    )
    AnthropicMessagesModel("model", "key", client=client).generate(
        [{"role": "system", "content": "rules"}, {"role": "user", "content": "task"}],
        TOOL_DEFINITIONS,
    )

    assert calls[0]["system"] == "rules"
    assert calls[0]["messages"] == [{"role": "user", "content": "task"}]
