"""Issue #47 transport regressions: real SDK clients against local stub
HTTP servers.

Matrix (3 adapters x 2 stall phases):

- ``ChatCompletionsModel`` / ``ResponsesModel`` / ``AnthropicMessagesModel``
  are constructed exactly as production does (``api_key`` + ``base_url``),
  so the budget-bounded isolated ``httpx2``/``httpx`` client with socket
  capture is exercised.
- Body drip: valid provider JSON is streamed 15 bytes per 0.5 s so the full
  body takes far longer than the Run wall budget.
- Header stall: the server accepts the POST but waits before sending
  response headers.

For every combination the loop must return ``unsolved:wall_limit`` near the
wall (well before the stub would finish), record no tool_call evidence, and
the stub server must observe the abort-induced disconnect.  Two additional
tests pin the public ``get_extra_info("socket")`` capture surface on the
pinned httpx2/httpx versions and fail clearly if it disappears, plus one
control matrix proving each stub payload parses into a normal
:class:`ModelResponse` when delivered immediately.

These tests are deterministic, use only loopback networking, and require no
Docker marker.
"""

import json
import select
import socket
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from flagagent.anthropic_messages import (
    AnthropicMessagesModel,
    _build_httpx_isolated_client,
)
from flagagent.artifacts import read_events
from flagagent.loop import AgentLoop, ChallengeInput, Limits
from flagagent.providers import ChatCompletionsModel, _build_httpx2_isolated_client
from flagagent.responses import ResponsesModel
from flagagent.tools import ExactStringVerifier, FakeExecutor

NOW = datetime(2026, 8, 14, 16, 15, 30, tzinfo=UTC)
RUN_ID = "FA-20260814T161530Z-a13f4c2d"

WALL_SECONDS = 1.2
DRIP_CHUNK_BYTES = 15
DRIP_INTERVAL_SECONDS = 0.5
STALL_SECONDS = 6.0

CHAT_PAYLOAD = json.dumps(
    {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 0,
        "model": "test-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "done", "tool_calls": None},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2},
    }
).encode()

RESPONSES_PAYLOAD = json.dumps(
    {
        "id": "resp_1",
        "object": "response",
        "created_at": 0,
        "status": "completed",
        "model": "test-model",
        "output": [
            {
                "type": "message",
                "id": "msg_1",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": "done"}],
            }
        ],
        "usage": {"input_tokens": 3, "output_tokens": 2},
    }
).encode()

ANTHROPIC_PAYLOAD = json.dumps(
    {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "test-model",
        "content": [{"type": "text", "text": "done"}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 3, "output_tokens": 2},
    }
).encode()

ADAPTERS = [
    ("chat_completions", ChatCompletionsModel, CHAT_PAYLOAD),
    ("responses", ResponsesModel, RESPONSES_PAYLOAD),
    ("anthropic_messages", AnthropicMessagesModel, ANTHROPIC_PAYLOAD),
]
ADAPTER_IDS = [name for name, _, _ in ADAPTERS]


class StubProviderState:
    def __init__(self):
        self.requests = 0
        self.disconnect = threading.Event()


class _StubProviderHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        state = self.server.state
        state.requests += 1
        length = int(self.headers.get("Content-Length", 0))
        if length:
            self.rfile.read(length)
        if self.server.mode == "ok":
            self._send_full()
        elif self.server.mode == "drip":
            self._start_body()
            self._drip()
        else:
            self._stall_then_send()

    def _start_full_response(self, body):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()

    def _send_full(self):
        body = self.server.payload
        try:
            self._start_full_response(body)
            self.wfile.write(body)
            self.wfile.flush()
        except OSError:
            self.server.state.disconnect.set()

    def _start_body(self):
        try:
            self._start_full_response(self.server.payload)
        except OSError:
            self.server.state.disconnect.set()

    def _drip(self):
        state = self.server.state
        body = self.server.payload
        try:
            for index in range(0, len(body), DRIP_CHUNK_BYTES):
                self.wfile.write(body[index : index + DRIP_CHUNK_BYTES])
                self.wfile.flush()
                time.sleep(DRIP_INTERVAL_SECONDS)
        except OSError:
            # The abort shutdown surfaces here as broken pipe / reset.
            state.disconnect.set()

    def _stall_before_headers(self):
        connection = self.connection
        deadline = time.monotonic() + STALL_SECONDS
        while time.monotonic() < deadline:
            readable, _, _ = select.select([connection], [], [], 0.1)
            if readable:
                try:
                    if connection.recv(1, socket.MSG_PEEK) == b"":
                        # Client aborted: FIN observed while we still hold
                        # the response headers back.
                        self.server.state.disconnect.set()
                except OSError:
                    self.server.state.disconnect.set()

    def _stall_then_send(self):
        self._stall_before_headers()
        try:
            self._start_full_response(b"{}")
            self.wfile.write(b"{}")
            self.wfile.flush()
        except OSError:
            self.server.state.disconnect.set()


@pytest.fixture()
def stub_provider():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubProviderHandler)
    server.daemon_threads = True
    server.block_on_close = False
    server.state = StubProviderState()
    server.mode = "ok"
    server.payload = b"{}"
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def make_loop(tmp_path, model, wall=WALL_SECONDS):
    return AgentLoop(
        model=model,
        executor=FakeExecutor([]),
        verifier=ExactStringVerifier("Flag{ok}"),
        challenge=ChallengeInput("fixture", "solve it"),
        limits=Limits(
            max_model_turns=3, wall_timeout_seconds=wall, command_timeout_seconds=10
        ),
        runs_root=tmp_path,
        monotonic=time.monotonic,
        utc_now=lambda: NOW,
        run_id=RUN_ID,
    )


def make_adapter(cls, base_url):
    """Production-style construction: no injected client, so the adapter
    builds its own SDK client and the budget-bounded isolated transport is
    exercised."""
    return cls(model="test-model", api_key="sk-test", base_url=base_url)


def assert_wall_limit_outcome(loop, result, executor):
    assert result["status:reason"] == "unsolved:wall_limit"
    assert WALL_SECONDS <= result["duration_seconds"] < 4.0
    events = read_events(loop.artifacts.events_path)
    assert [event["type"] for event in events if event["type"] == "tool_call"] == []
    assert executor.calls == []


@pytest.mark.parametrize(("name", "cls", "payload"), ADAPTERS, ids=ADAPTER_IDS)
def test_drip_body_deadline_wins(tmp_path, stub_provider, name, cls, payload):
    """A slowly dripping response body must lose to the wall deadline: the
    request arrives, the loop returns wall_limit near the wall with no tool
    execution, and the server observes the abort disconnect."""
    stub_provider.mode = "drip"
    stub_provider.payload = payload
    base_url = f"http://127.0.0.1:{stub_provider.server_address[1]}"
    model = make_adapter(cls, base_url)
    loop = make_loop(tmp_path, model)
    executor = loop.executor  # type: ignore[attr-defined]
    result = loop.run()
    assert stub_provider.state.requests == 1
    assert_wall_limit_outcome(loop, result, executor)
    # server must have observed the abort-induced disconnect (broken pipe)
    assert stub_provider.state.disconnect.wait(timeout=5.0)


@pytest.mark.parametrize(("name", "cls", "payload"), ADAPTERS, ids=ADAPTER_IDS)
def test_header_stall_deadline_wins(tmp_path, stub_provider, name, cls, payload):
    """A header stall (server accepts POST but delays headers) must lose to the
    wall: the connection is established, the abort shuts the socket, and the
    loop returns wall_limit before the stalled server would respond."""
    stub_provider.mode = "stall"
    stub_provider.payload = payload
    base_url = f"http://127.0.0.1:{stub_provider.server_address[1]}"
    model = make_adapter(cls, base_url)
    loop = make_loop(tmp_path, model)
    executor = loop.executor  # type: ignore[attr-defined]
    result = loop.run()
    assert stub_provider.state.requests == 1
    assert_wall_limit_outcome(loop, result, executor)
    assert stub_provider.state.disconnect.wait(timeout=6.0)


@pytest.mark.parametrize(("name", "cls", "payload"), ADAPTERS, ids=ADAPTER_IDS)
def test_ok_payload_still_parses(tmp_path, stub_provider, name, cls, payload):
    """Control: when the stub responds immediately, each payload still parses
    into a normal ModelResponse and the loop ends model_stop."""
    stub_provider.mode = "ok"
    stub_provider.payload = payload
    base_url = f"http://127.0.0.1:{stub_provider.server_address[1]}"
    model = make_adapter(cls, base_url)
    loop = make_loop(tmp_path, model, wall=8.0)
    result = loop.run()
    assert result["status:reason"] == "unsolved:model_stop"
    assert result["model_calls"] == 1


def test_get_extra_info_socket_httpx2(tmp_path, stub_provider):
    """Pin the public get_extra_info('socket') surface for httpx2/httpcore2."""
    import socket as _socket
    import threading as _threading

    stub_provider.mode = "ok"
    stub_provider.payload = CHAT_PAYLOAD
    sockets: list = []
    lock = _threading.Lock()
    client = _build_httpx2_isolated_client(2.0, sockets, lock)
    url = f"http://127.0.0.1:{stub_provider.server_address[1]}/x"
    try:
        client.post(url, json={}, timeout=3.0)
    except Exception:
        pass
    client.close()
    assert len(sockets) >= 1, "get_extra_info('socket') must expose the active socket"
    assert isinstance(sockets[0], _socket.socket)


def test_get_extra_info_socket_httpx(tmp_path, stub_provider):
    """Pin the public get_extra_info('socket') surface for httpx/httpcore."""
    import socket as _socket
    import threading as _threading

    stub_provider.mode = "ok"
    stub_provider.payload = ANTHROPIC_PAYLOAD
    sockets: list = []
    lock = _threading.Lock()
    client = _build_httpx_isolated_client(2.0, sockets, lock)
    url = f"http://127.0.0.1:{stub_provider.server_address[1]}/x"
    try:
        client.post(url, json={}, timeout=3.0)
    except Exception:
        pass
    client.close()
    assert len(sockets) >= 1, "get_extra_info('socket') must expose the active socket"
    assert isinstance(sockets[0], _socket.socket)
