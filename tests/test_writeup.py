import json

from flagagent.loop import AgentLoop, ChallengeInput, Limits
from flagagent.model import ModelResponse, ScriptedModel, ToolCall
from flagagent.tools import ExactStringVerifier, FakeExecutor, ShellResult
from flagagent.writeup import write_writeup


def run_solved(tmp_path):
    model = ScriptedModel(
        [
            ModelResponse(
                content="inspect",
                tool_calls=(ToolCall("s", "shell", {"command": "cat evidence"}),),
                usage={"input_tokens": 4, "output_tokens": 2},
            ),
            ModelResponse(
                tool_calls=(ToolCall("f", "submit_flag", {"candidate": "Flag{ok}"}),),
                usage={"input_tokens": 3, "output_tokens": 1},
            ),
        ]
    )
    loop = AgentLoop(
        model=model,
        executor=FakeExecutor([ShellResult("evidence", "", 0, False)]),
        verifier=ExactStringVerifier("Flag{ok}"),
        challenge=ChallengeInput("fixture", "solve it"),
        limits=Limits(
            max_model_turns=5, wall_timeout_seconds=100, command_timeout_seconds=10
        ),
        runs_root=tmp_path,
        monotonic=lambda: 0,
        utc_now=lambda: __import__("datetime").datetime.now(__import__("datetime").UTC),
        run_id="FA-20260817T000000Z-writeup",
        model_identity="test-model",
        protocol="openai-chat",
    )
    loop.run()
    return loop


def test_writeup_is_derived_from_structured_artifacts(tmp_path):
    loop = run_solved(tmp_path)

    path = write_writeup(loop.artifacts.directory)

    text = path.read_text()
    assert "# FlagAgent Run" in text
    assert "fixture" in text
    assert "solved" in text
    assert "Flag{ok}" in text
    assert "cat evidence" in text
    assert "Input tokens: `7`" in text
    assert "Output tokens: `3`" in text
    assert json.loads(loop.artifacts.result_path.read_text())["status"] == "solved"


def test_writeup_does_not_include_expected_flag_or_credentials(tmp_path):
    loop = run_solved(tmp_path)
    run_json = json.loads(loop.artifacts.run_path.read_text())
    run_json["model"] = {
        "name": "model",
        "protocol": "openai-chat",
        "base_url": "https://example.test",
    }
    loop.artifacts.run_path.write_text(json.dumps(run_json))

    text = write_writeup(loop.artifacts.directory).read_text()

    assert "api_key" not in text
    assert "secret" not in text
    assert "expected_flag" not in text


def test_writeup_is_atomic_and_replaced_on_repeat(tmp_path):
    loop = run_solved(tmp_path)

    first = write_writeup(loop.artifacts.directory)
    first_text = first.read_text()
    second = write_writeup(loop.artifacts.directory)

    assert second == first
    assert second.read_text() == first_text
    assert not list(loop.artifacts.directory.glob(".writeup.md.*"))


def test_writeup_escapes_shell_command_backticks(tmp_path):
    evil = "echo `evil` && echo hi` # HACKED [evil](http://evil) - list"
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=(ToolCall("c1", "shell", {"command": evil}),),
                usage={"input_tokens": 1, "output_tokens": 1},
            ),
        ]
    )
    loop = AgentLoop(
        model=model,
        executor=FakeExecutor([ShellResult("", "", 0, False)]),
        verifier=ExactStringVerifier("Flag{ok}"),
        challenge=ChallengeInput("fixture", "solve it"),
        limits=Limits(
            max_model_turns=5, wall_timeout_seconds=100, command_timeout_seconds=10
        ),
        runs_root=tmp_path,
        monotonic=lambda: 0,
        utc_now=lambda: __import__("datetime").datetime.now(__import__("datetime").UTC),
        run_id="FA-20260817T000000Z-shell-escape",
        model_identity="test-model",
        protocol="openai-chat",
    )
    loop.run()
    text = write_writeup(loop.artifacts.directory).read_text()
    from flagagent.writeup import _code_span

    assert _code_span(evil) in text
    assert "`evil\\` " not in text
    lines = text.splitlines()
    assert not any(
        line.strip() == "# HACKED [evil](http://evil) - list" for line in lines
    )
    assert not any(line.lstrip().startswith("# HACKED") for line in lines)
    assert lines.count("# FlagAgent Run") == 1


def test_writeup_escapes_flag_candidate_backticks(tmp_path):
    evil = "Flag{`evil`} # HACKED [evil](http://evil) - list"
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=(ToolCall("f1", "submit_flag", {"candidate": evil}),),
                usage={"input_tokens": 1, "output_tokens": 1},
            ),
        ]
    )
    loop = AgentLoop(
        model=model,
        executor=FakeExecutor([]),
        verifier=ExactStringVerifier("Flag{ok}"),
        challenge=ChallengeInput("fixture", "solve it"),
        limits=Limits(
            max_model_turns=5, wall_timeout_seconds=100, command_timeout_seconds=10
        ),
        runs_root=tmp_path,
        monotonic=lambda: 0,
        utc_now=lambda: __import__("datetime").datetime.now(__import__("datetime").UTC),
        run_id="FA-20260817T000000Z-flag-escape",
        model_identity="test-model",
        protocol="openai-chat",
    )
    loop.run()
    text = write_writeup(loop.artifacts.directory).read_text()
    from flagagent.writeup import _code_span

    assert _code_span(evil) in text
    lines = text.splitlines()
    assert not any(line.lstrip().startswith("# HACKED") for line in lines)


def test_writeup_escapes_tool_name_and_call_id_backticks(tmp_path):
    evil_name = "tool`name` # HACKED"
    evil_call = "call`id` [evil](http://evil) - list"
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=(ToolCall(evil_call, evil_name, {"x": 1}),),
                usage={"input_tokens": 1, "output_tokens": 1},
            ),
        ]
    )
    loop = AgentLoop(
        model=model,
        executor=FakeExecutor([]),
        verifier=ExactStringVerifier("Flag{ok}"),
        challenge=ChallengeInput("fixture", "solve it"),
        limits=Limits(
            max_model_turns=5, wall_timeout_seconds=100, command_timeout_seconds=10
        ),
        runs_root=tmp_path,
        monotonic=lambda: 0,
        utc_now=lambda: __import__("datetime").datetime.now(__import__("datetime").UTC),
        run_id="FA-20260817T000000Z-tool-escape",
        model_identity="test-model",
        protocol="openai-chat",
    )
    loop.run()
    text = write_writeup(loop.artifacts.directory).read_text()
    from flagagent.writeup import _code_span

    assert _code_span(evil_name) in text
    assert _code_span(evil_call) in text
    lines = text.splitlines()
    assert not any(line.lstrip().startswith("# HACKED") for line in lines)
    assert "[evil](http://evil)" in text


def test_writeup_benign_values_remain_readable(tmp_path):
    loop = run_solved(tmp_path)
    text = write_writeup(loop.artifacts.directory).read_text()
    assert "`cat evidence`" in text
    assert "`Flag{ok}`" in text
    assert "`s`" in text
    assert "`shell` call" in text


def test_code_span_delimiter_selection():
    from flagagent.writeup import _code_span

    assert _code_span("a`b") == "``a`b``"
    assert _code_span("a``b") == "```a``b```"
    assert _code_span("`leading") == "`` `leading ``"
    assert _code_span("trailing`") == "`` trailing` ``"
    assert _code_span("`both`") == "`` `both` ``"
    assert _code_span("no ticks") == "`no ticks`"
    assert _code_span("") == "``"
