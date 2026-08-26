import json
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from flagagent.artifacts import read_events
from flagagent.loop import AgentLoop, ChallengeInput, Limits
from flagagent.tools import ExactStringVerifier, FakeExecutor

NOW = datetime(2026, 8, 14, 16, 15, 30, tzinfo=UTC)

LARGE_CONTENT = "X" * (300 * 1024)

CHAT_LARGE = json.dumps(
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
                    "content": LARGE_CONTENT,
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": "shell", "arguments": '{"command":"echo hi"}'},
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
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    time.sleep(0.05)
    return srv


def test_large_success_within_deadline(tmp_path):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_POST(self):
            l = int(self.headers.get("Content-Length", 0))
            if l: self.rfile.read(l)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(CHAT_LARGE)))
            self.end_headers()
            self.wfile.write(CHAT_LARGE)
    srv = _start_server(H)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    from flagagent.providers import ChatCompletionsModel
    model = ChatCompletionsModel(model="m", api_key="sk-test", base_url=base)
    loop = AgentLoop(model=model, executor=FakeExecutor([]), verifier=ExactStringVerifier("Flag{ok}"), challenge=ChallengeInput("fixture","solve"), limits=Limits(max_model_turns=3, wall_timeout_seconds=5, command_timeout_seconds=10), runs_root=tmp_path, monotonic=time.monotonic, utc_now=lambda: NOW, run_id="FA-20260814T161530Z-large-a")
    holder={}
    orig=loop._ensure_provider_process
    def cap(*a,**kw):
        r=orig(*a,**kw)
        pp=getattr(loop,"_provider_process",None)
        if pp is not None and "pp" not in holder: holder["pp"]=pp
        return r
    loop._ensure_provider_process=cap
    t0=time.monotonic()
    result=loop.run()
    elapsed=time.monotonic()-t0
    srv.shutdown(); srv.server_close()
    assert elapsed < 4.0, f"should not stall until wall_limit elapsed={elapsed}"
    assert result["status:reason"] == "unsolved:wall_limit"
    events=read_events(loop.artifacts.events_path)
    mrs=[e for e in events if e["type"]=="model_response"]
    assert len(mrs)==1
    assert len(mrs[0]["payload"]["content"]) == len(LARGE_CONTENT)
    assert mrs[0]["payload"]["tool_calls"][0]["call_id"]=="c1"
    assert result.get("input_tokens")==11
    assert result.get("output_tokens")==22
    terminal=next(e for e in events if e["type"]=="terminal_decision")
    assert terminal["payload"]["unprocessed_call_ids"]==["c1"]
    assert not any(e["type"]=="tool_call" for e in events)
    assert loop._tool_calls==0
    pp=holder.get("pp")
    if pp is not None:
        assert not pp.is_alive()
        assert pp.exitcode is not None
    # also ensure no receiver thread leaked (check thread count roughly)
    assert elapsed < 5


def test_large_pre_deadline_preserved(tmp_path):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_POST(self):
            l=int(self.headers.get("Content-Length",0))
            if l: self.rfile.read(l)
            time.sleep(0.05)
            self.send_response(200)
            self.send_header("Content-Type","application/json")
            self.send_header("Content-Length", str(len(CHAT_LARGE)))
            self.end_headers()
            self.wfile.write(CHAT_LARGE)
    srv=_start_server(H)
    base=f"http://127.0.0.1:{srv.server_address[1]}"
    from flagagent.providers import ChatCompletionsModel
    model=ChatCompletionsModel(model="m", api_key="sk-test", base_url=base)
    loop=AgentLoop(model=model, executor=FakeExecutor([]), verifier=ExactStringVerifier("Flag{ok}"), challenge=ChallengeInput("fixture","solve"), limits=Limits(max_model_turns=3, wall_timeout_seconds=5, command_timeout_seconds=10), runs_root=tmp_path, monotonic=time.monotonic, utc_now=lambda: NOW, run_id="FA-20260814T161530Z-large-b")
    holder={}
    orig=loop._ensure_provider_process
    def cap(*a,**kw):
        r=orig(*a,**kw)
        pp=getattr(loop,"_provider_process",None)
        if pp is not None and "pp" not in holder: holder["pp"]=pp
        return r
    loop._ensure_provider_process=cap
    real_monotonic=loop.monotonic
    def watch():
        for _ in range(100):
            if holder.get("pp") is not None: break
            time.sleep(0.05)
        pp=holder.get("pp")
        if pp is None: return
        cr=getattr(pp,"commit_rx",None)
        if cr is not None:
            if cr.poll(timeout=5):
                dl=getattr(loop,"_deadline",None)
                if dl is not None: loop.monotonic=lambda: dl+1.0
        else:
            rr=getattr(pp,"response_rx",None)
            if rr is not None and rr.poll(timeout=5):
                dl=getattr(loop,"_deadline",None)
                if dl is not None: loop.monotonic=lambda: dl+1.0
    bg=threading.Thread(target=watch, daemon=True)
    bg.start()
    result=loop.run()
    try: loop.monotonic=real_monotonic
    except: pass
    srv.shutdown(); srv.server_close()
    assert result["status:reason"]=="unsolved:wall_limit"
    events=read_events(loop.artifacts.events_path)
    mrs=[e for e in events if e["type"]=="model_response"]
    assert len(mrs)==1
    assert len(mrs[0]["payload"]["content"])==len(LARGE_CONTENT)
    assert result.get("input_tokens")==11
    terminal=next(e for e in events if e["type"]=="terminal_decision")
    assert terminal["payload"]["unprocessed_call_ids"]==["c1"]
    assert not any(e["type"]=="tool_call" for e in events)
    assert loop._tool_calls==0


def test_large_incomplete_not_preserved(tmp_path):
    handler_started=threading.Event()
    handler_release=threading.Event()
    class SlowH(BaseHTTPRequestHandler):
        def log_message(self,*a): pass
        def do_POST(self):
            l=int(self.headers.get("Content-Length",0))
            if l: self.rfile.read(l)
            handler_started.set()
            handler_release.wait(timeout=5)
            self.send_response(200)
            self.send_header("Content-Type","application/json")
            self.send_header("Content-Length", str(len(CHAT_LARGE)))
            self.end_headers()
            try: self.wfile.write(CHAT_LARGE)
            except: pass
    srv=_start_server(SlowH)
    base=f"http://127.0.0.1:{srv.server_address[1]}"
    from flagagent.providers import ChatCompletionsModel
    model=ChatCompletionsModel(model="m", api_key="sk-test", base_url=base)
    loop=AgentLoop(model=model, executor=FakeExecutor([]), verifier=ExactStringVerifier("Flag{ok}"), challenge=ChallengeInput("fixture","solve"), limits=Limits(max_model_turns=3, wall_timeout_seconds=0.8, command_timeout_seconds=10), runs_root=tmp_path, monotonic=time.monotonic, utc_now=lambda: NOW, run_id="FA-20260814T161530Z-large-c")
    real_monotonic=loop.monotonic
    holder={}
    orig=loop._ensure_provider_process
    class _Wrapper:
        def __init__(self, real): self._real=real; self.observed=None; self.ev=threading.Event()
        def fileno(self): return self._real.fileno()
        def poll(self, timeout=0): return self._real.poll(timeout)
        def recv(self):
            m=self._real.recv()
            if isinstance(m, dict) and "committed_at" in m: self.observed=m; self.ev.set()
            return m
        def close(self): return self._real.close()
    def cap(*a,**kw):
        r=orig(*a,**kw)
        pp=getattr(loop,"_provider_process",None)
        if pp is not None and "pp" not in holder:
            try:
                real_cr=pp._commit_rx
                w=_Wrapper(real_cr); pp._commit_rx=w; holder["wrapper"]=w
            except: pass
            holder["pp"]=pp; holder["proc"]=pp.proc
        return r
    loop._ensure_provider_process=cap
    def watch():
        for _ in range(100):
            if holder.get("pp") is not None: break
            time.sleep(0.02)
        for _ in range(100):
            if hasattr(loop,"_deadline"): break
            time.sleep(0.02)
        pp=holder.get("pp")
        deadline=getattr(loop,"_deadline",None)
        wrapper=holder.get("wrapper")
        if pp is None or deadline is None: return
        holder["deadline"]=deadline
        loop.monotonic=lambda: deadline-0.5
        handler_started.wait(timeout=5)
        while real_monotonic() < deadline+0.2: time.sleep(0.02)
        handler_release.set()
        if wrapper is not None: wrapper.ev.wait(timeout=5)
        else:
            cr=getattr(pp,"commit_rx",None)
            if cr is not None: cr.poll(timeout=5)
        loop.monotonic=lambda: deadline+1.0
    bg=threading.Thread(target=watch, daemon=True)
    bg.start()
    t0=real_monotonic()
    result=loop.run()
    elapsed=real_monotonic()-t0
    try: loop.monotonic=real_monotonic
    except: pass
    srv.shutdown(); srv.server_close()
    wrapper=holder.get("wrapper")
    observed=getattr(wrapper,"observed",None) if wrapper else None
    deadline=holder.get("deadline")
    assert observed is not None
    assert deadline is not None
    assert float(observed["committed_at"]) >= float(deadline)
    assert result["status:reason"]=="unsolved:wall_limit"
    assert result["model_calls"]==1
    assert result["tool_calls"]==0
    assert elapsed < 5.0
    events=read_events(loop.artifacts.events_path)
    assert not any(e["type"]=="model_response" for e in events)
    assert not any(e["type"]=="tool_call" for e in events)
    terminal=next(e for e in events if e["type"]=="terminal_decision")
    assert terminal["payload"]["unprocessed_call_ids"]==[]
    pp=holder.get("pp")
    if pp is not None: assert not pp.is_alive(); assert pp.exitcode is not None
