"""Tests for the Anthropic Messages model adapter."""

import json
import types

import pytest

from flagagent.anthropic_messages import (
    ANTHROPIC_MAX_TOKENS,
    AnthropicMessagesModel,
    ProviderError,
)
from flagagent.model import ModelResponse, ToolCall
from flagagent.tools import TOOL_DEFINITIONS


class _FakeMessages:
    """Records create kwargs and returns scripted responses."""

    def __init__(self, script):
        self._script = list(script)
        self._index = 0
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._index >= len(self._script):
            raise RuntimeError("fake messages exhausted")
        item = self._script[self._index]
        self._index += 1
        if isinstance(item, BaseException):
            raise item
        return item


class _WithOptionsClient:
    """Wraps a _FakeMessages to simulate the official SDK with_options."""

    def __init__(self, messages):
        self.messages = messages
        self.with_options_calls = []

    def with_options(self, **kwargs):
        self.with_options_calls.append(kwargs)
        return self


def _text_block(text):
    return types.SimpleNamespace(type="text", text=text)


def _tool_use_block(call_id, name, input_dict):
    return types.SimpleNamespace(
        type="tool_use", id=call_id, name=name, input=input_dict
    )


def _thinking_block(thinking, signature):
    return types.SimpleNamespace(
        type="thinking", thinking=thinking, signature=signature
    )


def _redacted_block(data):
    return types.SimpleNamespace(type="redacted_thinking", data=data)


def _response(content=None, usage=None, stop_reason="end_turn"):
    return types.SimpleNamespace(
        content=content or [], usage=usage, stop_reason=stop_reason
    )


def _model(script, model="test-model"):
    messages = _FakeMessages(script)
    client = types.SimpleNamespace(messages=messages)
    return (
        AnthropicMessagesModel(model=model, api_key="sk-test", client=client),
        messages,
    )


def _model_with_options(script, model="test-model"):
    messages = _FakeMessages(script)
    client = _WithOptionsClient(messages)
    return (
        AnthropicMessagesModel(model=model, api_key="sk-test", client=client),
        messages,
        client,
    )


# --- Anthropic tool schema ---


def test_anthropic_tool_schema():
    model, messages = _model([_response(content=[_text_block("ok")])])

    model.generate([{"role": "user", "content": "go"}], TOOL_DEFINITIONS)

    call = messages.calls[0]
    assert call["model"] == "test-model"
    assert call["tools"] == [
        {
            "name": t["name"],
            "description": t["description"],
            "input_schema": t["parameters"],
        }
        for t in TOOL_DEFINITIONS
    ]


# --- text-only response ---


def test_text_only_response_normalizes_content_and_usage():
    response = _response(
        content=[_text_block("hello model")],
        usage=types.SimpleNamespace(input_tokens=42, output_tokens=7),
    )
    model, _ = _model([response])

    result = model.generate([{"role": "user", "content": "hi"}], TOOL_DEFINITIONS)

    assert isinstance(result, ModelResponse)
    assert result.content == "hello model"
    assert result.tool_calls == ()
    assert result.usage == {"input_tokens": 42, "output_tokens": 7}


# --- ordered tool_use normalization ---


def test_ordered_tool_use_normalization():
    response = _response(
        content=[
            _tool_use_block("call-1", "shell", {"command": "ls"}),
            _tool_use_block("call-2", "submit_flag", {"candidate": "Flag{x}"}),
        ],
        usage=types.SimpleNamespace(input_tokens=5, output_tokens=3),
        stop_reason="tool_use",
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


# --- assistant block order ---


def test_assistant_block_order_text_before_tool_use():
    messages = [
        {
            "role": "assistant",
            "content": "thinking...",
            "tool_calls": [
                {"call_id": "c1", "name": "shell", "arguments": {"command": "ls"}},
            ],
        },
    ]
    model, msgs = _model([_response(content=[_text_block("ok")])])

    model.generate(messages, TOOL_DEFINITIONS)

    sent = msgs.calls[0]["messages"]
    assistant = sent[0]
    assert assistant["role"] == "assistant"
    blocks = assistant["content"]
    assert blocks[0] == {"type": "text", "text": "thinking..."}
    assert blocks[1] == {
        "type": "tool_use",
        "id": "c1",
        "name": "shell",
        "input": {"command": "ls"},
    }


def test_assistant_without_content_has_only_tool_use_blocks():
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"call_id": "c1", "name": "shell", "arguments": {"command": "ls"}},
            ],
        },
    ]
    model, msgs = _model([_response(content=[_text_block("ok")])])

    model.generate(messages, TOOL_DEFINITIONS)

    blocks = msgs.calls[0]["messages"][0]["content"]
    assert len(blocks) == 1
    assert blocks[0]["type"] == "tool_use"


# --- consecutive tool-result grouping ---


def test_consecutive_tool_results_grouped_into_one_user_message():
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
    model, msgs = _model([_response(content=[_text_block("done")])])

    model.generate(messages, TOOL_DEFINITIONS)

    sent = msgs.calls[0]["messages"]
    assert sent[0]["role"] == "assistant"
    assert sent[1]["role"] == "user"
    tool_results = sent[1]["content"]
    assert len(tool_results) == 2
    assert all(b["type"] == "tool_result" for b in tool_results)
    assert [b["tool_use_id"] for b in tool_results] == ["c1", "c2"]
    assert json.loads(tool_results[0]["content"]) == shell_result
    assert json.loads(tool_results[1]["content"]) == {"outcome": "incorrect"}
    assert len(sent) == 2


# --- unknown/invalid result serialization ---


def test_non_serializable_tool_result_falls_back_to_str():
    class Custom:
        def __str__(self):
            return "<custom>"

    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"call_id": "c1", "name": "shell", "arguments": {"command": "ls"}},
            ],
        },
        {"role": "tool", "call_id": "c1", "name": "shell", "result": Custom()},
    ]
    model, msgs = _model([_response(content=[_text_block("ok")])])

    model.generate(messages, TOOL_DEFINITIONS)

    tool_result = msgs.calls[0]["messages"][1]["content"][0]
    assert tool_result["content"] == "<custom>"


# --- usage ---


def test_missing_usage_yields_null_usage():
    response = _response(content=[_text_block("stop")], usage=None)
    model, _ = _model([response])

    result = model.generate([{"role": "user", "content": "hi"}], TOOL_DEFINITIONS)

    assert result.usage is None


# --- fixed max_tokens ---


def test_fixed_max_tokens():
    model, messages = _model([_response(content=[_text_block("ok")])])

    model.generate([{"role": "user", "content": "hi"}], TOOL_DEFINITIONS)

    assert messages.calls[0]["max_tokens"] == ANTHROPIC_MAX_TOKENS


# --- SDK error ---


def test_sdk_error_raises_provider_error():
    model, _ = _model([ConnectionError("upstream down")])

    with pytest.raises(ProviderError, match="messages request failed"):
        model.generate([{"role": "user", "content": "hi"}], TOOL_DEFINITIONS)


# --- malformed response ---


@pytest.mark.parametrize(
    "content",
    [
        None,
        [],
        "not-a-list",
        [types.SimpleNamespace(type="unknown")],
        [types.SimpleNamespace(type="text", text=1)],
        [types.SimpleNamespace(type="text")],
        [types.SimpleNamespace(type="tool_use", id="", name="shell", input={})],
        [types.SimpleNamespace(type="tool_use", id="c1", name="", input={})],
        [
            types.SimpleNamespace(
                type="tool_use", id="c1", name="shell", input="not-a-dict"
            )
        ],
        [types.SimpleNamespace(type="tool_use", id="c1", name="shell")],
    ],
)
def test_malformed_response_raises_provider_error(content):
    response = types.SimpleNamespace(content=content, usage=None, stop_reason="end_turn")
    model, _ = _model([response])

    with pytest.raises(ProviderError):
        model.generate([{"role": "user", "content": "hi"}], TOOL_DEFINITIONS)


# --- response block ordering ---


def test_text_before_tool_use_response_is_valid():
    response = _response(
        content=[
            _text_block("thinking"),
            _tool_use_block("c1", "shell", {"command": "ls"}),
        ],
        stop_reason="tool_use",
    )
    model, _ = _model([response])

    result = model.generate([{"role": "user", "content": "go"}], TOOL_DEFINITIONS)

    assert result.content == "thinking"
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].call_id == "c1"


def test_text_after_tool_use_raises_provider_error():
    response = _response(
        content=[
            _tool_use_block("c1", "shell", {"command": "ls"}),
            _text_block("after"),
        ],
        stop_reason="tool_use",
    )
    model, _ = _model([response])

    with pytest.raises(ProviderError, match="text block after tool use"):
        model.generate([{"role": "user", "content": "go"}], TOOL_DEFINITIONS)


def test_text_between_tool_uses_raises_provider_error():
    response = _response(
        content=[
            _text_block("before"),
            _tool_use_block("c1", "shell", {"command": "ls"}),
            _text_block("after"),
        ],
        stop_reason="tool_use",
    )
    model, _ = _model([response])

    with pytest.raises(ProviderError, match="text block after tool use"):
        model.generate([{"role": "user", "content": "go"}], TOOL_DEFINITIONS)


# --- positive timeout/retry configuration ---


def test_positive_timeout_uses_with_options_for_official_client():
    model, messages, client = _model_with_options(
        [_response(content=[_text_block("ok")])]
    )
    model.set_remaining(12.5)

    model.generate([{"role": "user", "content": "hi"}], TOOL_DEFINITIONS)

    assert client.with_options_calls == [{"timeout": 12.5, "max_retries": 0}]
    assert "timeout" not in messages.calls[0]
    assert "max_retries" not in messages.calls[0]


def test_positive_timeout_via_fallback_for_test_double():
    model, messages = _model([_response(content=[_text_block("ok")])])
    model.set_remaining(12.5)

    model.generate([{"role": "user", "content": "hi"}], TOOL_DEFINITIONS)

    assert messages.calls[0]["timeout"] == 12.5
    assert messages.calls[0]["max_retries"] == 0


# --- exhausted/nonfinite budget ---


def test_exhausted_budget_raises_before_sdk_call():
    model, messages = _model([_response(content=[_text_block("never")])])
    model.set_remaining(0)

    with pytest.raises(ProviderError, match="budget exhausted"):
        model.generate([{"role": "user", "content": "hi"}], TOOL_DEFINITIONS)

    assert messages.calls == []


def test_negative_budget_raises_before_sdk_call():
    model, messages = _model([_response(content=[_text_block("never")])])
    model.set_remaining(-5.0)

    with pytest.raises(ProviderError, match="budget exhausted"):
        model.generate([{"role": "user", "content": "hi"}], TOOL_DEFINITIONS)

    assert messages.calls == []


@pytest.mark.parametrize("budget", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_budget_rejected(budget):
    model, messages = _model([_response(content=[_text_block("never")])])

    with pytest.raises(ValueError, match="finite"):
        model.set_remaining(budget)

    assert messages.calls == []


def test_non_number_budget_rejected():
    model, _ = _model([_response(content=[_text_block("never")])])

    with pytest.raises(TypeError, match="number"):
        model.set_remaining("not-a-number")


def test_bool_budget_rejected():
    model, _ = _model([_response(content=[_text_block("never")])])

    with pytest.raises(TypeError, match="number"):
        model.set_remaining(True)


# --- default behavior ---


def test_default_behavior_no_timeout_or_retries():
    model, messages = _model([_response(content=[_text_block("ok")])])

    model.generate([{"role": "user", "content": "hi"}], TOOL_DEFINITIONS)

    assert "timeout" not in messages.calls[0]
    assert "max_retries" not in messages.calls[0]


def test_user_message_strips_extra_fields():
    messages = [
        {"role": "user", "content": "solve it", "challenge_identity": "fixture"},
    ]
    model, msgs = _model([_response(content=[_text_block("ok")])])

    model.generate(messages, TOOL_DEFINITIONS)

    assert msgs.calls[0]["messages"] == [{"role": "user", "content": "solve it"}]


def test_end_turn_stop_reason_returns_normal_response():
    response = _response(content=[_text_block("done")], stop_reason="end_turn")
    model, _ = _model([response])

    result = model.generate([{"role": "user", "content": "hi"}], TOOL_DEFINITIONS)

    assert isinstance(result, ModelResponse)
    assert result.content == "done"
    assert result.tool_calls == ()


def test_tool_use_stop_reason_returns_tool_calls():
    response = _response(
        content=[_tool_use_block("c1", "shell", {"command": "ls"})],
        stop_reason="tool_use",
    )
    model, _ = _model([response])

    result = model.generate([{"role": "user", "content": "go"}], TOOL_DEFINITIONS)

    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].call_id == "c1"
    assert result.tool_calls[0].name == "shell"


def test_max_tokens_stop_reason_raises_provider_error():
    response = _response(content=[_text_block("partial")], stop_reason="max_tokens")
    model, _ = _model([response])

    with pytest.raises(ProviderError, match="non-normal stop reason"):
        model.generate([{"role": "user", "content": "hi"}], TOOL_DEFINITIONS)


@pytest.mark.parametrize("stop_reason", [None, "stop_sequence"])
def test_non_normal_stop_reason_raises_provider_error(stop_reason):
    response = _response(content=[_text_block("partial")], stop_reason=stop_reason)
    model, _ = _model([response])

    with pytest.raises(ProviderError, match="non-normal stop reason"):
        model.generate([{"role": "user", "content": "hi"}], TOOL_DEFINITIONS)


def test_missing_stop_reason_raises_provider_error():
    response = types.SimpleNamespace(
        content=[_text_block("partial")], usage=None
    )
    model, _ = _model([response])

    with pytest.raises(ProviderError, match="non-normal stop reason"):
        model.generate([{"role": "user", "content": "hi"}], TOOL_DEFINITIONS)


def test_thinking_plus_text_does_not_raise_and_content_is_only_text():
    response = _response(
        content=[_thinking_block("internal reasoning", "sig-abc"), _text_block("hello")]
    )
    model, _ = _model([response])

    result = model.generate([{"role": "user", "content": "hi"}], TOOL_DEFINITIONS)

    assert result.content == "hello"
    assert result.tool_calls == ()
    assert "internal reasoning" not in result.content
    assert "sig-abc" not in result.content
    assert "thinking" not in result.to_dict().values()
    dumped = json.dumps(result.to_dict())
    assert "sig-abc" not in dumped
    assert "internal reasoning" not in dumped


def test_thinking_plus_tool_use_replayed_on_next_turn():
    thinking = "analyze files"
    signature = "sig-xyz-123"
    first = _response(
        content=[
            _thinking_block(thinking, signature),
            _tool_use_block("c1", "shell", {"command": "ls"}),
        ],
        stop_reason="tool_use",
    )
    second = _response(content=[_text_block("done")])
    model, msgs = _model([first, second])

    result1 = model.generate([{"role": "user", "content": "go"}], TOOL_DEFINITIONS)
    assert len(result1.tool_calls) == 1

    history = [
        {"role": "user", "content": "go"},
        {
            "role": "assistant",
            "content": result1.content,
            "tool_calls": [
                {"call_id": c.call_id, "name": c.name, "arguments": c.arguments}
                for c in result1.tool_calls
            ],
        },
        {
            "role": "tool",
            "call_id": "c1",
            "name": "shell",
            "result": {
                "stdout": "out",
                "stderr": "",
                "exit_code": 0,
                "timed_out": False,
                "truncated": False,
            },
        },
    ]
    result2 = model.generate(history, TOOL_DEFINITIONS)

    assert result2.content == "done"
    sent = msgs.calls[1]["messages"]
    assert sent[0] == {"role": "user", "content": "go"}
    assistant = sent[1]
    assert assistant["role"] == "assistant"
    blocks = assistant["content"]
    assert blocks[0] == {"type": "thinking", "thinking": thinking, "signature": signature}
    assert blocks[1] == {
        "type": "tool_use",
        "id": "c1",
        "name": "shell",
        "input": {"command": "ls"},
    }
    assert blocks[0]["type"] == "thinking"
    assert blocks[1]["type"] == "tool_use"
    tool_user = sent[2]
    assert tool_user["role"] == "user"
    assert tool_user["content"][0]["type"] == "tool_result"
    assert tool_user["content"][0]["tool_use_id"] == "c1"
    assert json.loads(tool_user["content"][0]["content"]) == {
        "stdout": "out",
        "stderr": "",
        "exit_code": 0,
        "timed_out": False,
        "truncated": False,
    }


def test_redacted_thinking_accepted_and_replayed_unchanged():
    data = "opaque-encrypted-data=="
    first = _response(
        content=[
            _redacted_block(data),
            _tool_use_block("c2", "shell", {"command": "id"}),
        ],
        stop_reason="tool_use",
    )
    second = _response(content=[_text_block("done")])
    model, msgs = _model([first, second])

    result1 = model.generate([{"role": "user", "content": "go"}], TOOL_DEFINITIONS)
    assert result1.content == ""
    assert "opaque" not in json.dumps(result1.to_dict())

    history = [
        {"role": "user", "content": "go"},
        {
            "role": "assistant",
            "content": result1.content,
            "tool_calls": [
                {"call_id": c.call_id, "name": c.name, "arguments": c.arguments}
                for c in result1.tool_calls
            ],
        },
        {
            "role": "tool",
            "call_id": "c2",
            "name": "shell",
            "result": {"stdout": "uid=0", "stderr": "", "exit_code": 0, "timed_out": False, "truncated": False},
        },
    ]
    model.generate(history, TOOL_DEFINITIONS)

    assistant = msgs.calls[1]["messages"][1]
    assert assistant["content"][0] == {"type": "redacted_thinking", "data": data}
    assert assistant["content"][1]["type"] == "tool_use"


def test_thinking_text_tool_use_replay_preserves_order_and_text():
    thinking = "step by step"
    signature = "sig-order"
    first = _response(
        content=[
            _thinking_block(thinking, signature),
            _text_block("progress"),
            _tool_use_block("c3", "shell", {"command": "ls"}),
        ],
        stop_reason="tool_use",
    )
    second = _response(content=[_text_block("done")])
    model, msgs = _model([first, second])

    result1 = model.generate([{"role": "user", "content": "go"}], TOOL_DEFINITIONS)
    assert result1.content == "progress"

    history = [
        {"role": "user", "content": "go"},
        {
            "role": "assistant",
            "content": result1.content,
            "tool_calls": [
                {"call_id": c.call_id, "name": c.name, "arguments": c.arguments}
                for c in result1.tool_calls
            ],
        },
        {"role": "tool", "call_id": "c3", "name": "shell", "result": "ok"},
    ]
    model.generate(history, TOOL_DEFINITIONS)

    blocks = msgs.calls[1]["messages"][1]["content"]
    assert blocks[0] == {"type": "thinking", "thinking": thinking, "signature": signature}
    assert blocks[1] == {"type": "text", "text": "progress"}
    assert blocks[2]["type"] == "tool_use"


def test_tool_use_stop_reason_without_tool_use_raises_provider_error():
    response = _response(content=[_text_block("hello")], stop_reason="tool_use")
    model, _ = _model([response])

    with pytest.raises(ProviderError):
        model.generate([{"role": "user", "content": "hi"}], TOOL_DEFINITIONS)


def test_end_turn_with_tool_use_raises_provider_error():
    response = _response(
        content=[_tool_use_block("c1", "shell", {"command": "ls"})],
        stop_reason="end_turn",
    )
    model, _ = _model([response])

    with pytest.raises(ProviderError):
        model.generate([{"role": "user", "content": "hi"}], TOOL_DEFINITIONS)
