"""Issue #47 transport regressions: real SDK clients via supervised process.

Matrix (3 adapters x 2 stall phases):

- ``ChatCompletionsModel`` / ``ResponsesModel`` / ``AnthropicMessagesModel``
  are constructed as production does (``api_key`` + ``base_url``), so a real
  SDK request is issued from the supervised child process.
- Body drip: valid provider JSON is streamed 15 bytes per 0.5 s so the body
  takes far longer than the Run wall budget.
- Header stall: the server accepts the POST but delays response headers.

For every combination the loop must return ``unsolved:wall_limit`` near the
wall (well before the stub would finish), record no tool_call evidence, and
the stub server must observe disconnect because the provider child process
dies/closes its socket.  The parent ``AgentLoop`` owns the absolute wall
deadline and kills/verifies the provider child; SDK ``timeout=remaining`` /
``max_retries=0`` is defense in depth only.

Control matrix proves each stub payload parses into a normal ``ModelResponse``
when delivered immediately without stall/drip.
"""

import json
import select
import socket
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from flagagent.anthropic_messages import AnthropicMessagesModel
from flagagent.artifacts import read_events
from flagagent.loop import AgentLoop, ChallengeInput, Limits
from flagagent.providers import ChatCompletionsModel
from flagagent.responses import ResponsesModel
from flagagent.tools import ExactStringVerifier, FakeExecutor

NOW = datetime(2026, 8, 14, 16, 15, 30, tzinfo=UTC)
RUN_ID = "FA-20260814T161530Z-a13f4c2d"

WALL_SECONDS = 3.2
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
            state.disconnect.set()

    def _stall_before_headers(self):
        connection = self.connection
        deadline = time.monotonic() + STALL_SECONDS
        while time.monotonic() < deadline:
            readable, _, _ = select.select([connection], [], [], 0.1)
            if readable:
                try:
                    if connection.recv(1, socket.MSG_PEEK) == b"":
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
    return cls(model="test-model", api_key="sk-test", base_url=base_url)


def assert_wall_limit_outcome(loop, result, executor):
    assert result["status:reason"] == "unsolved:wall_limit"
    assert WALL_SECONDS <= result["duration_seconds"] < 6.5
    events = read_events(loop.artifacts.events_path)
    assert [event["type"] for event in events if event["type"] == "tool_call"] == []
    assert executor.calls == []


@pytest.mark.parametrize(("name", "cls", "payload"), ADAPTERS, ids=ADAPTER_IDS)
def test_drip_body_deadline_wins(tmp_path, stub_provider, name, cls, payload):
    """A slowly dripping body must lose to the absolute Run deadline: the
    provider child is killed, wall_limit wins, and the server observes
    disconnect because the child's socket is closed."""
    stub_provider.mode = "drip"
    stub_provider.payload = payload
    base_url = f"http://127.0.0.1:{stub_provider.server_address[1]}"
    model = make_adapter(cls, base_url)
    loop = make_loop(tmp_path, model)
    executor = loop.executor  # type: ignore[attr-defined]
    result = loop.run()
    assert stub_provider.state.requests == 1
    assert_wall_limit_outcome(loop, result, executor)
    assert stub_provider.state.disconnect.wait(timeout=5.0)


@pytest.mark.parametrize(("name", "cls", "payload"), ADAPTERS, ids=ADAPTER_IDS)
def test_header_stall_deadline_wins(tmp_path, stub_provider, name, cls, payload):
    """A header stall must lose to the absolute Run wall: the provider child
    is killed and wall_limit wins even though the server still holds headers."""
    stub_provider.mode = "stall"
    stub_provider.payload = payload
    base_url = f"http://127.0.0.1:{stub_provider.server_address[1]}"
    model = make_adapter(cls, base_url)
    loop = make_loop(tmp_path, model)
    executor = loop.executor  # type: ignore[attr-defined]
    result = loop.run()
    assert stub_provider.state.requests in (0, 1)
    assert_wall_limit_outcome(loop, result, executor)
    if stub_provider.state.requests == 1:
        assert stub_provider.state.disconnect.wait(timeout=6.0)


@pytest.mark.parametrize(("name", "cls", "payload"), ADAPTERS, ids=ADAPTER_IDS)
def test_ok_payload_still_parses(tmp_path, stub_provider, name, cls, payload):
    """Control: immediate stub response parses into a normal ModelResponse."""
    stub_provider.mode = "ok"
    stub_provider.payload = payload
    base_url = f"http://127.0.0.1:{stub_provider.server_address[1]}"
    model = make_adapter(cls, base_url)
    loop = make_loop(tmp_path, model, wall=8.0)
    result = loop.run()
    assert result["status:reason"] == "unsolved:model_stop"
    assert result["model_calls"] == 1
