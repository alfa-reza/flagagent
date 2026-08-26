import json
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from flagagent.artifacts import read_events
from flagagent.loop import AgentLoop, ChallengeInput, Limits
from flagagent.tools import ExactStringVerifier, FakeExecutor

NOW = datetime(2026, 8, 14, 16, 15, 30, tzinfo=UTC)

CHAT_PAYLOAD = json.dumps(
    {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 0,
        "model": "m",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "done",
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {
                                "name": "shell",
                                "arguments": '{"command":"echo hi"}',
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 11, "completion_tokens": 22},
    }
).encode()


def _start_server(handler_cls):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    srv.daemon_threads = True
    t = threading.Thread(
        target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    t.start()
    time.sleep(0.05)
    return srv


def test_provider_response_committed_before_deadline_preserved_despite_late_parent(
    tmp_path,
):
    """A: commit demonstrably before deadline (commit pipe) survives late parent.

    Synchronization: child commit_tx.send only after response_tx.send returns.
    Parent commit_rx readiness therefore proves full response completion, not
    bare pipe readability. Watcher waits for commit_rx.poll (not consumed) then
    makes parent monotonic report expired while keeping absolute deadline
    unchanged; committed_at < deadline holds, late scheduling must preserve.
    """

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            l = int(self.headers.get("Content-Length", 0))
            if l:
                self.rfile.read(l)
            time.sleep(0.05)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(CHAT_PAYLOAD)))
            self.end_headers()
            self.wfile.write(CHAT_PAYLOAD)

    srv = _start_server(H)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    from flagagent.providers import ChatCompletionsModel

    model = ChatCompletionsModel(model="m", api_key="sk-test", base_url=base)
    loop = AgentLoop(
        model=model,
        executor=FakeExecutor([]),
        verifier=ExactStringVerifier("Flag{ok}"),
        challenge=ChallengeInput("fixture", "solve"),
        limits=Limits(
            max_model_turns=3, wall_timeout_seconds=5, command_timeout_seconds=10
        ),
        runs_root=tmp_path,
        monotonic=time.monotonic,
        utc_now=lambda: NOW,
        run_id="FA-20260814T161530Z-a13f4c2d",
    )
    orig_ensure = loop._ensure_provider_process
    holder = {}
    real_monotonic = loop.monotonic

    def cap(*a, **kw):
        r = orig_ensure(*a, **kw)
        pp = getattr(loop, "_provider_process", None)
        if pp is not None and "pp" not in holder:
            holder["pp"] = pp
        return r

    loop._ensure_provider_process = cap
    deadline_holder = {}

    def watch_commit():
        for _ in range(100):
            if holder.get("pp") is not None:
                break
            time.sleep(0.05)
        pp = holder.get("pp")
        if pp is None:
            return
        deadline_holder["deadline"] = getattr(loop, "_deadline", None)
        cr = getattr(pp, "commit_rx", None)
        if cr is not None:
            if cr.poll(timeout=5):
                dl = getattr(loop, "_deadline", None)
                if dl is not None:
                    loop.monotonic = lambda: dl + 1.0
        else:
            rr = getattr(pp, "response_rx", None)
            if rr is not None and rr.poll(timeout=5):
                dl = getattr(loop, "_deadline", None)
                if dl is not None:
                    loop.monotonic = lambda: dl + 1.0

    bg = threading.Thread(target=watch_commit, daemon=True)
    bg.start()
    result = loop.run()
    try:
        loop.monotonic = real_monotonic
    except Exception:
        pass
    srv.shutdown()
    srv.server_close()
    assert result["status:reason"] == "unsolved:wall_limit"
    assert result["model_calls"] == 1
    assert result.get("input_tokens") == 11
    assert result.get("output_tokens") == 22
    events = read_events(loop.artifacts.events_path)
    mrs = [e for e in events if e["type"] == "model_response"]
    assert len(mrs) == 1
    assert mrs[0]["payload"]["tool_calls"][0]["call_id"] == "c1"
    assert any(m["role"] == "assistant" for m in loop.messages)
    terminal = next(e for e in events if e["type"] == "terminal_decision")
    assert terminal["payload"]["unprocessed_call_ids"] == ["c1"]
    assert not any(e["type"] == "tool_call" for e in events)
    assert loop._tool_calls == 0
    pp = holder.get("pp")
    if pp is not None:
        assert not pp.is_alive()
        assert pp.exitcode is not None


def test_post_deadline_completion_not_preserved(tmp_path):
    """B: response completing after deadline must be rejected via committed_at.

    Establishes D in real monotonic domain, blocks handler until D has passed,
    keeps parent from terminating before commit (frozen monotonic), proves
    commit marker exists with committed_at >= D before allowing parent
    expiration observation.
    """

    handler_started = threading.Event()
    handler_release = threading.Event()

    class SlowH(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            l = int(self.headers.get("Content-Length", 0))
            if l:
                self.rfile.read(l)
            handler_started.set()
            handler_release.wait(timeout=5)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(CHAT_PAYLOAD)))
            self.end_headers()
            try:
                self.wfile.write(CHAT_PAYLOAD)
            except Exception:
                pass

    srv = _start_server(SlowH)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    from flagagent.providers import ChatCompletionsModel

    model = ChatCompletionsModel(model="m", api_key="sk-test", base_url=base)
    loop = AgentLoop(
        model=model,
        executor=FakeExecutor([]),
        verifier=ExactStringVerifier("Flag{ok}"),
        challenge=ChallengeInput("fixture", "solve"),
        limits=Limits(
            max_model_turns=3, wall_timeout_seconds=0.8, command_timeout_seconds=10
        ),
        runs_root=tmp_path,
        monotonic=time.monotonic,
        utc_now=lambda: NOW,
        run_id="FA-20260814T161530Z-b13f4c2d",
    )
    real_monotonic = loop.monotonic
    holder = {}
    orig = loop._ensure_provider_process

    def cap(*a, **kw):
        r = orig(*a, **kw)
        pp = getattr(loop, "_provider_process", None)
        if pp is not None and "pp" not in holder:
            holder["pp"] = pp
            holder["proc"] = pp.proc
        return r

    loop._ensure_provider_process = cap

    def watch():
        for _ in range(100):
            if holder.get("pp") is not None:
                break
            time.sleep(0.02)
        for _ in range(100):
            if hasattr(loop, "_deadline"):
                break
            time.sleep(0.02)
        pp = holder.get("pp")
        deadline = getattr(loop, "_deadline", None)
        if pp is None or deadline is None:
            return
        loop.monotonic = lambda: deadline - 0.5
        handler_started.wait(timeout=5)
        while real_monotonic() < deadline + 0.2:
            time.sleep(0.02)
        handler_release.set()
        cr = getattr(pp, "commit_rx", None)
        if cr is not None:
            cr.poll(timeout=5)
        loop.monotonic = lambda: deadline + 1.0

    bg = threading.Thread(target=watch, daemon=True)
    bg.start()
    t0 = real_monotonic()
    result = loop.run()
    elapsed = real_monotonic() - t0
    try:
        loop.monotonic = real_monotonic
    except Exception:
        pass
    srv.shutdown()
    srv.server_close()
    assert result["status:reason"] == "unsolved:wall_limit"
    assert result["model_calls"] == 1
    assert result["tool_calls"] == 0
    assert elapsed < 5.0
    events = read_events(loop.artifacts.events_path)
    assert not any(e["type"] == "model_response" for e in events)
    assert not any(e["type"] == "tool_call" for e in events)
    terminal = next(e for e in events if e["type"] == "terminal_decision")
    assert terminal["payload"]["unprocessed_call_ids"] == []
    assert loop._tool_calls == 0
    assert not any(e["type"] == "tool_call" for e in events)
    assert loop._input_tokens is None
    assert loop._output_tokens is None
    pp = holder.get("pp")
    if pp is not None:
        assert not pp.is_alive()
        assert pp.exitcode is not None
    raw = holder.get("proc")
    if raw is not None:
        assert not raw.is_alive()
        assert raw.exitcode is not None
    assert handler_started.is_set()
    assert handler_release.is_set()


def test_provider_error_before_deadline_preserved(tmp_path):
    """C: real provider failure before deadline remains provider_error.

    Uses real ProviderProcess + local HTTP stub returning 500 quickly.
    Failure commit occurs with committed_at < deadline, so parent must return
    provider_error, not wall_limit, and not swallow committed message.
    """

    class ErrH(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            l = int(self.headers.get("Content-Length", 0))
            if l:
                self.rfile.read(l)
            body = json.dumps(
                {"error": {"message": "boom", "type": "server_error"}}
            ).encode()
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = _start_server(ErrH)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    from flagagent.providers import ChatCompletionsModel

    model = ChatCompletionsModel(model="m", api_key="sk-test", base_url=base)
    loop = AgentLoop(
        model=model,
        executor=FakeExecutor([]),
        verifier=ExactStringVerifier("Flag{ok}"),
        challenge=ChallengeInput("fixture", "solve"),
        limits=Limits(
            max_model_turns=3, wall_timeout_seconds=5, command_timeout_seconds=10
        ),
        runs_root=tmp_path,
        monotonic=time.monotonic,
        utc_now=lambda: NOW,
        run_id="FA-20260814T161530Z-c13f4c2d",
    )
    holder = {}
    orig = loop._ensure_provider_process

    def cap(*a, **kw):
        r = orig(*a, **kw)
        pp = getattr(loop, "_provider_process", None)
        if pp is not None and "pp" not in holder:
            holder["pp"] = pp
        return r

    loop._ensure_provider_process = cap
    result = loop.run()
    srv.shutdown()
    srv.server_close()
    assert result["status:reason"] == "error:provider_error"
    assert result["model_calls"] == 1
    assert loop._tool_calls == 0
    events = read_events(loop.artifacts.events_path)
    assert not any(e["type"] == "model_response" for e in events)
    assert any(
        e["type"] == "error" and e["payload"]["reason"] == "provider_error"
        for e in events
    )
    pp = holder.get("pp")
    if pp is not None:
        assert not pp.is_alive()
        assert pp.exitcode is not None
