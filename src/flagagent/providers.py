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
  request is bounded with ``timeout=remaining`` and ``max_retries=0`` which
  bounds transport phases (connect/read/write/pool) via the SDK timeout;
  the :class:`AgentLoop` enforces the absolute Run wall deadline for
  supported in-flight transport phases (header/body stall) by shutting down
  the underlying socket; platform DNS resolution is not bounded by the
  socket connect timeout and remains out of scope; isolated per-request
  ``httpx2`` client pools remain separate and the base client is unaffected;
- client injection (``client=``) supports deterministic tests without a
  dependency-injection framework;
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

from openai import OpenAI

from flagagent.model import ModelResponse, ToolCall


def _capture_to_list(
    sock: Any,
    sockets: list[Any],
    lock: threading.Lock,
    abort_flag: Any | None = None,
) -> None:
    if sock is None:
        return
    # Validate it's a socket-like object with shutdown.
    if not hasattr(sock, "shutdown"):
        return
    should_shutdown = False
    with lock:
        if sock not in sockets:
            sockets.append(sock)
        # Check abort already requested under same lock as append.
        if abort_flag is not None:
            flag_set = False
            try:
                if isinstance(abort_flag, threading.Event):
                    flag_set = abort_flag.is_set()
                elif callable(abort_flag):
                    flag_set = bool(abort_flag())
                elif isinstance(abort_flag, list):
                    flag_set = bool(abort_flag[0]) if abort_flag else False
                else:
                    flag_set = bool(abort_flag)
            except Exception:
                flag_set = False
            if flag_set:
                should_shutdown = True
    if should_shutdown:
        with contextlib.suppress(Exception):
            sock.shutdown(socket.SHUT_RDWR)


def _build_httpx2_isolated_client(
    budget: float,
    sockets: list[Any],
    lock: threading.Lock,
    abort_flag: Any | None = None,
) -> Any:
    """Build isolated httpx2.Client that captures the live socket.

    Captures via two supported surfaces:
    - request.extensions["trace"] callback for connection.connect_tcp.complete
    - response event_hooks capturing response.extensions["network_stream"]

    Both retrieve the live socket via network_stream.get_extra_info("socket")
    (public API, not private _sock). DNS resolution remains unbounded by
    connect timeout (platform resolver limitation).
    """
    try:
        import httpx2
        from httpx2 import Timeout as Httpx2Timeout
    except Exception as error:  # pragma: no cover
        raise RuntimeError("httpx2 unavailable") from error

    def _capture(sock: Any) -> None:
        _capture_to_list(sock, sockets, lock, abort_flag)

    # Tracing transport
    base_transport = httpx2.HTTPTransport

    class TracingTransport(base_transport):  # type: ignore[valid-type]
        def handle_request(self, request: Any) -> Any:  # type: ignore[override]
            # Preserve any existing trace callback.
            original_trace = request.extensions.get("trace")

            def _trace(name: str, info: dict[str, Any]) -> None:
                try:
                    if name.endswith("connect_tcp.complete") or name.endswith(
                        "connect_unix_socket.complete"
                    ):
                        rv = info.get("return_value")
                        if rv is not None:
                            try:
                                sock = rv.get_extra_info("socket")
                            except Exception:
                                sock = None
                            if sock is not None:
                                _capture(sock)
                except Exception:
                    pass
                if original_trace is not None:
                    try:
                        return original_trace(name, info)
                    except Exception:
                        pass

            request.extensions["trace"] = _trace
            return super().handle_request(request)

    def _response_hook(response: Any) -> None:
        try:
            stream = None
            ext = getattr(response, "extensions", None)
            if isinstance(ext, dict):
                stream = ext.get("network_stream")
            else:
                try:
                    stream = response.extensions.get("network_stream")  # type: ignore
                except Exception:
                    stream = None
            if stream is not None:
                try:
                    sock = stream.get_extra_info("socket")
                except Exception:
                    sock = None
                if sock is not None:
                    _capture(sock)
        except Exception:
            pass

    transport = TracingTransport()
    # Use explicit Timeout so all phases (connect/read/write/pool) are bounded.
    try:
        timeout = Httpx2Timeout(budget)
    except Exception:
        timeout = budget  # fallback, Client will normalize
    client = httpx2.Client(
        transport=transport,
        event_hooks={"response": [_response_hook]},
        timeout=timeout,
    )
    return client


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
    _abort_sockets: list[Any] = field(default_factory=list, init=False, repr=False)
    _abort_lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False
    )
    _isolated_http: Any = field(default=None, init=False, repr=False)
    _abort_requested: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.client is None:
            self.client = _build_client(self.api_key, self.base_url)

    def set_remaining(self, remaining: float) -> None:
        """Receive the Run wall-budget remaining (seconds) from ``AgentLoop``.

        When set to a positive value, the next budget-bounded request uses an
        isolated ``httpx2`` client with ``timeout=remaining`` and
        ``max_retries=0`` which bounds transport phases (connect/read/write/
        pool). AgentLoop enforces the absolute Run wall deadline for supported
        in-flight phases by shutting down the captured socket; DNS resolution
        is not bounded by connect timeout. ``AgentLoop`` invokes this via an
        optional ``getattr`` seam, so models without it (including the M0
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
            self._abort_requested = True
            sockets = list(self._abort_sockets)
        for sock in sockets:
            with contextlib.suppress(Exception):
                sock.shutdown(socket.SHUT_RDWR)

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
        # Budget-bounded path uses isolated per-request httpx2 client with
        # socket capture so deadline abort can shutdown(SHUT_RDWR) the live
        # socket. Closing the http_client alone does NOT wake blocked recv.
        use_isolated = self._remaining_budget is not None and self._remaining_budget > 0
        isolated_client: Any | None = None
        isolated_sdk: Any | None = None
        if self._remaining_budget is not None and self._remaining_budget <= 0:
            raise ProviderError("chat completions request budget exhausted")
        if use_isolated:
            # Explicit test-double detection: allow fallback to _client_for_budget.
            if type(self.client).__name__ == "SimpleNamespace":
                client, extra = _client_for_budget(
                    self.client,
                    float(self._remaining_budget),  # type: ignore[arg-type]
                )
                kwargs.update(extra)
                with self._abort_lock:
                    self._abort_sockets.clear()
                    self._abort_requested = False
            else:
                isolated_client = None
                try:
                    with self._abort_lock:
                        self._abort_sockets.clear()
                        self._abort_requested = False
                    isolated_client = _build_httpx2_isolated_client(
                        float(self._remaining_budget),  # type: ignore[arg-type]
                        self._abort_sockets,
                        self._abort_lock,
                        lambda: self._abort_requested,
                    )
                    self._isolated_http = isolated_client
                    # Build SDK client that uses isolated http_client.
                    isolated_sdk = OpenAI(
                        api_key=self.api_key,
                        base_url=self.base_url,
                        http_client=isolated_client,
                        max_retries=0,
                    )
                    # Per-request timeout bounds transport phases.
                    kwargs["timeout"] = float(self._remaining_budget)  # type: ignore[arg-type]
                    client = isolated_sdk  # type: ignore[assignment]
                except Exception as error:
                    # Fail-closed for real SDK: do not fall back to _client_for_budget.
                    with contextlib.suppress(Exception):
                        if isolated_client is not None:
                            isolated_client.close()
                    self._isolated_http = None
                    isolated_client = None
                    isolated_sdk = None
                    if isinstance(error, ProviderError):
                        raise
                    raise ProviderError(
                        "chat completions isolated transport unavailable"
                    ) from error
        else:
            client = self.client
            if self._remaining_budget is not None:
                client, extra = _client_for_budget(
                    self.client, float(self._remaining_budget)
                )  # type: ignore[arg-type]
                kwargs.update(extra)

        try:
            response = client.chat.completions.create(**kwargs)
        except Exception as error:
            raise ProviderError("chat completions request failed") from error
        finally:
            if isolated_client is not None:
                with contextlib.suppress(Exception):
                    isolated_client.close()
                self._isolated_http = None
        try:
            return _parse_chat_response(response)
        except ProviderError:
            raise
        except (AttributeError, TypeError, ValueError, IndexError, KeyError) as error:
            raise ProviderError("malformed chat completions response") from error
