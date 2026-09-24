from dataclasses import dataclass
from typing import Protocol


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict  # JSON Schema


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class LLMMessage:
    role: str  # system | user | assistant | tool
    content: str | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None
    name: str | None = None


@dataclass
class LLMResponse:
    content: str | None
    tool_calls: list[ToolCall]
    reasoning: str | None = None


class LLMClient(Protocol):
    async def chat(
        self,
        messages: list[LLMMessage],
        tools: list[ToolSpec] | None = None,
    ) -> LLMResponse: ...
