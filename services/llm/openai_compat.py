import json
from typing import Any

from openai import AsyncOpenAI

from services.llm.base import LLMMessage, LLMResponse, ToolCall, ToolSpec


class OpenAICompatibleClient:
    """Adapter for OpenAI-compatible endpoints (Ollama Cloud, OpenAI, vLLM)."""

    def __init__(
        self, base_url: str, api_key: str, model: str, timeout: float | None = None, **defaults
    ):
        self._client = AsyncOpenAI(base_url=base_url, api_key=api_key or "ollama", timeout=timeout)
        self.model = model
        self.defaults = defaults

    def _to_openai(self, messages: list[LLMMessage]) -> list[dict]:
        out = []
        for m in messages:
            msg: dict[str, object] = {"role": m.role, "content": m.content or ""}
            if m.tool_calls:
                msg["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments),
                        },
                    }
                    for tc in m.tool_calls
                ]
            if m.tool_call_id:
                msg["tool_call_id"] = m.tool_call_id
            if m.name:
                msg["name"] = m.name
            out.append(msg)
        return out

    async def chat(
        self,
        messages: list[LLMMessage],
        tools: list[ToolSpec] | None = None,
    ) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": self._to_openai(messages),
            **self.defaults,
        }
        if tools:
            kwargs["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in tools
            ]
            kwargs["parallel_tool_calls"] = True
        resp = await self._client.chat.completions.create(**kwargs)
        msg = resp.choices[0].message
        reasoning = getattr(msg, "reasoning_content", None) or getattr(msg, "thinking", None)
        return LLMResponse(
            content=msg.content,
            tool_calls=[
                ToolCall(
                    id=tc.id,
                    name=tc.function.name,
                    arguments=json.loads(tc.function.arguments or "{}"),
                )
                for tc in (msg.tool_calls or [])
            ],
            reasoning=reasoning,
        )
