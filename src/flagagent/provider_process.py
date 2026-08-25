"""Supervised provider child process for wall-deadline enforcement.

The provider state (Responses _built_input, Anthropic thinking_history) is
private to the child and must survive across model turns in the same Run.
The child is started once per Run, handles sequential generate requests, and
is killed if the absolute Run deadline wins.

Only primitive configuration is pickled to the child (model name, api key,
base url, protocol variant). The supervised provider request uses a
child-owned adapter/client; the child does not receive the parent's SDK
client/socket/lock objects; only primitive configuration crosses the boundary.

IPC uses two unidirectional pipes:

    parent request_tx  ---> child request_rx   (generate / close)
    parent response_rx <--- child response_tx  (ready / response / provider_error / worker_error)

Protocol (dicts):

    -> {"type":"generate","messages":..., "tools":..., "remaining": float|None}
    <- {"type":"ready"}
    <- {"type":"response","response": dict(ModelResponse.to_dict())}
    <- {"type":"provider_error","error": str}
    <- {"type":"worker_error","error": str}
    -> {"type":"close"}

A worker/IPC session terminated by the wall deadline is disposable and is
never reused.
"""

from __future__ import annotations

import multiprocessing
import multiprocessing.connection
from dataclasses import dataclass

TERM_GRACE = 0.3
KILL_GRACE = 0.3
CLOSE_GRACE = 0.4
SENDER_JOIN_GRACE = 1.5


class ProviderProcessTerminationError(RuntimeError):
    pass


class ProviderSenderTerminationError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProviderConfig:
    protocol: str
    model: str
    api_key: str
    base_url: str | None


def _child_main(
    config: ProviderConfig, request_rx, response_tx
) -> None:  # pragma: no cover - exercised via integration tests
    try:
        adapter = _build_adapter(config)
    except Exception as error:
        try:
            response_tx.send(
                {
                    "type": "worker_error",
                    "error": f"adapter_init:{type(error).__name__}",
                }
            )
        except Exception:
            pass
        try:
            request_rx.close()
        except Exception:
            pass
        try:
            response_tx.close()
        except Exception:
            pass
        return

    try:
        response_tx.send({"type": "ready"})
    except Exception:
        try:
            request_rx.close()
        except Exception:
            pass
        try:
            response_tx.close()
        except Exception:
            pass
        return

    while True:
        try:
            msg = request_rx.recv()
        except EOFError:
            break
        except Exception:
            break

        if not isinstance(msg, dict):
            continue
        msg_type = msg.get("type")
        if msg_type == "close":
            break
        if msg_type != "generate":
            continue

        messages = msg.get("messages") or []
        tools = msg.get("tools") or []
        remaining = msg.get("remaining")

        if remaining is not None:
            try:
                setter = getattr(adapter, "set_remaining", None)
                if callable(setter):
                    setter(float(remaining))
            except Exception:
                pass

        try:
            response = adapter.generate(messages, tools)
        except Exception as error:
            try:
                response_tx.send(
                    {"type": "provider_error", "error": f"{type(error).__name__}"}
                )
            except Exception:
                break
            continue
        except BaseException:  # pragma: no cover
            try:
                response_tx.send({"type": "worker_error", "error": "base_exception"})
            except Exception:
                pass
            break

        try:
            payload = (
                response.to_dict()
                if hasattr(response, "to_dict")
                else {
                    "content": getattr(response, "content", ""),
                    "tool_calls": [],
                    "usage": getattr(response, "usage", None),
                    "truncated": bool(getattr(response, "truncated", False)),
                }
            )
            response_tx.send({"type": "response", "response": payload})
        except Exception:
            try:
                response_tx.send(
                    {"type": "worker_error", "error": "response_serialize"}
                )
            except Exception:
                pass
            break

    try:
        request_rx.close()
    except Exception:
        pass
    try:
        response_tx.close()
    except Exception:
        pass


def _build_adapter(config: ProviderConfig):
    protocol = config.protocol
    if protocol == "openai-chat":
        from flagagent.providers import ChatCompletionsModel

        return ChatCompletionsModel(
            model=config.model, api_key=config.api_key, base_url=config.base_url
        )
    if protocol == "openai-responses":
        from flagagent.responses import ResponsesModel

        return ResponsesModel(
            model=config.model, api_key=config.api_key, base_url=config.base_url
        )
    if protocol == "anthropic":
        from flagagent.anthropic_messages import AnthropicMessagesModel

        return AnthropicMessagesModel(
            model=config.model, api_key=config.api_key, base_url=config.base_url
        )
    raise ValueError(f"unsupported provider protocol {protocol!r}")


class ProviderProcess:
    """One persistent provider child per Run.

    Must be closed explicitly; daemon=False with explicit parent ownership.
    After deadline termination the pipes are discarded and must not be reused.
    """

    def __init__(self, config: ProviderConfig) -> None:
        self._ctx = multiprocessing.get_context("spawn")
        request_recv, request_send = self._ctx.Pipe(duplex=False)
        response_recv, response_send = self._ctx.Pipe(duplex=False)
        self._request_tx = request_send
        self._response_rx = response_recv
        self._proc = self._ctx.Process(
            target=_child_main, args=(config, request_recv, response_send), daemon=False
        )
        self._closed = False
        self._proc.start()
        try:
            request_recv.close()
        except Exception:
            pass
        try:
            response_send.close()
        except Exception:
            pass

    @property
    def request_tx(self):
        return self._request_tx

    @property
    def response_rx(self):
        return self._response_rx

    @property
    def conn(self):
        return self._response_rx

    @property
    def proc(self):
        return self._proc

    @property
    def exitcode(self):
        return self._proc.exitcode

    def is_alive(self) -> bool:
        return self._proc.is_alive()

    def terminate_for_deadline(self) -> None:
        if self._closed:
            return
        self._force_terminate()
        still_alive = self._proc.is_alive() or self._proc.exitcode is None
        try:
            self._request_tx.close()
        except Exception:
            pass
        try:
            self._response_rx.close()
        except Exception:
            pass
        if still_alive:
            raise ProviderProcessTerminationError(
                f"provider child still alive after SIGKILL pid={self._proc.pid} alive={self._proc.is_alive()} exitcode={self._proc.exitcode}"
            )
        self._closed = True

    def _force_terminate(self) -> None:
        if not self._proc.is_alive():
            try:
                self._proc.join(timeout=0.2)
            except Exception:
                pass
            return
        try:
            self._proc.terminate()
        except Exception:
            pass
        self._proc.join(timeout=TERM_GRACE)
        if self._proc.is_alive():
            try:
                self._proc.kill()
            except Exception:
                pass
            self._proc.join(timeout=KILL_GRACE)

    def close(self) -> None:
        if self._closed:
            return
        is_alive_before = self._proc.is_alive()
        if is_alive_before:
            try:
                self._request_tx.send({"type": "close"})
            except Exception:
                pass
            self._proc.join(timeout=CLOSE_GRACE)
        if self._proc.is_alive():
            self._force_terminate()
        still_alive = self._proc.is_alive() or self._proc.exitcode is None
        try:
            self._request_tx.close()
        except Exception:
            pass
        try:
            self._response_rx.close()
        except Exception:
            pass
        if still_alive:
            if self._proc.is_alive():
                raise ProviderProcessTerminationError(
                    f"provider child still alive after close pid={self._proc.pid}"
                )
            return
        self._closed = True
