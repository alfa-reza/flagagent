import json
import types

import pytest

from flagagent.model import ModelResponse, ToolCall
from flagagent.providers import ChatCompletionsModel, ProviderError
from flagagent.tools import TOOL_DEFINITIONS


def _tool_call(call_id, name, arguments_json):
    """Provider-side tool call: arguments is the raw JSON string from the SDK."""
    return types.SimpleNamespace(
        id=call_id,
        type="function",
        function=types.SimpleNamespace(name=name, arguments=arguments_json),
    )


def _response(content=None, tool_calls=None, usage=None, finish_reason="stop"):
    message = types.SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = types.SimpleNamespace(message=message, finish_reason=finish_reason)
    return types.SimpleNamespace(choices=[choice], usage=usage)


class _FakeCompletions:
    def __init__(self, script):
        self._script = list(script)
        self._index = 0
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._index >= len(self._script):
            raise RuntimeError("fake completions exhausted")
        item = self._script[self._index]
        self._index += 1
        if isinstance(item, BaseException):
            raise item
        return item


def _model(script, model="test-model"):
    completions = _FakeCompletions(script)
    client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=completions))
    return (
        ChatCompletionsModel(model=model, api_key="sk-test", client=client),
        completions,
    )


def test_text_only_response_normalizes_content_and_usage():
    response = _response(
        content="hello model",
        usage=types.SimpleNamespace(
            prompt_tokens=42, completion_tokens=7, total_tokens=49
        ),
    )
    model, _ = _model([response])

    result = model.generate([{"role": "user", "content": "hi"}], TOOL_DEFINITIONS)

    assert isinstance(result, ModelResponse)
    assert result.content == "hello model"
    assert result.tool_calls == ()
    assert result.usage == {"input_tokens": 42, "output_tokens": 7}


def test_multiple_ordered_tool_calls_preserve_order():
    response = _response(
        tool_calls=[
            _tool_call("call-1", "shell", json.dumps({"command": "ls"})),
            _tool_call("call-2", "submit_flag", json.dumps({"candidate": "Flag{x}"})),
        ],
        usage=types.SimpleNamespace(prompt_tokens=5, completion_tokens=3),
    )
    model, _ = _model([response])

    result = model.generate([{"role": "user", "content": "go"}], TOOL_DEFINITIONS)

    assert result.content == ""
    assert len(result.tool_calls) == 2
    assert [c.call_id for c in result.tool_calls] == ["call-1", "call-2"]
    assert [c.name for c in result.tool_calls] == ["shell", "submit_flag"]
    assert all(isinstance(c, ToolCall) for c in result.tool_calls)
    assert result.tool_calls[0].arguments == {"command": "ls"}
    assert result.tool_calls[1].arguments == {"candidate": "Flag{x}"}


def test_request_tool_and_message_conversion():
    messages = [
        {"role": "user", "content": "solve it", "challenge_identity": "fixture"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"call_id": "c1", "name": "shell", "arguments": {"command": "ls"}},
                {
                    "call_id": "c2",
                    "name": "submit_flag",
                    "arguments": {"candidate": "Flag{x}"},
                },
            ],
        },
    ]
    model, completions = _model([_response(content="ok")])

    model.generate(messages, TOOL_DEFINITIONS)

    call = completions.calls[0]
    assert call["model"] == "test-model"
    assert call["tools"] == [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["parameters"],
            },
        }
        for t in TOOL_DEFINITIONS
    ]
    assert call["messages"] == [
        {"role": "user", "content": "solve it"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {
                        "name": "shell",
                        "arguments": json.dumps({"command": "ls"}),
                    },
                },
                {
                    "id": "c2",
                    "type": "function",
                    "function": {
                        "name": "submit_flag",
                        "arguments": json.dumps({"candidate": "Flag{x}"}),
                    },
                },
            ],
        },
    ]


def test_correlated_follow_up_tool_results():
    shell_result = {
        "stdout": "out",
        "stderr": "",
        "exit_code": 0,
        "timed_out": False,
        "truncated": False,
    }
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"call_id": "c1", "name": "shell", "arguments": {"command": "ls"}},
                {
                    "call_id": "c2",
                    "name": "submit_flag",
                    "arguments": {"candidate": "Flag{x}"},
                },
            ],
        },
        {"role": "tool", "call_id": "c1", "name": "shell", "result": shell_result},
        {
            "role": "tool",
            "call_id": "c2",
            "name": "submit_flag",
            "result": {"outcome": "incorrect"},
        },
    ]
    model, completions = _model([_response(content="done")])

    model.generate(messages, TOOL_DEFINITIONS)

    sent = completions.calls[0]["messages"]
    assistant = sent[0]
    assert assistant["tool_calls"][0]["id"] == "c1"
    assert assistant["tool_calls"][1]["id"] == "c2"
    tool_msgs = [m for m in sent if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_msgs] == ["c1", "c2"]
    assert json.loads(tool_msgs[0]["content"]) == shell_result
    assert json.loads(tool_msgs[1]["content"]) == {"outcome": "incorrect"}


def test_malformed_json_arguments_raise_provider_error():
    response = _response(tool_calls=[_tool_call("call-1", "shell", "{not valid json")])
    model, _ = _model([response])

    with pytest.raises(ProviderError, match="not valid JSON"):
        model.generate([{"role": "user", "content": "go"}], TOOL_DEFINITIONS)


def test_missing_usage_yields_null_usage():
    response = _response(content="stop", usage=None)
    model, _ = _model([response])

    result = model.generate([{"role": "user", "content": "hi"}], TOOL_DEFINITIONS)

    assert result.usage is None


def test_sdk_failure_raises_provider_error_without_fabricating_calls():
    model, _ = _model([ConnectionError("upstream down")])

    with pytest.raises(ProviderError, match="chat completions request failed"):
        model.generate([{"role": "user", "content": "hi"}], TOOL_DEFINITIONS)


def test_openrouter_base_url_uses_openai_compatible_path(monkeypatch):
    built = {}

    class _RecordingOpenAI:
        def __init__(self, **kwargs):
            built.update(kwargs)

    monkeypatch.setattr("flagagent.providers.OpenAI", _RecordingOpenAI)

    adapter = ChatCompletionsModel(
        model="openai/gpt-4o-mini",
        api_key="sk-or-test",
        base_url="https://openrouter.ai/api/v1",
    )

    assert adapter.model == "openai/gpt-4o-mini"
    assert built == {
        "api_key": "sk-or-test",
        "base_url": "https://openrouter.ai/api/v1",
    }
    assert isinstance(adapter.client, _RecordingOpenAI)


def test_set_remaining_bounds_request_timeout_and_disables_retries():
    response = _response(content="ok")
    model, completions = _model([response])
    model.set_remaining(12.5)

    model.generate([{"role": "user", "content": "hi"}], TOOL_DEFINITIONS)

    call = completions.calls[0]
    assert call["timeout"] == 12.5
    assert call["max_retries"] == 0


def test_exhausted_budget_does_not_call_provider():
    model, completions = _model([_response(content="never")])
    model.set_remaining(0)

    with pytest.raises(ProviderError, match="budget exhausted"):
        model.generate([{"role": "user", "content": "hi"}], TOOL_DEFINITIONS)

    assert completions.calls == []


def test_default_retries_left_in_place_without_set_remaining():
    response = _response(content="ok")
    model, completions = _model([response])

    model.generate([{"role": "user", "content": "hi"}], TOOL_DEFINITIONS)

    call = completions.calls[0]
    assert "timeout" not in call
    assert "max_retries" not in call


def test_missing_choices_raises_provider_error():
    model, _ = _model([types.SimpleNamespace(choices=None, usage=None)])

    with pytest.raises(ProviderError, match="no choices"):
        model.generate([{"role": "user", "content": "hi"}], TOOL_DEFINITIONS)


def test_invalid_choices_raises_provider_error():
    model, _ = _model([types.SimpleNamespace(choices="not-a-list", usage=None)])

    with pytest.raises(ProviderError, match="no choices"):
        model.generate([{"role": "user", "content": "hi"}], TOOL_DEFINITIONS)


def test_missing_message_raises_provider_error():
    response = types.SimpleNamespace(
        choices=[types.SimpleNamespace(message=None, finish_reason="stop")],
        usage=None,
    )
    model, _ = _model([response])

    with pytest.raises(ProviderError, match="missing message"):
        model.generate([{"role": "user", "content": "hi"}], TOOL_DEFINITIONS)


def test_missing_tool_call_id_raises_provider_error():
    response = _response(tool_calls=[_tool_call(None, "shell", "{}")])
    model, _ = _model([response])

    with pytest.raises(ProviderError, match="missing id"):
        model.generate([{"role": "user", "content": "hi"}], TOOL_DEFINITIONS)


def test_missing_tool_call_name_raises_provider_error():
    response = _response(tool_calls=[_tool_call("c1", None, "{}")])
    model, _ = _model([response])

    with pytest.raises(ProviderError, match="missing function name"):
        model.generate([{"role": "user", "content": "hi"}], TOOL_DEFINITIONS)


def test_missing_tool_call_arguments_raises_provider_error():
    response = _response(tool_calls=[_tool_call("c1", "shell", None)])
    model, _ = _model([response])

    with pytest.raises(ProviderError, match="arguments missing"):
        model.generate([{"role": "user", "content": "hi"}], TOOL_DEFINITIONS)


def test_non_object_arguments_raise_provider_error():
    response = _response(tool_calls=[_tool_call("c1", "shell", "[1, 2, 3]")])
    model, _ = _model([response])

    with pytest.raises(ProviderError, match="must be a JSON object"):
        model.generate([{"role": "user", "content": "hi"}], TOOL_DEFINITIONS)


def test_nan_arguments_raise_provider_error_for_strict_json():
    response = _response(tool_calls=[_tool_call("c1", "shell", '{"command": NaN}')])
    model, _ = _model([response])

    with pytest.raises(ProviderError, match="strict JSON"):
        model.generate([{"role": "user", "content": "hi"}], TOOL_DEFINITIONS)


def test_finish_reason_stop_is_normal_text_response():
    response = _response(content="done", finish_reason="stop")
    model, _ = _model([response])

    result = model.generate([{"role": "user", "content": "hi"}], TOOL_DEFINITIONS)

    assert isinstance(result, ModelResponse)
    assert result.content == "done"
    assert result.tool_calls == ()


def test_finish_reason_tool_calls_is_normal_tool_call_response():
    response = _response(
        tool_calls=[_tool_call("c1", "shell", json.dumps({"command": "ls"}))],
        finish_reason="tool_calls",
    )
    model, _ = _model([response])

    result = model.generate([{"role": "user", "content": "go"}], TOOL_DEFINITIONS)

    assert result.content == ""
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].call_id == "c1"


def test_finish_reason_length_raises_provider_error():
    response = _response(content="partial", finish_reason="length")
    model, _ = _model([response])

    with pytest.raises(ProviderError, match="finish reason is not normal"):
        model.generate([{"role": "user", "content": "hi"}], TOOL_DEFINITIONS)


def test_finish_reason_content_filter_raises_provider_error():
    response = _response(content="", finish_reason="content_filter")
    model, _ = _model([response])

    with pytest.raises(ProviderError, match="finish reason is not normal"):
        model.generate([{"role": "user", "content": "hi"}], TOOL_DEFINITIONS)
