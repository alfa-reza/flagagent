"""Supervised provider child process for wall-deadline enforcement.

The provider state (Responses _built_input, Anthropic thinking_history) is
private to the child and must survive across model turns in the same Run.
The child is started once per Run, handles sequential generate requests over
a Pipe, and is killed if the absolute Run deadline wins.

Only primitive configuration is pickled to the child (model name, api key,
base url, protocol variant).  SDK/HTTP clients, sockets and locks are
constructed solely inside the child.

IPC protocol (all messages are one dict over Pipe):
  -> {"type":"generate","messages":..., "tools":..., "remaining": float|None}
  <- {"type":"response","response": dict(ModelResponse.to_dict())}
  <- {"type":"provider_error","error": str}
  <- {"type":"worker_error","error": str}
  -> {"type":"close"}

Parent never sends exceptions or SDK objects; child never leaks raw
exceptions (they are mapped to provider_error strings).
"""

from __future__ import annotations

import multiprocessing
import multiprocessing.connection
import traceback
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ProviderConfig:
    protocol: str  # openai-chat | openai-responses | anthropic
    model: str
    api_key: str
    base_url: str | None


def _child_main(
    config: ProviderConfig, conn
) -> None:  # pragma: no cover - exercised via integration tests
    try:
        adapter = _build_adapter(config)
    except Exception as error:
        # Child failed to construct adapter; notify parent then exit.
        try:
            conn.send(
                {
                    "type": "worker_error",
                    "error": f"adapter_init:{type(error).__name__}",
                }
            )
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass
        return

    while True:
        try:
            msg = conn.recv()
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

        # Remaining influences adapter's per-request SDK timeout; only if valid.
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
            # ProviderError and others become provider_error for parent arbitration.
            try:
                conn.send(
                    {"type": "provider_error", "error": f"{type(error).__name__}"}
                )
            except Exception:
                break
            continue
        except BaseException:  # pragma: no cover
            try:
                conn.send({"type": "worker_error", "error": "base_exception"})
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
            conn.send({"type": "response", "response": payload})
        except Exception:
            try:
                conn.send({"type": "worker_error", "error": "response_serialize"})
            except Exception:
                pass
            break

    try:
        conn.close()
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

    Must be closed explicitly; daemon=False.  After deadline termination the
    Pipe is discarded and must not be reused.
    """

    def __init__(self, config: ProviderConfig) -> None:
        self._ctx = multiprocessing.get_context("spawn")
        parent_conn, child_conn = self._ctx.Pipe(duplex=True)
        self._conn = parent_conn
        self._child_conn = child_conn
        self._proc = self._ctx.Process(
            target=_child_main, args=(config, child_conn), daemon=False
        )
        self._closed = False
        self._proc.start()
        # Parent no longer needs child end.
        try:
            self._child_conn.close()
        except Exception:
            pass
        # mypy: _child_conn not used again
        del self._child_conn

    @property
    def conn(self):
        return self._conn

    @property
    def proc(self):
        return self._proc

    def is_alive(self) -> bool:
        return self._proc.is_alive()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._conn.close()
        except Exception:
            pass
        # Bounded terminate/kill if still alive.
        self._terminate_and_join()

    def _terminate_and_join(self, grace: float = 0.6) -> None:
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
        self._proc.join(timeout=grace)
        if self._proc.is_alive():
            try:
                self._proc.kill()
            except Exception:
                pass
            self._proc.join(timeout=grace)
