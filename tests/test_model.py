from copy import deepcopy

import pytest

from flagagent.model import ModelResponse, ScriptedModel, ToolCall
from flagagent.tools import (
    ExactStringVerifier,
    ShellResult,
    validate_tool_arguments,
)


def test_model_response_snapshots_mutable_values():
    arguments = {"command": "true"}
    usage = {"input_tokens": 1}
    response = ModelResponse(
        content="working",
        tool_calls=(ToolCall("call-1", "shell", arguments),),
        usage=usage,
    )

    arguments["command"] = "false"
    usage["input_tokens"] = 99

    assert response.tool_calls[0].arguments == {"command": "true"}
    assert response.usage == {"input_tokens": 1}


@pytest.mark.parametrize(
    ("call_id", "name", "arguments"),
    [
        ("", "shell", {"command": "true"}),
        ("call-1", "", {"command": "true"}),
        ("call-1", "shell", []),
        ("call-1", "shell", {"bad": object()}),
    ],
)
def test_tool_call_rejects_malformed_normalized_values(call_id, name, arguments):
    with pytest.raises((TypeError, ValueError)):
        ToolCall(call_id, name, arguments)


def test_scripted_model_records_snapshot_and_raises_scripted_error():
    error = RuntimeError("provider failed")
    model = ScriptedModel([ModelResponse(content="first"), error])
    messages = [{"role": "user", "content": "task"}]
    tools = [{"name": "shell"}]

    assert model.generate(messages, tools).content == "first"
    messages[0]["content"] = "changed"
    tools[0]["name"] = "changed"
    with pytest.raises(RuntimeError, match="provider failed"):
        model.generate(messages, tools)

    assert model.calls[0] == (
        [{"role": "user", "content": "task"}],
        [{"name": "shell"}],
    )


def test_tool_argument_validation_is_exact_and_strict():
    assert validate_tool_arguments("shell", {"command": "printf ok"}) == {
        "command": "printf ok"
    }
    assert validate_tool_arguments("submit_flag", {"candidate": " Flag{x} "}) == {
        "candidate": " Flag{x} "
    }

    for name, arguments in [
        ("shell", {}),
        ("shell", {"command": "  "}),
        ("shell", {"command": "true", "cwd": "/"}),
        ("submit_flag", {"candidate": 1}),
    ]:
        with pytest.raises(ValueError):
            validate_tool_arguments(name, arguments)


@pytest.mark.parametrize(
    "result",
    [
        ShellResult("", "", 1, False, False),
        ShellResult("partial", "", None, True, False),
    ],
)
def test_shell_result_accepts_nonzero_and_timeout_evidence(result):
    assert result.exit_code in {1, None}


@pytest.mark.parametrize(
    ("exit_code", "timed_out"),
    [(None, False), (0, True), (True, False)],
)
def test_shell_result_rejects_invalid_timeout_shapes(exit_code, timed_out):
    with pytest.raises((TypeError, ValueError)):
        ShellResult("", "", exit_code, timed_out, False)


def test_exact_verifier_strips_candidate_and_is_case_sensitive():
    verifier = ExactStringVerifier("Flag{Example}")

    assert verifier.check("  Flag{Example}\n") == "correct"
    assert verifier.check("flag{Example}") == "incorrect"
    assert deepcopy(verifier).check("Flag{Example}") == "correct"
