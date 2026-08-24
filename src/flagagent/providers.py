"""OpenAI-compatible Chat Completions model adapter.

One :class:`ChatCompletionsModel` adapts the OpenAI Chat Completions
non-streaming endpoint to the existing FlagAgent :class:`Model` /
:class:`ModelResponse` boundary used by :class:`AgentLoop`.  Provider-specific
SDK objects never leak past this module; ``generate`` returns a normalized
:class:`ModelResponse`.

OpenRouter is supported through the same adapter by passing an OpenRouter
``base_url`` with the OpenAI-compatible API key and model name; no separate
provider architecture is introduced.

Design constraints (PRD-M2):

- official ``openai`` SDK, non-streaming ``client.chat.completions.create``;
- default SDK retry behavior is intentionally left in place for standalone use;
  when ``AgentLoop`` sets a Run wall budget via :meth:`set_remaining`, the
  request uses ``timeout=remaining`` and ``max_retries=0`` which bounds
  transport phases (connect/read/write/pool) via the SDK timeout;
  the :class:`AgentLoop` enforces the absolute Run wall deadline by
  terminating a supervised provider child process; platform DNS resolution is
  not bounded by connect timeout; SDK timeout remains defense in depth;
- client injection (``client=``) supports deterministic tests without a
  dependency-injection framework;
- malformed provider output raises :class:`ProviderError` rather than
  fabricating tool calls or leaking ``AttributeError``/``TypeError``/
  ``ValueError``;
- usage is normalized only from values the provider actually returns;
- API keys and raw exception text are never persisted or logged here.
"""

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from openai import OpenAI

from flagagent.model import ModelResponse, ToolCall


class ProviderError(RuntimeError):
    """Provider/adapter failure, distinct from ordinary model output.

    Raised when the provider response is malformed or the SDK call fails, so
    ``AgentLoop`` maps it to ``provider_error`` without fabricating tool calls
    or leaking credentials.
    """


def _build_client(api_key: str, base_url: str | None) -> OpenAI:
    return OpenAI(api_key=api_key, base_url=base_url)


def _client_for_budget(client: Any, budget: float) -> tuple[Any, dict[str, Any]]:
    """Return ``(client, extra_kwargs)`` for a budget-bounded request.

    Official OpenAI/Anthropic SDK clients expose ``with_options``; the returned
    client carries ``timeout``/``max_retries=0`` and ``extra_kwargs`` is empty
    so ``create`` never receives ``max_retries``.  Injected test doubles without
    ``with_options`` receive ``timeout``/``max_retries`` as create kwargs.
    """
    with_options = getattr(client, "with_options", None)
    if callable(with_options):
        return with_options(timeout=budget, max_retries=0), {}
    return client, {"timeout": budget, "max_retries": 0}


def _to_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _to_chat_tool(tool: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool["description"],
            "parameters": tool["parameters"],
        },
    }


def _to_chat_tool_call(call: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": call["call_id"],
        "type": "function",
        "function": {
            "name": call["name"],
            "arguments": _to_json(call["arguments"]),
        },
    }


def _to_chat_message(message: dict[str, Any]) -> dict[str, Any]:
    role = message["role"]
    if role in {"system", "user"}:
        return {"role": role, "content": message.get("content", "")}
    if role == "assistant":
        tool_calls = message.get("tool_calls") or []
        chat_message: dict[str, Any] = {
            "role": "assistant",
            "content": message.get("content") or None,
        }
        if tool_calls:
            chat_message["tool_calls"] = [
                _to_chat_tool_call(call) for call in tool_calls
            ]
        return chat_message
    if role == "tool":
        return {
            "role": "tool",
            "tool_call_id": message["call_id"],
            "content": _to_json(message["result"]),
        }
    raise ProviderError("unsupported message role")


def _usage_field(usage: Any, name: str) -> int | None:
    value = (
        usage.get(name) if isinstance(usage, Mapping) else getattr(usage, name, None)
    )
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _normalize_usage(usage: Any) -> dict[str, int] | None:
    if usage is None:
        return None
    result: dict[str, int] = {}
    prompt = _usage_field(usage, "prompt_tokens")
    completion = _usage_field(usage, "completion_tokens")
    if prompt is not None:
        result["input_tokens"] = prompt
    if completion is not None:
        result["output_tokens"] = completion
    return result


def _parse_chat_response(response: Any) -> ModelResponse:
    choices = getattr(response, "choices", None)
    if not isinstance(choices, list) or not choices:
        raise ProviderError("chat completions response has no choices")
    choice = choices[0]
    finish_reason = getattr(choice, "finish_reason", None)
    truncated = finish_reason == "length"
    if not truncated and finish_reason not in {"stop", "tool_calls"}:
        raise ProviderError("chat completions finish reason is not normal")
    message = getattr(choice, "message", None)
    if message is None:
        raise ProviderError("chat completions response missing message")
    raw_content = getattr(message, "content", None)
    content = raw_content if isinstance(raw_content, str) else ""
    raw_tool_calls = getattr(message, "tool_calls", None)
    if truncated:
        tool_calls: list[ToolCall] = []
    else:
        if raw_tool_calls is None:
            raw_tool_calls = []
        elif not isinstance(raw_tool_calls, list):
            raise ProviderError("tool calls must be a list")
        tool_calls = []
        for raw in raw_tool_calls:
            call_id = getattr(raw, "id", None)
            if not isinstance(call_id, str) or not call_id:
                raise ProviderError("tool call missing id")
            function = getattr(raw, "function", None)
            if function is None:
                raise ProviderError("tool call missing function")
            name = getattr(function, "name", None)
            if not isinstance(name, str) or not name:
                raise ProviderError("tool call missing function name")
            arguments_str = getattr(function, "arguments", None)
            if not isinstance(arguments_str, str) or not arguments_str.strip():
                raise ProviderError("tool call arguments missing")
            try:
                arguments = json.loads(arguments_str)
            except ValueError as error:
                raise ProviderError("tool call arguments are not valid JSON") from error
            if not isinstance(arguments, dict):
                raise ProviderError("tool call arguments must be a JSON object")
            try:
                tool_calls.append(
                    ToolCall(call_id=call_id, name=name, arguments=arguments)
                )
            except (TypeError, ValueError) as error:
                raise ProviderError(
                    "tool call arguments are not strict JSON"
                ) from error
    usage = _normalize_usage(getattr(response, "usage", None))
    return ModelResponse(
        content=content, tool_calls=tuple(tool_calls), usage=usage, truncated=truncated
    )


@dataclass
class ChatCompletionsModel:
    model: str
    api_key: str = field(repr=False)
    base_url: str | None = None
    client: Any = field(default=None)
    _remaining_budget: float | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.client is None:
            self.client = _build_client(self.api_key, self.base_url)

    def set_remaining(self, remaining: float) -> None:
        if isinstance(remaining, bool) or not isinstance(remaining, (int, float)):
            raise TypeError("remaining budget must be a number")
        value = float(remaining)
        if not math.isfinite(value):
            raise ValueError("remaining budget must be a finite number")
        self._remaining_budget = value

    def generate(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelResponse:
        request_messages = [_to_chat_message(message) for message in messages]
        request_tools = [_to_chat_tool(tool) for tool in tools]
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": request_messages,
            "tools": request_tools,
        }
        client = self.client
        if self._remaining_budget is not None:
            if self._remaining_budget <= 0:
                raise ProviderError("chat completions request budget exhausted")
            client, extra = _client_for_budget(self.client, self._remaining_budget)
            kwargs.update(extra)
        try:
            response = client.chat.completions.create(**kwargs)
        except Exception as error:
            raise ProviderError("chat completions request failed") from error
        try:
            return _parse_chat_response(response)
        except ProviderError:
            raise
        except (AttributeError, TypeError, ValueError, IndexError, KeyError) as error:
            raise ProviderError("malformed chat completions response") from error
