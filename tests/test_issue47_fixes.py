"""Regression tests for issue 47: process boundary."""

import types

from flagagent.providers import ChatCompletionsModel
from flagagent.tools import TOOL_DEFINITIONS


def _dummy_messages():
    return [{"role": "user", "content": "hi"}]


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
    model.set_remaining(5)
    result = model.generate(_dummy_messages(), TOOL_DEFINITIONS)
    assert result.content == "ok"


# Abort-before-capture is now via process kill (see transport tests).
