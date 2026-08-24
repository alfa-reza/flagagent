"""Regression tests for issue 47: fail-closed isolation and abort-before-capture."""

import socket
import threading
import types

import pytest

from flagagent.anthropic_messages import AnthropicMessagesModel
from flagagent.providers import ChatCompletionsModel, ProviderError, _capture_to_list
from flagagent.responses import ResponsesModel
from flagagent.tools import TOOL_DEFINITIONS

# ---- Fail-closed: isolated setup must not fall back for real SDK clients ----


def _dummy_messages():
    return [{"role": "user", "content": "hi"}]


def test_fail_closed_chat_completions(monkeypatch):
    monkeypatch.setattr(
        "flagagent.providers._build_httpx2_isolated_client",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("isolated boom")),
    )
    # Also patch responses copy if needed (doesn't affect chat)
    model = ChatCompletionsModel(
        model="test-model", api_key="sk-test", base_url="http://127.0.0.1:9"
    )

    # Mock the underlying fallback client to detect unwanted fallback create.
    def _fail_create(**kwargs):
        pytest.fail("fail-open: fallback create was called for ChatCompletionsModel")

    # model.client is a real OpenAI instance; replace its chat.completions.create
    monkeypatch.setattr(model.client.chat.completions, "create", _fail_create)
    model.set_remaining(5)
    with pytest.raises(ProviderError, match="isolated transport unavailable"):
        model.generate(_dummy_messages(), TOOL_DEFINITIONS)


def test_fail_closed_responses(monkeypatch):
    monkeypatch.setattr(
        "flagagent.providers._build_httpx2_isolated_client",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("isolated boom")),
    )
    # responses module has its own imported reference
    monkeypatch.setattr(
        "flagagent.responses._build_httpx2_isolated_client",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("isolated boom")),
    )
    model = ResponsesModel(
        model="test-model", api_key="sk-test", base_url="http://127.0.0.1:9"
    )

    def _fail_create(**kwargs):
        pytest.fail("fail-open: fallback create was called for ResponsesModel")

    monkeypatch.setattr(model.client.responses, "create", _fail_create)
    model.set_remaining(5)
    with pytest.raises(ProviderError, match="isolated transport unavailable"):
        model.generate(_dummy_messages(), TOOL_DEFINITIONS)


def test_fail_closed_anthropic_messages(monkeypatch):
    monkeypatch.setattr(
        "flagagent.anthropic_messages._build_httpx_isolated_client",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("isolated boom")),
    )
    model = AnthropicMessagesModel(
        model="test-model", api_key="sk-test", base_url="http://127.0.0.1:9"
    )

    def _fail_create(**kwargs):
        pytest.fail("fail-open: fallback create was called for AnthropicMessagesModel")

    monkeypatch.setattr(model.client.messages, "create", _fail_create)
    model.set_remaining(5)
    with pytest.raises(ProviderError, match="isolated transport unavailable"):
        model.generate(_dummy_messages(), TOOL_DEFINITIONS)


def test_fail_closed_test_double_still_falls_back(monkeypatch):
    """Test doubles (SimpleNamespace) must still use _client_for_budget fallback when isolated is skipped."""
    # For doubles, isolated is not attempted; should not raise isolated transport unavailable.
    completions = types.SimpleNamespace(
        create=lambda **kw: types.SimpleNamespace(
            choices=[
                types.SimpleNamespace(
                    message=types.SimpleNamespace(content="ok", tool_calls=None),
                    finish_reason="stop",
                )
            ],
            usage=None,
        )
    )
    chat = types.SimpleNamespace(completions=completions)
    client = types.SimpleNamespace(chat=chat)
    model = ChatCompletionsModel(model="test-model", api_key="sk-test", client=client)
    # Ensure isolation builder would raise if called (should not be called for double)
    monkeypatch.setattr(
        "flagagent.providers._build_httpx2_isolated_client",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("should not be called")),
    )
    model.set_remaining(5)
    result = model.generate(_dummy_messages(), TOOL_DEFINITIONS)
    assert result.content == "ok"


# ---- Abort-before-capture race ----


class _FakeSocket:
    def __init__(self):
        self.shutdown_calls = []

    def shutdown(self, how):
        self.shutdown_calls.append(how)


def test_capture_shuts_down_immediately_if_abort_already_requested():
    sockets: list = []
    lock = threading.Lock()
    sock = _FakeSocket()
    # abort flag true via callable
    _capture_to_list(sock, sockets, lock, abort_flag=lambda: True)
    assert sock in sockets
    assert sock.shutdown_calls == [socket.SHUT_RDWR]


def test_capture_no_shutdown_if_not_aborted():
    sockets: list = []
    lock = threading.Lock()
    sock = _FakeSocket()
    _capture_to_list(sock, sockets, lock, abort_flag=lambda: False)
    assert sock in sockets
    assert sock.shutdown_calls == []


def test_capture_with_event_abort():
    sockets: list = []
    lock = threading.Lock()
    ev = threading.Event()
    ev.set()
    sock = _FakeSocket()
    _capture_to_list(sock, sockets, lock, abort_flag=ev)
    assert sock.shutdown_calls == [socket.SHUT_RDWR]
    # not set => no shutdown
    sockets2: list = []
    ev2 = threading.Event()
    sock2 = _FakeSocket()
    _capture_to_list(sock2, sockets2, lock, abort_flag=ev2)
    assert sock2.shutdown_calls == []


def test_abort_request_sets_flag_and_shuts_existing_sockets():
    # Use ChatCompletionsModel with fake client
    completions = types.SimpleNamespace(create=lambda **kw: None)
    client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=completions))
    model = ChatCompletionsModel(model="test-model", api_key="sk-test", client=client)
    fake = _FakeSocket()
    with model._abort_lock:
        model._abort_sockets.append(fake)
        assert model._abort_requested is False
    model.abort_request()
    assert model._abort_requested is True
    assert fake.shutdown_calls == [socket.SHUT_RDWR]


def test_abort_before_capture_via_model_flag():
    """Simulate race: abort requested before transport captures socket."""
    completions = types.SimpleNamespace(create=lambda **kw: None)
    client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=completions))
    model = ChatCompletionsModel(model="test-model", api_key="sk-test", client=client)
    # Simulate abort already requested (as loop would do on deadline)
    with model._abort_lock:
        model._abort_requested = True
    sock = _FakeSocket()
    # Capture via helper using model's flag
    _capture_to_list(
        sock,
        model._abort_sockets,
        model._abort_lock,
        abort_flag=lambda: model._abort_requested,
    )
    assert sock.shutdown_calls == [socket.SHUT_RDWR]
    assert sock in model._abort_sockets


def test_responses_abort_flag():
    client = types.SimpleNamespace(
        responses=types.SimpleNamespace(create=lambda **kw: None)
    )
    # Need to use injected client to avoid real SDK for this unit test; double path is fine.
    model = ResponsesModel(model="test-model", api_key="sk-test", client=client)
    fake = _FakeSocket()
    with model._abort_lock:
        model._abort_sockets.append(fake)
    model.abort_request()
    assert model._abort_requested is True
    assert fake.shutdown_calls == [socket.SHUT_RDWR]
    # Now test capture after abort
    sock2 = _FakeSocket()
    _capture_to_list(
        sock2,
        model._abort_sockets,
        model._abort_lock,
        abort_flag=lambda: model._abort_requested,
    )
    assert sock2.shutdown_calls == [socket.SHUT_RDWR]


def test_anthropic_abort_flag():

    # Anthropic also reuses providers._capture_to_list but has its own model flag
    client = types.SimpleNamespace(
        messages=types.SimpleNamespace(create=lambda **kw: None)
    )
    model = AnthropicMessagesModel(model="test-model", api_key="sk-test", client=client)
    fake = _FakeSocket()
    with model._abort_lock:
        model._abort_sockets.append(fake)
    model.abort_request()
    assert model._abort_requested is True
    assert fake.shutdown_calls == [socket.SHUT_RDWR]
    sock2 = _FakeSocket()
    # Use providers helper directly (same behavior)
    _capture_to_list(
        sock2,
        model._abort_sockets,
        model._abort_lock,
        abort_flag=lambda: model._abort_requested,
    )
    assert sock2.shutdown_calls == [socket.SHUT_RDWR]


def test_set_remaining_before_worker_abort_not_lost():
    """Lost-abort race: set_remaining -> worker.start -> abort -> generate
    must not clear abort. This fails on commit 35740d0."""
    for ModelCls in (ChatCompletionsModel, ResponsesModel, AnthropicMessagesModel):
        fake_inner = types.SimpleNamespace(create=lambda **kw: None)
        if ModelCls is ChatCompletionsModel:
            client = types.SimpleNamespace(
                chat=types.SimpleNamespace(completions=fake_inner)
            )
        elif ModelCls is ResponsesModel:
            client = types.SimpleNamespace(responses=fake_inner)
        else:
            client = types.SimpleNamespace(messages=fake_inner)
        model = ModelCls(model="test-model", api_key="sk-test", client=client)
        # 1. new-request state initialized via set_remaining
        model.set_remaining(5)
        # 2. abort requested after worker.start (simulated)
        model.abort_request()
        assert model._abort_requested is True
        # 3. capture happens afterward inside generate
        sock = _FakeSocket()
        _capture_to_list(
            sock,
            model._abort_sockets,
            model._abort_lock,
            abort_flag=lambda: model._abort_requested,
        )
        assert sock.shutdown_calls == [socket.SHUT_RDWR]
        # 4. next request resets state
        model.set_remaining(5)
        assert model._abort_requested is False
        assert model._abort_sockets == []
        sock2 = _FakeSocket()
        _capture_to_list(
            sock2,
            model._abort_sockets,
            model._abort_lock,
            abort_flag=lambda: model._abort_requested,
        )
        assert sock2.shutdown_calls == []
