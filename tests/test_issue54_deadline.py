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


def test_provider_response_committed_before_deadline_preserved_despite_late_parent(
    tmp_path,
):
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

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    t = threading.Thread(
        target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    t.start()
    time.sleep(0.1)
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

    def cap(*a, **kw):
        r = orig_ensure(*a, **kw)
        pp = getattr(loop, "_provider_process", None)
        if pp is not None and "pp" not in holder:
            holder["pp"] = pp
        return r

    loop._ensure_provider_process = cap

    def watch_commit():
        for _ in range(100):
            if holder.get("pp") is not None:
                break
            time.sleep(0.05)
        pp = holder.get("pp")
        if pp is None:
            return
        cr = getattr(pp, "commit_rx", None)
        if cr is not None:
            if cr.poll(timeout=5):
                loop._deadline = time.monotonic() - 0.1
        else:
            rr = getattr(pp, "response_rx", None)
            if rr is not None and rr.poll(timeout=5):
                time.sleep(0.02)
                loop._deadline = time.monotonic() - 0.1

    bg = threading.Thread(target=watch_commit, daemon=True)
    bg.start()
    result = loop.run()
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
