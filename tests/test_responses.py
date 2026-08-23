import json
import types

import pytest

from flagagent.model import ModelResponse, ToolCall
from flagagent.responses import ProviderError, ResponsesModel
from flagagent.tools import TOOL_DEFINITIONS


def message_item(text):
    return {
        "type": "message",
        "id": "msg_1",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text}],
    }


def function_call_item(call_id, name, arguments):
    return {
        "type": "function_call",
        "call_id": call_id,
        "name": name,
        "arguments": arguments,
    }


def reasoning_item(item_id="rs_1"):
    return {
        "type": "reasoning",
        "id": item_id,
        "summary": [{"type": "summary_text", "text": "thinking"}],
        "encrypted_content": "encrypted-reasoning",
    }


def response(output, usage=None):
    return types.SimpleNamespace(output=output, usage=usage)


class FakeResponses:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        item = self.script[len(self.calls) - 1]
        if isinstance(item, BaseException):
            raise item
        return item


def make_model(script, model="test-model"):
    responses = FakeResponses(script)
    client = types.SimpleNamespace(responses=responses)
    return ResponsesModel(model, "sk-test", client=client), responses


def generate(model, messages=None):
    return model.generate(
        messages or [{"role": "user", "content": "solve"}], TOOL_DEFINITIONS
    )


def test_content_and_usage_normalize():
    model, _ = make_model(
        [
            response(
                [message_item("hello")],
                types.SimpleNamespace(input_tokens=4, output_tokens=2),
            )
        ]
    )

    result = generate(model)

    assert isinstance(result, ModelResponse)
    assert result.content == "hello"
    assert result.tool_calls == ()
    assert result.usage == {"input_tokens": 4, "output_tokens": 2}


def test_ordered_function_calls_normalize():
    model, _ = make_model(
        [
            response(
                [
                    function_call_item("c1", "shell", '{"command":"ls"}'),
                    function_call_item("c2", "submit_flag", '{"candidate":"Flag{x}"}'),
                ]
            )
        ]
    )

    result = generate(model)

    assert [call.call_id for call in result.tool_calls] == ["c1", "c2"]
    assert [call.name for call in result.tool_calls] == ["shell", "submit_flag"]
    assert all(isinstance(call, ToolCall) for call in result.tool_calls)
    assert result.tool_calls[0].arguments == {"command": "ls"}


def test_tool_outputs_replay_with_correlated_call_ids_and_reasoning():
    first = response(
        [
            reasoning_item("reason-1"),
            function_call_item("c1", "shell", '{"command":"ls"}'),
        ]
    )
    model, responses = make_model([first, response([message_item("done")])])
    first_messages = [{"role": "user", "content": "solve"}]
    generate(model, first_messages)
    second_messages = first_messages + [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"call_id": "c1", "name": "shell", "arguments": {"command": "ls"}}
            ],
        },
        {
            "role": "tool",
            "call_id": "c1",
            "name": "shell",
            "result": {"stdout": "out", "exit_code": 0},
        },
    ]

    result = generate(model, second_messages)

    assert result.content == "done"
    second_input = responses.calls[1]["input"]
    reasoning_items = [item for item in second_input if item.get("type") == "reasoning"]
    assert [item["id"] for item in reasoning_items] == ["reason-1"]
    assert reasoning_items[0]["encrypted_content"] == "encrypted-reasoning"
    assert [
        item["call_id"] for item in second_input if item.get("type") == "function_call"
    ] == ["c1"]
    outputs = [
        item for item in second_input if item.get("type") == "function_call_output"
    ]
    assert outputs[0]["call_id"] == "c1"
    assert json.loads(outputs[0]["output"]) == {"stdout": "out", "exit_code": 0}


def test_requests_are_stateless_and_translate_tools():
    model, responses = make_model([response([message_item("ok")])])

    generate(model)

    request = responses.calls[0]
    assert request["model"] == "test-model"
    assert request["store"] is False
    assert "previous_response_id" not in request
    assert request["include"] == ["reasoning.encrypted_content"]
    assert request["tools"] == [
        {
            "type": "function",
            "name": tool["name"],
            "description": tool["description"],
            "parameters": tool["parameters"],
            "strict": True,
        }
        for tool in TOOL_DEFINITIONS
    ]


def test_missing_usage_is_not_fabricated():
    model, _ = make_model([response([message_item("ok")])])

    assert generate(model).usage is None


@pytest.mark.parametrize(
    "output",
    [
        None,
        [],
        "not-a-list",
        [{"type": "unknown"}],
        [function_call_item(None, "shell", "{}")],
        [function_call_item("c1", None, "{}")],
        [function_call_item("c1", "shell", None)],
        [function_call_item("c1", "shell", "bad")],
        [function_call_item("c1", "shell", "[]")],
        [function_call_item("c1", "shell", '{"command":NaN}')],
        [{"type": "message", "content": 1}],
        [
            {
                "type": "message",
                "id": "m",
                "role": "user",
                "status": "completed",
                "content": [],
            }
        ],
        [
            {
                "type": "message",
                "id": "m",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": 1}],
            }
        ],
        [{"type": "reasoning", "summary": []}],
        [{"type": "reasoning", "id": "r", "summary": [{}]}],
    ],
)
def test_malformed_output_raises_provider_error(output):
    model, _ = make_model([response(output)])

    with pytest.raises(ProviderError):
        generate(model)


def test_sdk_failure_raises_provider_error():
    model, _ = make_model([ConnectionError("secret response")])

    with pytest.raises(ProviderError, match="responses request failed"):
        generate(model)


def test_run_budget_sets_timeout_and_disables_retries():
    model, responses = make_model([response([message_item("ok")])])
    model.set_remaining(12.5)

    generate(model)

    assert responses.calls[0]["timeout"] == 12.5
    assert responses.calls[0]["max_retries"] == 0


def test_exhausted_budget_does_not_call_provider():
    model, responses = make_model([response([message_item("never")])])
    model.set_remaining(0)

    with pytest.raises(ProviderError, match="budget exhausted"):
        generate(model)

    assert responses.calls == []


@pytest.mark.parametrize("budget", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_budget_is_rejected(budget):
    model, responses = make_model([response([message_item("never")])])

    with pytest.raises(ValueError, match="finite"):
        model.set_remaining(budget)

    assert responses.calls == []


def test_default_request_does_not_override_sdk_retries():
    model, responses = make_model([response([message_item("ok")])])

    generate(model)

    assert "timeout" not in responses.calls[0]
    assert "max_retries" not in responses.calls[0]


def test_new_model_has_no_prior_state():
    model, responses = make_model([response([message_item("ok")])])

    generate(model)

    assert responses.calls[0]["input"] == [{"role": "user", "content": "solve"}]


def test_incomplete_reasoning_only_raises_provider_error():
    incomplete = types.SimpleNamespace(
        status="incomplete", output=[reasoning_item()], usage=None
    )
    model, _ = make_model([incomplete])

    with pytest.raises(ProviderError, match="incomplete"):
        generate(model)


def test_incomplete_empty_output_raises_provider_error():
    incomplete = types.SimpleNamespace(status="incomplete", output=[], usage=None)
    model, _ = make_model([incomplete])

    with pytest.raises(ProviderError, match="incomplete"):
        generate(model)


def test_completed_status_returns_normal_response():
    completed = types.SimpleNamespace(
        status="completed", output=[message_item("ok")], usage=None
    )
    model, _ = make_model([completed])

    result = generate(model)

    assert result.content == "ok"
    assert result.tool_calls == ()


@pytest.mark.parametrize("status", ["failed", "cancelled", "queued", "in_progress"])
def test_non_completed_top_level_status_raises_provider_error_with_valid_output(status):
    response_with_status = types.SimpleNamespace(
        status=status, output=[message_item("ok")], usage=None
    )
    model, _ = make_model([response_with_status])

    with pytest.raises(ProviderError):
        generate(model)


def test_incomplete_without_max_output_tokens_is_provider_error():
    response_with_status = types.SimpleNamespace(
        status="incomplete",
        output=[message_item("ok")],
        usage=None,
        incomplete_details={"reason": "content_filter"},
    )
    model, _ = make_model([response_with_status])

    with pytest.raises(ProviderError):
        generate(model)


def test_incomplete_max_output_tokens_is_truncated_with_text_usage_and_no_tool_calls():
    usage = types.SimpleNamespace(input_tokens=3, output_tokens=7)
    resp = types.SimpleNamespace(
        status="incomplete",
        incomplete_details={"reason": "max_output_tokens"},
        output=[message_item("partial text")],
        usage=usage,
    )
    model, _ = make_model([resp])

    result = generate(model)

    assert isinstance(result, ModelResponse)
    assert result.truncated is True
    assert result.content == "partial text"
    assert result.usage == {"input_tokens": 3, "output_tokens": 7}
    assert result.tool_calls == ()
    assert result.to_dict()["truncated"] is True


def test_incomplete_max_output_tokens_object_details_is_truncated():
    resp = types.SimpleNamespace(
        status="incomplete",
        incomplete_details=types.SimpleNamespace(reason="max_output_tokens"),
        output=[message_item("partial")],
        usage=None,
    )
    model, _ = make_model([resp])

    result = generate(model)

    assert result.truncated is True
    assert result.content == "partial"
    assert result.tool_calls == ()


def test_truncated_max_output_tokens_suppresses_invalid_partial_function_call_and_replay():
    invalid_call = function_call_item("c1", "shell", '{"command": "incomplete')
    truncated = types.SimpleNamespace(
        status="incomplete",
        incomplete_details={"reason": "max_output_tokens"},
        output=[message_item("partial"), invalid_call],
        usage=types.SimpleNamespace(input_tokens=4, output_tokens=6),
    )
    follow = types.SimpleNamespace(
        status="completed", output=[message_item("done")], usage=None
    )
    model, responses = make_model([truncated, follow])

    result = generate(model)

    assert result.truncated is True
    assert result.content == "partial"
    assert result.tool_calls == ()
    assert result.usage == {"input_tokens": 4, "output_tokens": 6}
    assert "c1" not in json.dumps(result.to_dict())
    assert all(item.get("call_id") != "c1" for item in model._built_input)
    assert all(item.get("type") != "function_call" for item in model._built_input)

    generate(
        model,
        [
            {"role": "user", "content": "solve"},
            {"role": "assistant", "content": result.content, "tool_calls": []},
            {"role": "user", "content": "next turn"},
        ],
    )
    second_input = responses.calls[1]["input"]
    assert not any(
        item.get("type") == "function_call" and item.get("call_id") == "c1"
        for item in second_input
    )
    assert not any(item.get("type") == "function_call" for item in second_input)


def test_content_filter_is_not_truncation_and_remains_provider_error():
    for details in [
        {"reason": "content_filter"},
        types.SimpleNamespace(reason="content_filter"),
    ]:
        resp = types.SimpleNamespace(
            status="incomplete",
            incomplete_details=details,
            output=[message_item("partial")],
            usage=None,
        )
        model, _ = make_model([resp])
        with pytest.raises(ProviderError, match="incomplete"):
            generate(model)

    resp_none = types.SimpleNamespace(
        status="incomplete",
        output=[message_item("partial")],
        usage=None,
    )
    model, _ = make_model([resp_none])
    with pytest.raises(ProviderError, match="incomplete"):
        generate(model)
