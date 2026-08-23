"""Anthropic-compatible Messages model adapter.

One :class:`AnthropicMessagesModel` adapts the Anthropic Messages
non-streaming endpoint to the existing FlagAgent :class:`Model` /
:class:`ModelResponse` boundary used by :class:`AgentLoop`.
Provider-specific SDK objects never leak past this module; ``generate``
returns a normalized :class:`ModelResponse`.

Design constraints (PRD-M2):

- official ``anthropic`` SDK, non-streaming ``client.messages.create``;
- a fixed explicit ``ANTHROPIC_MAX_TOKENS`` constant is used for v0.1.0;
- default SDK retry behavior is left in place for standalone use; when
  ``AgentLoop`` sets a Run wall budget via :meth:`set_remaining`, the
  request is bounded to that timeout with ``max_retries=0`` so one SDK
  call cannot exceed the Run wall budget and the loop deadline stays
  authoritative;
- client injection (``client=``) supports deterministic tests without a
  dependency-injection framework;
- malformed provider output raises :class:`ProviderError` rather than
  fabricating tool calls or leaking ``AttributeError``/``TypeError``/
  ``ValueError``;
- usage is normalized only from values the provider actually returns;
- API keys and raw exception text are never persisted or logged here.
"""

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from anthropic import Anthropic

from flagagent.model import ModelResponse, ToolCall
from flagagent.providers import (
    ProviderError,
    _client_for_budget,
    _to_json,
    _usage_field,
)

ANTHROPIC_MAX_TOKENS = 4096


def _build_anthropic_client(api_key: str, base_url: str | None) -> Anthropic:
    return Anthropic(api_key=api_key, base_url=base_url)


def _to_anthropic_tool(tool: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": tool["name"],
        "description": tool["description"],
        "input_schema": tool["parameters"],
    }


def _serialize_tool_result(result: Any) -> str:
    try:
        return _to_json(result)
    except (TypeError, ValueError):
        return str(result)


def _to_anthropic_messages(
    messages: list[dict[str, Any]],
    thinking_history: list[list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    index = 0
    assistant_index = 0
    while index < len(messages):
        message = messages[index]
        role = message["role"]
        if role == "user":
            result.append({"role": "user", "content": message.get("content", "")})
            index += 1
        elif role == "assistant":
            blocks: list[dict[str, Any]] = []
            if thinking_history is not None and assistant_index < len(thinking_history):
                for tb in thinking_history[assistant_index]:
                    blocks.append(dict(tb))
            content = message.get("content") or ""
            if content:
                blocks.append({"type": "text", "text": content})
            for call in message.get("tool_calls") or []:
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call["call_id"],
                        "name": call["name"],
                        "input": call["arguments"],
                    }
                )
            if blocks:
                result.append({"role": "assistant", "content": blocks})
            assistant_index += 1
            index += 1
        elif role == "tool":
            tool_results: list[dict[str, Any]] = []
            while index < len(messages) and messages[index]["role"] == "tool":
                tool_message = messages[index]
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_message["call_id"],
                        "content": _serialize_tool_result(tool_message["result"]),
                    }
                )
                index += 1
            result.append({"role": "user", "content": tool_results})
        else:
            raise ProviderError("unsupported message role")
    return result


def _normalize_anthropic_usage(usage: Any) -> dict[str, int] | None:
    if usage is None:
        return None
    result: dict[str, int] = {}
    input_tokens = _usage_field(usage, "input_tokens")
    output_tokens = _usage_field(usage, "output_tokens")
    if input_tokens is not None:
        result["input_tokens"] = input_tokens
    if output_tokens is not None:
        result["output_tokens"] = output_tokens
    return result


def _parse_anthropic_response(
    response: Any,
) -> tuple[ModelResponse, list[dict[str, Any]]]:
    stop_reason = getattr(response, "stop_reason", None)
    if stop_reason not in ("end_turn", "tool_use"):
        raise ProviderError("messages response has non-normal stop reason")
    content_list = getattr(response, "content", None)
    if not isinstance(content_list, list) or not content_list:
        raise ProviderError("messages response has no content")
    text_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    thinking_blocks: list[dict[str, Any]] = []
    seen_tool_use = False
    for block in content_list:
        block_type = getattr(block, "type", None)
        if block_type == "text":
            text = getattr(block, "text", None)
            if not isinstance(text, str):
                raise ProviderError("text block missing text")
            if seen_tool_use:
                raise ProviderError("text block after tool use is not supported")
            text_parts.append(text)
        elif block_type == "thinking":
            thinking = getattr(block, "thinking", None)
            signature = getattr(block, "signature", None)
            if not isinstance(thinking, str) or not isinstance(signature, str):
                raise ProviderError("thinking block missing required fields")
            if seen_tool_use:
                raise ProviderError("thinking block after tool use is not supported")
            thinking_blocks.append(
                {"type": "thinking", "thinking": thinking, "signature": signature}
            )
        elif block_type == "redacted_thinking":
            data = getattr(block, "data", None)
            if not isinstance(data, str):
                raise ProviderError("redacted thinking block missing data")
            if seen_tool_use:
                raise ProviderError(
                    "redacted thinking block after tool use is not supported"
                )
            thinking_blocks.append({"type": "redacted_thinking", "data": data})
        elif block_type == "tool_use":
            call_id = getattr(block, "id", None)
            if not isinstance(call_id, str) or not call_id:
                raise ProviderError("tool use missing id")
            name = getattr(block, "name", None)
            if not isinstance(name, str) or not name:
                raise ProviderError("tool use missing name")
            input_val = getattr(block, "input", None)
            if not isinstance(input_val, Mapping):
                raise ProviderError("tool use input must be a JSON object")
            try:
                tool_calls.append(
                    ToolCall(call_id=call_id, name=name, arguments=dict(input_val))
                )
            except (TypeError, ValueError) as error:
                raise ProviderError("tool use arguments are not strict JSON") from error
            seen_tool_use = True
        else:
            raise ProviderError("unsupported content block type")
    if stop_reason == "tool_use" and not tool_calls:
        raise ProviderError("tool_use stop reason without client tool_use block")
    if stop_reason == "end_turn" and tool_calls:
        raise ProviderError("end_turn stop reason with client tool_use block")
    content = "".join(text_parts)
    usage = _normalize_anthropic_usage(getattr(response, "usage", None))
    return (
        ModelResponse(content=content, tool_calls=tuple(tool_calls), usage=usage),
        thinking_blocks,
    )


@dataclass
class AnthropicMessagesModel:
    model: str
    api_key: str = field(repr=False)
    base_url: str | None = None
    client: Any = field(default=None)
    _remaining_budget: float | None = field(default=None, init=False, repr=False)
    _thinking_history: list[list[dict[str, Any]]] = field(
        default_factory=list, init=False, repr=False
    )

    def __post_init__(self) -> None:
        if self.client is None:
            self.client = _build_anthropic_client(self.api_key, self.base_url)

    def set_remaining(self, remaining: float) -> None:
        """Receive the Run wall-budget remaining (seconds) from ``AgentLoop``.

        When set to a positive value, the next request is bounded to that
        timeout with ``max_retries=0`` so one SDK call cannot exceed the Run
        wall budget and the loop deadline stays authoritative.  ``AgentLoop``
        invokes this via an optional ``getattr`` seam, so models without it
        (including the M0 ``ScriptedModel``) are unaffected.
        """
        if isinstance(remaining, bool) or not isinstance(remaining, (int, float)):
            raise TypeError("remaining budget must be a number")
        value = float(remaining)
        if not math.isfinite(value):
            raise ValueError("remaining budget must be a finite number")
        self._remaining_budget = value

    def generate(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelResponse:
        system_prompt = next(
            (
                message.get("content", "")
                for message in messages
                if message.get("role") == "system"
            ),
            None,
        )
        request_messages = _to_anthropic_messages(
            [message for message in messages if message.get("role") != "system"],
            self._thinking_history,
        )
        request_tools = [_to_anthropic_tool(tool) for tool in tools]
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": ANTHROPIC_MAX_TOKENS,
            "messages": request_messages,
            "tools": request_tools,
        }
        if system_prompt is not None:
            kwargs["system"] = system_prompt
        client = self.client
        if self._remaining_budget is not None:
            if self._remaining_budget <= 0:
                raise ProviderError("messages request budget exhausted")
            client, extra = _client_for_budget(self.client, self._remaining_budget)
            kwargs.update(extra)
        try:
            response = client.messages.create(**kwargs)
        except Exception as error:
            raise ProviderError("messages request failed") from error
        try:
            model_response, thinking_blocks = _parse_anthropic_response(response)
            self._thinking_history.append(thinking_blocks)
            return model_response
        except ProviderError:
            raise
        except (AttributeError, TypeError, ValueError, IndexError, KeyError) as error:
            raise ProviderError("malformed messages response") from error
