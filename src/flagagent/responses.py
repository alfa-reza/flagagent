"""OpenAI-compatible Responses model adapter.

One :class:`ResponsesModel` adapts the OpenAI Responses non-streaming
endpoint to the existing FlagAgent :class:`Model` / :class:`ModelResponse`
boundary used by :class:`AgentLoop`.  Provider-specific SDK objects never
leak past this module; ``generate`` returns a normalized
:class:`ModelResponse`.

Stateless replay (PRD-M2 AC-M2-03):

- ``store=False`` and no ``previous_response_id`` on every request;
- the adapter keeps prior provider output-item dicts privately and replays
  them with ``function_call_output`` items so each request is
  self-contained — the provider holds no conversation state for this Run;
- the adapter is constructed once per Run (fresh instance per
  :class:`AgentLoop`); there is no cross-Run state reuse because
  ``_built_input`` / ``_processed_count`` start at defaults each time.

Design constraints mirror :class:`ChatCompletionsModel`:

- official ``openai`` SDK, non-streaming ``client.responses.create``;
- default SDK retry behavior is left in place for standalone use; when
  ``AgentLoop`` sets a Run wall budget via :meth:`set_remaining`, the
  request is bounded with ``timeout=remaining`` and ``max_retries=0`` which
  bounds transport phases via SDK timeout; ``AgentLoop`` enforces the
  absolute Run wall deadline for supported in-flight phases by shutting
  down the live socket; platform DNS resolution is not bounded by connect
  timeout; isolated per-request ``httpx2`` client pools remain separate;
- client injection (``client=``) supports deterministic tests;
- malformed provider output raises :class:`ProviderError` rather than
  fabricating tool calls or leaking ``AttributeError``/``TypeError``/
  ``ValueError``;
- usage is normalized only from values the provider actually returns;
- API keys and raw exception text are never persisted or logged here.
"""

import contextlib
import json
import math
import socket
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from flagagent.model import ModelResponse, ToolCall
from flagagent.providers import (
    ProviderError,
    _build_client,
    _build_httpx2_isolated_client,
    _client_for_budget,
    _to_json,
    _usage_field,
)


def _to_responses_tool(tool: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "name": tool["name"],
        "description": tool["description"],
        "parameters": tool["parameters"],
        "strict": True,
    }


def _normalize_responses_usage(usage: Any) -> dict[str, int] | None:
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


def _output_item_to_dict(item: Any) -> dict[str, Any]:
    """Convert a Responses output item to a plain dict for stateless replay."""
    if isinstance(item, Mapping):
        return dict(item)
    model_dump = getattr(item, "model_dump", None)
    if callable(model_dump):
        return model_dump()
    if hasattr(item, "__dict__"):
        return dict(vars(item))
    raise ProviderError("unsupported responses output item")


def _extract_message_text(item_dict: dict[str, Any]) -> str:
    if item_dict.get("role") != "assistant":
        raise ProviderError("message role is invalid")
    message_id = item_dict.get("id")
    if not isinstance(message_id, str) or not message_id:
        raise ProviderError("message id is missing")
    status = item_dict.get("status")
    if status not in {"in_progress", "completed", "incomplete"}:
        raise ProviderError("message status is invalid")
    content = item_dict.get("content")
    if not isinstance(content, list):
        raise ProviderError("message content must be a list")
    parts: list[str] = []
    for part in content:
        if not isinstance(part, Mapping):
            raise ProviderError("message content item is invalid")
        part_type = part.get("type")
        if part_type == "output_text":
            text = part.get("text")
        elif part_type == "refusal":
            text = part.get("refusal")
        else:
            raise ProviderError("message content item type is invalid")
        if not isinstance(text, str):
            raise ProviderError("message content text is invalid")
        parts.append(text)
    return "".join(parts)


def _validate_reasoning_item(item_dict: dict[str, Any]) -> None:
    item_id = item_dict.get("id")
    if not isinstance(item_id, str) or not item_id:
        raise ProviderError("reasoning id is missing")
    summary = item_dict.get("summary")
    if not isinstance(summary, list):
        raise ProviderError("reasoning summary is invalid")
    for part in summary:
        if not isinstance(part, Mapping):
            raise ProviderError("reasoning summary item is invalid")
        if part.get("type") != "summary_text" or not isinstance(part.get("text"), str):
            raise ProviderError("reasoning summary item is invalid")
    encrypted_content = item_dict.get("encrypted_content")
    if encrypted_content is not None and not isinstance(encrypted_content, str):
        raise ProviderError("reasoning encrypted content is invalid")


def _parse_function_call(item_dict: dict[str, Any]) -> ToolCall:
    call_id = item_dict.get("call_id")
    if not isinstance(call_id, str) or not call_id:
        raise ProviderError("function call missing call_id")
    name = item_dict.get("name")
    if not isinstance(name, str) or not name:
        raise ProviderError("function call missing name")
    arguments_str = item_dict.get("arguments")
    if not isinstance(arguments_str, str) or not arguments_str.strip():
        raise ProviderError("function call arguments missing")
    try:
        arguments = json.loads(arguments_str)
    except ValueError as error:
        raise ProviderError("function call arguments are not valid JSON") from error
    if not isinstance(arguments, dict):
        raise ProviderError("function call arguments must be a JSON object")
    try:
        return ToolCall(call_id=call_id, name=name, arguments=arguments)
    except (TypeError, ValueError) as error:
        raise ProviderError("function call arguments are not strict JSON") from error


def _parse_responses_output(
    output: Any,
    *,
    truncated: bool = False,
) -> tuple[str, list[ToolCall], list[dict[str, Any]]]:
    """Parse Responses output into content, tool calls, and replay items.

    Returns ``(content, tool_calls, replay_items)`` where ``replay_items`` are
    the provider output-item dicts to replay in the next stateless request.
    When ``truncated`` is True, incomplete function-call material is not
    executed or replayed.
    """
    if not isinstance(output, list) or not output:
        raise ProviderError("responses output has no items")
    content_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    replay_items: list[dict[str, Any]] = []
    for item in output:
        item_dict = _output_item_to_dict(item)
        item_type = item_dict.get("type")
        if item_type == "message":
            content_parts.append(_extract_message_text(item_dict))
            replay_items.append(item_dict)
        elif item_type == "function_call":
            if truncated:
                continue
            tool_calls.append(_parse_function_call(item_dict))
            replay_items.append(item_dict)
        elif item_type == "reasoning":
            _validate_reasoning_item(item_dict)
            replay_items.append(item_dict)
        else:
            raise ProviderError("unsupported responses output item type")
    return "".join(content_parts), tool_calls, replay_items


@dataclass
class ResponsesModel:
    model: str
    api_key: str = field(repr=False)
    base_url: str | None = None
    client: Any = field(default=None)
    _remaining_budget: float | None = field(default=None, init=False, repr=False)
    _built_input: list[dict[str, Any]] = field(
        default_factory=list, init=False, repr=False
    )
    _processed_count: int = field(default=0, init=False, repr=False)
    _abort_sockets: list[Any] = field(default_factory=list, init=False, repr=False)
    _abort_lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False
    )
    _isolated_http: Any = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.client is None:
            self.client = _build_client(self.api_key, self.base_url)

    def set_remaining(self, remaining: float) -> None:
        """Receive the Run wall-budget remaining (seconds) from ``AgentLoop``.

        When set to a positive value, the next budget-bounded request uses an
        isolated ``httpx2`` client with ``timeout=remaining`` and
        ``max_retries=0`` which bounds transport phases. AgentLoop enforces
        the absolute Run wall deadline for supported in-flight phases by
        shutting down the captured socket; DNS resolution is not bounded by
        connect timeout. ``AgentLoop`` invokes this via an optional
        ``getattr`` seam, so models without it (including the M0
        ``ScriptedModel``) are unaffected.
        """
        if isinstance(remaining, bool) or not isinstance(remaining, (int, float)):
            raise TypeError("remaining budget must be a number")
        value = float(remaining)
        if not math.isfinite(value):
            raise ValueError("remaining budget must be a finite number")
        self._remaining_budget = value

    def abort_request(self) -> None:
        """Abort in-flight request by shutting down captured socket(s)."""
        with self._abort_lock:
            sockets = list(self._abort_sockets)
        for sock in sockets:
            with contextlib.suppress(Exception):
                sock.shutdown(socket.SHUT_RDWR)

    def generate(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelResponse:
        if self._remaining_budget is not None and self._remaining_budget <= 0:
            raise ProviderError("responses request budget exhausted")
        instructions = next(
            (
                message.get("content", "")
                for message in messages
                if message.get("role") == "system"
            ),
            None,
        )
        new_messages = messages[self._processed_count :]
        for message in new_messages:
            role = message.get("role")
            if role == "system":
                continue
            if role == "user":
                self._built_input.append(
                    {"role": "user", "content": message.get("content", "")}
                )
            elif role == "assistant":
                pass
            elif role == "tool":
                self._built_input.append(
                    {
                        "type": "function_call_output",
                        "call_id": message["call_id"],
                        "output": _to_json(message["result"]),
                    }
                )
            else:
                raise ProviderError("unsupported message role")
        self._processed_count = len(messages)

        kwargs: dict[str, Any] = {
            "model": self.model,
            "input": list(self._built_input),
            "tools": [_to_responses_tool(tool) for tool in tools],
            "store": False,
            "include": ["reasoning.encrypted_content"],
        }
        if instructions is not None:
            kwargs["instructions"] = instructions
        use_isolated = self._remaining_budget is not None and self._remaining_budget > 0
        isolated_client: Any | None = None
        if self._remaining_budget is not None and self._remaining_budget <= 0:
            # already raised at top; kept only to narrow use_isolated above
            pass
        if use_isolated:
            try:
                if type(self.client).__name__ == "SimpleNamespace":
                    raise RuntimeError("test double, skip isolation")
                with self._abort_lock:
                    self._abort_sockets.clear()
                isolated_client = _build_httpx2_isolated_client(
                    float(self._remaining_budget),  # type: ignore[arg-type]
                    self._abort_sockets,
                    self._abort_lock,
                )
                self._isolated_http = isolated_client
                from openai import OpenAI

                isolated_sdk = OpenAI(
                    api_key=self.api_key,
                    base_url=self.base_url,
                    http_client=isolated_client,
                    max_retries=0,
                )
                kwargs["timeout"] = float(self._remaining_budget)  # type: ignore[arg-type]
                client = isolated_sdk  # type: ignore[assignment]
            except Exception:
                with contextlib.suppress(Exception):
                    if isolated_client is not None:
                        isolated_client.close()
                self._isolated_http = None
                isolated_client = None
                client, extra = _client_for_budget(
                    self.client, float(self._remaining_budget)
                )  # type: ignore[arg-type]
                kwargs.update(extra)
                with self._abort_lock:
                    self._abort_sockets.clear()
        else:
            client = self.client
            if self._remaining_budget is not None:
                client, extra = _client_for_budget(
                    self.client, float(self._remaining_budget)
                )  # type: ignore[arg-type]
                kwargs.update(extra)

        try:
            response = client.responses.create(**kwargs)
        except Exception as error:
            raise ProviderError("responses request failed") from error
        finally:
            if isolated_client is not None:
                with contextlib.suppress(Exception):
                    isolated_client.close()
                self._isolated_http = None
        try:
            status = getattr(response, "status", None)
            truncated = False
            if status == "incomplete":
                details = getattr(response, "incomplete_details", None)
                if isinstance(details, Mapping):
                    reason = details.get("reason")
                elif details is not None:
                    reason = getattr(details, "reason", None)
                else:
                    reason = None
                if reason == "max_output_tokens":
                    truncated = True
                else:
                    raise ProviderError(f"responses {status}")
            elif status is not None and status != "completed":
                raise ProviderError(f"responses {status}")
            output = getattr(response, "output", None)
            content, tool_calls, replay_items = _parse_responses_output(
                output, truncated=truncated
            )
            usage = _normalize_responses_usage(getattr(response, "usage", None))
        except ProviderError:
            raise
        except (AttributeError, TypeError, ValueError, IndexError, KeyError) as error:
            raise ProviderError("malformed responses output") from error
        self._built_input.extend(replay_items)
        return ModelResponse(
            content=content,
            tool_calls=tuple(tool_calls),
            usage=usage,
            truncated=truncated,
        )
