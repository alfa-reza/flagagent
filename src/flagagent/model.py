import json
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Protocol


def _snapshot_json(value: Any) -> Any:
    try:
        encoded = json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise TypeError("value must be strict JSON") from error
    return json.loads(encoded)


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.call_id, str) or not self.call_id:
            raise ValueError("call_id must be a non-empty string")
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("name must be a non-empty string")
        if not isinstance(self.arguments, Mapping):
            raise TypeError("arguments must be an object")
        object.__setattr__(self, "arguments", _snapshot_json(dict(self.arguments)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "name": self.name,
            "arguments": deepcopy(self.arguments),
        }


@dataclass(frozen=True)
class ModelResponse:
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    usage: Any | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.content, str):
            raise TypeError("content must be a string")
        if not isinstance(self.tool_calls, Sequence):
            raise TypeError("tool_calls must be a sequence")
        calls = tuple(self.tool_calls)
        if not all(isinstance(call, ToolCall) for call in calls):
            raise TypeError("tool_calls must contain ToolCall values")
        object.__setattr__(self, "tool_calls", calls)
        if self.usage is not None:
            object.__setattr__(self, "usage", _snapshot_json(self.usage))

    def to_dict(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "tool_calls": [call.to_dict() for call in self.tool_calls],
            "usage": deepcopy(self.usage),
        }


class Model(Protocol):
    def generate(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelResponse: ...


@dataclass
class ScriptedModel:
    script: Sequence[ModelResponse | Exception]
    calls: list[tuple[list[dict[str, Any]], list[dict[str, Any]]]] = field(
        default_factory=list, init=False
    )
    _index: int = field(default=0, init=False)

    def generate(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelResponse:
        self.calls.append((_snapshot_json(messages), _snapshot_json(tools)))
        if self._index >= len(self.script):
            raise RuntimeError("scripted model exhausted")
        item = self.script[self._index]
        self._index += 1
        if isinstance(item, Exception):
            raise item
        if not isinstance(item, ModelResponse):
            raise TypeError("script entry must be a ModelResponse or Exception")
        return ModelResponse(
            content=item.content,
            tool_calls=tuple(
                ToolCall(call.call_id, call.name, call.arguments)
                for call in item.tool_calls
            ),
            usage=item.usage,
        )
