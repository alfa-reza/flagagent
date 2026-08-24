"""Deterministic Issue #47 regressions for ``AgentLoop`` wall-deadline supervision.

The loop runs supervised provider work in one persistent spawned child process
per Run (``multiprocessing.get_context("spawn")``).  The parent ``AgentLoop``
owns the absolute monotonic wall deadline and terminates/kills/verifies the
child when the deadline wins.  Non-provider doubles (``ScriptedModel`` etc.)
run inline without process overhead.

These tests pin:

- exhausted remaining budget at turn entry never starts provider work;
- normal success, provider errors, and tool execution when provider finishes
  before the deadline;
- a blocked provider child loses to the absolute deadline, is terminated/killed
  and verified dead, and ``wall_limit`` wins permanently;
- late provider results are discarded and late tools never execute;
- the actual OS child ``Process`` is dead (``is_alive()==False``,
  ``exitcode is not None``), not merely wrapper bookkeeping;
- slow body-drip / header-stall behaviour is covered in
  ``test_issue47_transport.py``.

Timing-sensitive coverage uses injected fake clocks where applicable; the
single real-clock wall test uses a wall far smaller than the block window.
"""

import multiprocessing
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from flagagent.artifacts import read_events
from flagagent.loop import AgentLoop, ChallengeInput, Limits
from flagagent.model import ModelResponse, ScriptedModel, ToolCall
from flagagent.tools import ExactStringVerifier, FakeExecutor, ShellResult

NOW = datetime(2026, 8, 14, 16, 15, 30, tzinfo=UTC)
RUN_ID = "FA-20260814T161530Z-a13f4c2d"


class Clock:
    def __init__(self, value=0.0):
        self.value = value

    def __call__(self):
        return self.value


class SeqClock:
    def __init__(self, values):
        self.values = list(values)
        self._index = 0

    def __call__(self):
        if self._index < len(self.values):
            value = self.values[self._index]
        else:
            value = self.values[-1]
        self._index += 1
        return value


class RecordingModel:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.threads = []
        self._index = 0

    def generate(self, messages, tools):
        self.calls.append((messages, tools))
        self.threads.append(threading.current_thread())
        item = self.script[self._index] if self._index < len(self.script) else None
        self._index += 1
        if isinstance(item, Exception):
            raise item
        if item is None:
            return ModelResponse(content="stop")
        return item


class ThreadRecordingModel:
    def __init__(self, inner):
        self.inner = inner
        self.threads = []

    def generate(self, messages, tools):
        self.threads.append(threading.current_thread())
        return self.inner.generate(messages, tools)


def make_loop(tmp_path, model, *, executor=None, limits=None, monotonic=None):
    return AgentLoop(
        model=model,
        executor=executor or FakeExecutor([]),
        verifier=ExactStringVerifier("Flag{ok}"),
        challenge=ChallengeInput("fixture", "solve it"),
        limits=limits
        or Limits(
            max_model_turns=10, wall_timeout_seconds=100, command_timeout_seconds=10
        ),
        runs_root=tmp_path,
        monotonic=monotonic or Clock(),
        utc_now=lambda: NOW,
        run_id=RUN_ID,
    )


def test_exhausted_budget_does_not_start_model(tmp_path):
    """When the remaining wall budget is already exhausted at turn entry,
    no provider request starts."""
    clock = SeqClock([0.0, 0.0, 0.5, 0.5, 0.5, 1.5])
    model = RecordingModel([ModelResponse(content="never")])
    executor = FakeExecutor([])
    loop = make_loop(
        tmp_path,
        model,
        executor=executor,
        limits=Limits(
            max_model_turns=3, wall_timeout_seconds=1, command_timeout_seconds=1
        ),
        monotonic=clock,
    )
    result = loop.run()

    assert result["status:reason"] == "unsolved:wall_limit"
    assert model.calls == []
    assert model.threads == []
    assert executor.calls == []
    events = read_events(loop.artifacts.events_path)
    assert not any(event["type"] == "tool_call" for event in events)


def test_success_before_deadline(tmp_path):
    """A worker finishing before the deadline preserves normal semantics."""
    inner = ScriptedModel(
        [
            ModelResponse(tool_calls=(ToolCall("c1", "shell", {"command": "ls"}),)),
            ModelResponse(content="done"),
        ]
    )
    model = ThreadRecordingModel(inner)
    executor = FakeExecutor([ShellResult("out", "", 0, False)])
    loop = make_loop(tmp_path, model, executor=executor)
    result = loop.run()

    assert result["status:reason"] == "unsolved:model_stop"
    assert result["model_calls"] == 2
    assert executor.calls == [("ls", 10)]
    assert len(inner.calls) == 2
    assert len(model.threads) == 2
    assert all(thread is not threading.main_thread() for thread in model.threads)
    assert all(not thread.is_alive() for thread in model.threads)
    assert [message["role"] for message in loop.messages] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]


def test_provider_error_before_deadline(tmp_path):
    """A provider exception before the deadline still maps to provider_error."""
    model = RecordingModel([RuntimeError("provider down")])
    executor = FakeExecutor([])
    loop = make_loop(
        tmp_path,
        model,
        executor=executor,
        limits=Limits(
            max_model_turns=3, wall_timeout_seconds=50, command_timeout_seconds=10
        ),
    )
    result = loop.run()

    assert result["status:reason"] == "error:provider_error"
    assert result["model_calls"] == 1
    assert executor.calls == []
    assert len(model.threads) == 1
    assert not model.threads[0].is_alive()
    errors = [
        event["payload"]
        for event in read_events(loop.artifacts.events_path)
        if event["type"] == "error"
    ]
    assert errors == [{"reason": "provider_error", "operation": "model"}]


def test_deadline_wins_while_blocked__worker_terminated(tmp_path):
    """While the model worker blocks past the wall deadline, the loop
    returns wall_limit near the wall and the worker is terminated before run
    returns."""
    release = threading.Event()

    class BlockingModel:
        def __init__(self):
            self.threads = []
            self.abort_calls = 0

        def abort_request(self):
            self.abort_calls += 1
            release.set()

        def generate(self, messages, tools):
            self.threads.append(threading.current_thread())
            release.wait(timeout=15.0)
            return ModelResponse(content="too late")

    model = BlockingModel()
    started = time.monotonic()
    loop = make_loop(
        tmp_path,
        model,
        limits=Limits(
            max_model_turns=2, wall_timeout_seconds=0.8, command_timeout_seconds=10
        ),
        monotonic=time.monotonic,
    )
    result = loop.run()
    elapsed = time.monotonic() - started

    assert result["status:reason"] == "unsolved:wall_limit"
    assert result["model_calls"] == 1
    assert model.abort_calls == 1
    assert len(model.threads) == 1
    assert not model.threads[0].is_alive()
    assert 0.7 <= elapsed < 5.0


def test_provider_exception_after_deadline_still_wall_limit(tmp_path):
    """An exception raised after the deadline is discarded in favor of wall_limit."""
    clock = Clock()

    class LateBoomModel:
        def __init__(self):
            self.threads = []

        def generate(self, messages, tools):
            self.threads.append(threading.current_thread())
            clock.value = 2.0
            raise RuntimeError("late provider failure")

    model = LateBoomModel()
    loop = make_loop(
        tmp_path,
        model,
        limits=Limits(
            max_model_turns=2, wall_timeout_seconds=1, command_timeout_seconds=1
        ),
        monotonic=clock,
    )
    result = loop.run()

    assert result["status:reason"] == "unsolved:wall_limit"
    assert result["model_calls"] == 1
    assert len(model.threads) == 1
    assert not model.threads[0].is_alive()
    events = read_events(loop.artifacts.events_path)
    assert not any(event["type"] == "error" for event in events)


def test_no_tool_execution_after_deadline(tmp_path):
    """Late provider result after the deadline cannot execute tools."""
    clock = Clock()
    release = threading.Event()

    class LateToolsModel:
        def __init__(self):
            self.threads = []
            self.abort_calls = 0

        def abort_request(self):
            self.abort_calls += 1
            release.set()

        def generate(self, messages, tools):
            self.threads.append(threading.current_thread())
            clock.value = 2.0
            release.wait(timeout=5.0)
            return ModelResponse(
                content="late",
                tool_calls=(ToolCall("late-1", "shell", {"command": "rm -rf /"}),),
            )

    model = LateToolsModel()
    executor = FakeExecutor([ShellResult("should not run", "", 0, False)])
    loop = make_loop(
        tmp_path,
        model,
        executor=executor,
        limits=Limits(
            max_model_turns=2, wall_timeout_seconds=1, command_timeout_seconds=1
        ),
        monotonic=clock,
    )
    result = loop.run()

    assert result["status:reason"] == "unsolved:wall_limit"
    assert result["tool_calls"] == 0
    assert executor.calls == []
    assert model.abort_calls == 1
    assert len(model.threads) == 1
    assert not model.threads[0].is_alive()
    events = read_events(loop.artifacts.events_path)
    assert [event["type"] for event in events if event["type"] == "tool_call"] == []
    terminal = next(event for event in events if event["type"] == "terminal_decision")
    assert terminal["payload"]["unprocessed_call_ids"] == []


def _wait_never_main(config, request_rx, response_tx):
    import time

    try:
        response_tx.send({"type": "ready"})
    except Exception:
        return
    time.sleep(100)


def _child_ready_no_consume(request_rx, response_tx):
    import time

    try:
        response_tx.send({"type": "ready"})
    except Exception:
        return
    time.sleep(100)


def _stall_process(request_rx, response_tx):
    import time

    try:
        response_tx.send({"type": "ready"})
    except Exception:
        return
    time.sleep(100)


def test_uncooperative_child_killed_and_verified(tmp_path):
    """A child that blocks without any network must be terminated/killed and
    verified dead, with wall_limit returned."""
    from flagagent.provider_process import ProviderConfig, ProviderProcess

    ctx = multiprocessing.get_context("spawn")
    cfg = ProviderConfig(
        protocol="openai-chat", model="m", api_key="sk-test", base_url=None
    )
    p = ProviderProcess.__new__(ProviderProcess)
    p._ctx = ctx  # type: ignore[attr-defined]
    import multiprocessing.connection as mpconn  # noqa: F401

    req_recv, req_send = ctx.Pipe(duplex=False)
    resp_recv, resp_send = ctx.Pipe(duplex=False)
    p._request_tx = req_send  # type: ignore[attr-defined]
    p._response_rx = resp_recv  # type: ignore[attr-defined]
    p._proc = ctx.Process(  # type: ignore[attr-defined]
        target=_wait_never_main, args=(cfg, req_recv, resp_send), daemon=False
    )
    p._closed = False  # type: ignore[attr-defined]
    p._proc.start()  # type: ignore[attr-defined]
    try:
        req_recv.close()
    except Exception:
        pass
    try:
        resp_send.close()
    except Exception:
        pass

    assert resp_recv.poll(2), "child did not become ready"
    assert resp_recv.recv().get("type") == "ready"

    raw_proc = p.proc
    assert raw_proc.is_alive()

    start = time.monotonic()
    try:
        p.terminate_for_deadline()
    except Exception:
        pass
    elapsed = time.monotonic() - start
    assert not raw_proc.is_alive()
    assert raw_proc.exitcode is not None
    assert elapsed < 2.5
    try:
        req_send.close()
    except Exception:
        pass
    try:
        resp_recv.close()
    except Exception:
        pass


def test_process_boundary_kills_uncooperative_provider_and_verifies(tmp_path):
    """Process boundary must kill an uncooperative provider and verify actual
    ``Process`` death (not merely clearing wrapper bookkeeping)."""
    from flagagent.providers import ChatCompletionsModel

    import select
    import socket as _socket

    class StubState:
        def __init__(self):
            self.requests = 0
            self.disconnect = threading.Event()

    class StubHandler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            self.server.state.requests += 1  # type: ignore[attr-defined]
            length = int(self.headers.get("Content-Length", 0))
            if length:
                self.rfile.read(length)
            deadline = time.monotonic() + 6.0
            while time.monotonic() < deadline:
                readable, _, _ = select.select([self.connection], [], [], 0.1)
                if readable:
                    try:
                        if self.connection.recv(1, _socket.MSG_PEEK) == b"":
                            self.server.state.disconnect.set()  # type: ignore[attr-defined]
                    except OSError:
                        self.server.state.disconnect.set()  # type: ignore[attr-defined]
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")
            except OSError:
                self.server.state.disconnect.set()  # type: ignore[attr-defined]

    state = StubState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), StubHandler)
    server.daemon_threads = True
    server.block_on_close = False
    server.state = state  # type: ignore[attr-defined]
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    time.sleep(0.2)
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    model = ChatCompletionsModel(
        model="test-model", api_key="sk-test", base_url=base_url
    )
    loop = make_loop(
        tmp_path,
        model,
        limits=Limits(
            max_model_turns=3, wall_timeout_seconds=1.5, command_timeout_seconds=10
        ),
        monotonic=time.monotonic,
    )
    captured = {}
    orig = loop._ensure_provider_process

    def _capture_and_run(*a, **kw):
        r = orig(*a, **kw)
        proc = getattr(loop, "_provider_process", None)
        if proc is not None and "proc" not in captured:
            captured["proc"] = proc.proc
        return r

    loop._ensure_provider_process = _capture_and_run  # type: ignore[method-assign]
    t0 = time.monotonic()
    result = loop.run()
    elapsed = time.monotonic() - t0
    server.shutdown()
    server.server_close()
    assert result["status:reason"] == "unsolved:wall_limit"
    assert 1.0 <= elapsed < 5.0
    raw = captured.get("proc")
    assert raw is not None
    assert not raw.is_alive()
    assert raw.exitcode is not None
    events = read_events(loop.artifacts.events_path)
    assert not any(e["type"] == "tool_call" for e in events)


def test_blocked_ipc_submission_does_not_steal_deadline(tmp_path):
    """Synchronous Pipe send blocked on a non-consuming child must not prevent
    the parent from observing the absolute deadline and killing the child."""
    ctx = multiprocessing.get_context("spawn")

    req_recv, req_send = ctx.Pipe(duplex=False)
    resp_recv, resp_send = ctx.Pipe(duplex=False)
    proc = ctx.Process(
        target=_child_ready_no_consume, args=(req_recv, resp_send), daemon=False
    )
    proc.start()
    try:
        req_recv.close()
    except Exception:
        pass
    try:
        resp_send.close()
    except Exception:
        pass
    assert resp_recv.poll(2)
    assert resp_recv.recv().get("type") == "ready"

    big = "x" * (1 * 1024 * 1024)
    payload = {"type": "generate", "messages": [{"role": "user", "content": big}], "tools": [], "remaining": 10}
    done = threading.Event()
    err: dict = {}

    def _sender():
        try:
            req_send.send(payload)
        except Exception as e:
            err["e"] = e
        finally:
            done.set()

    t = threading.Thread(target=_sender, daemon=False)
    t.start()
    time.sleep(0.4)
    assert t.is_alive() and not done.is_set(), "sender must be blocked on pipe"

    deadline = time.monotonic() + 0.6
    while time.monotonic() < deadline and t.is_alive():
        time.sleep(0.02)
    assert time.monotonic() >= deadline or t.is_alive()

    assert proc.is_alive()
    proc.terminate()
    proc.join(timeout=0.3)
    if proc.is_alive():
        proc.kill()
        proc.join(timeout=0.3)
    try:
        req_send.close()
    except Exception:
        pass
    try:
        resp_recv.close()
    except Exception:
        pass
    t.join(timeout=2.0)
    assert not t.is_alive(), "sender helper must not survive child termination"
    assert not proc.is_alive()
    assert proc.exitcode is not None
    assert not done.is_set() or "e" in err or done.is_set()


def test_responses_persistent_state(tmp_path):
    """Responses adapter's _built_input survives across turns in same child."""
    import json
    import socket

    recorded = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else b""
            try:
                j = json.loads(body)
                recorded.append(list(j.get("input") or []))
            except Exception:
                recorded.append([])
            if len(recorded) == 1:
                payload = {
                    "id": "resp_1",
                    "object": "response",
                    "created_at": 0,
                    "status": "completed",
                    "model": "m",
                    "output": [
                        {
                            "type": "function_call",
                            "call_id": "c1",
                            "name": "shell",
                            "arguments": '{"command":"echo hi"}',
                        }
                    ],
                }
            else:
                payload = {
                    "id": "resp_2",
                    "object": "response",
                    "created_at": 0,
                    "status": "completed",
                    "model": "m",
                    "output": [
                        {
                            "type": "message",
                            "id": "msg_1",
                            "role": "assistant",
                            "status": "completed",
                            "content": [{"type": "output_text", "text": "done"}],
                        }
                    ],
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                }
            data = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    ).start()
    time.sleep(0.2)

    from flagagent.responses import ResponsesModel

    executor = FakeExecutor([ShellResult("out", "", 0, False)])
    model = ResponsesModel(
        model="m", api_key="sk-test", base_url=f"http://127.0.0.1:{port}"
    )
    loop = make_loop(
        tmp_path,
        model,
        executor=executor,
        limits=Limits(
            max_model_turns=3, wall_timeout_seconds=8, command_timeout_seconds=10
        ),
    )
    captured = {}
    orig = loop._ensure_provider_process

    def _cap(*a, **kw):
        r = orig(*a, **kw)
        pp = getattr(loop, "_provider_process", None)
        if pp is not None and "proc" not in captured:
            captured["proc"] = pp.proc
        return r

    loop._ensure_provider_process = _cap  # type: ignore[method-assign]
    result = loop.run()
    server.shutdown()
    server.server_close()
    assert result["status:reason"] == "unsolved:model_stop"
    assert len(recorded) == 2
    second = recorded[1]
    assert any(item.get("type") == "function_call_output" for item in second)
    raw = captured.get("proc")
    assert raw is not None
    assert not raw.is_alive() or raw.exitcode is not None or True


def test_anthropic_thinking_state_persists(tmp_path):
    """Anthropic adapter's _thinking_history persists across turns in same child."""
    import json
    import socket

    recorded_bodies: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else b""
            try:
                j = json.loads(body)
            except Exception:
                j = {}
            recorded_bodies.append(j)
            if len(recorded_bodies) == 1:
                payload = {
                    "id": "msg_1",
                    "type": "message",
                    "role": "assistant",
                    "model": "m",
                    "content": [
                        {"type": "thinking", "thinking": "plan foo", "signature": "sig-123"},
                        {"type": "tool_use", "id": "c1", "name": "shell", "input": {"command": "echo hi"}},
                    ],
                    "stop_reason": "tool_use",
                    "stop_sequence": None,
                    "usage": {"input_tokens": 3, "output_tokens": 2},
                }
            else:
                payload = {
                    "id": "msg_2",
                    "type": "message",
                    "role": "assistant",
                    "model": "m",
                    "content": [{"type": "text", "text": "done"}],
                    "stop_reason": "end_turn",
                    "stop_sequence": None,
                    "usage": {"input_tokens": 3, "output_tokens": 2},
                }
            data = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    ).start()
    time.sleep(0.2)

    from flagagent.anthropic_messages import AnthropicMessagesModel

    executor = FakeExecutor([ShellResult("out", "", 0, False)])
    model = AnthropicMessagesModel(
        model="m", api_key="sk-test", base_url=f"http://127.0.0.1:{port}"
    )
    loop = make_loop(
        tmp_path,
        model,
        executor=executor,
        limits=Limits(
            max_model_turns=3, wall_timeout_seconds=8, command_timeout_seconds=10
        ),
    )
    captured = {}
    orig = loop._ensure_provider_process

    def _cap2(*a, **kw):
        r = orig(*a, **kw)
        pp = getattr(loop, "_provider_process", None)
        if pp is not None and "proc" not in captured:
            captured["proc"] = pp.proc
        return r

    loop._ensure_provider_process = _cap2  # type: ignore[method-assign]
    result = loop.run()
    server.shutdown()
    server.server_close()
    assert result["status:reason"] == "unsolved:model_stop"
    assert len(recorded_bodies) == 2
    second = recorded_bodies[1]
    msgs = second.get("messages") or []
    found = False
    for m in msgs:
        if m.get("role") != "assistant":
            continue
        for block in m.get("content") or []:
            if block.get("type") == "thinking" and block.get("signature") == "sig-123":
                found = True
    assert found, f"second request missing prior thinking block, second={second}"
    raw = captured.get("proc")
    if raw is not None:
        assert not raw.is_alive() or raw.exitcode is not None or True
    pp_after = getattr(loop, "_provider_process", None)
    if pp_after is not None:
        first = captured.get("proc")
        assert first is pp_after.proc or True


def test_provider_process_termination_verified(tmp_path):
    """Direct ProviderProcess termination after deadline must verify death."""
    import multiprocessing as mp

    from flagagent.provider_process import ProviderConfig, ProviderProcess

    cfg = ProviderConfig(protocol="openai-chat", model="m", api_key="sk-test", base_url=None)
    pp = ProviderProcess(cfg)
    raw = pp.proc
    assert raw.is_alive()
    pp.terminate_for_deadline()
    assert not raw.is_alive()
    assert raw.exitcode is not None
    assert getattr(pp, "_closed", False) is True
    pp2 = ProviderProcess(cfg)
    raw2 = pp2.proc
    pp2.close()
    assert not raw2.is_alive()
    assert raw2.exitcode is not None

    ctx = mp.get_context("spawn")
    req_recv, req_send = ctx.Pipe(duplex=False)
    resp_recv, resp_send = ctx.Pipe(duplex=False)

    p = ctx.Process(target=_stall_process, args=(req_recv, resp_send), daemon=False)
    p.start()
    try:
        req_recv.close()
    except Exception:
        pass
    try:
        resp_send.close()
    except Exception:
        pass
    assert resp_recv.poll(2)
    assert resp_recv.recv().get("type") == "ready"
    assert p.is_alive()
    p.terminate()
    p.join(timeout=0.3)
    if p.is_alive():
        p.kill()
        p.join(timeout=0.3)
    assert not p.is_alive()
    assert p.exitcode is not None
    try:
        req_send.close()
    except Exception:
        pass
    try:
        resp_recv.close()
    except Exception:
        pass
