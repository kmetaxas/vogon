"""JEV (Judgment/Evaluation/Verdict) API client for Ollama's /v1/systemone endpoint.

Provides structured judgment capabilities for admission gates and other
decision-making workflows where deterministic verdicts with confidence
scores are preferred over free-form chat completions.
"""

import json
from typing import Any, cast

import httpx

from services.llm.base import LLMMessage, LLMResponse, ToolSpec


class JEVClient:
    """Client for Ollama's /v1/systemone structured judgment endpoint.

    This endpoint is NOT OpenAI-compatible. It accepts a 'state' string
    and a set of 'questions' with criteria, returning structured 'answers'
    with choices, probabilities, and confidence scores.
    """

    def __init__(self, base_url: str, model: str, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self._client = httpx.AsyncClient(timeout=timeout)

    async def judge(
        self,
        state: str,
        questions: dict[str, dict],
    ) -> dict[str, Any]:
        """Submit a judgment request.

        Args:
            state: The situation/context to evaluate (e.g., alert summary)
            questions: Dict of question definitions. Each question must have:
                - type: "choice" (or "number", "boolean")
                - instructions: str describing what to evaluate
                - criteria: dict of possible choices (for choice type)

        Returns:
            The raw JEV response dict with "answers", "model", "usage"
        """
        payload = {
            "model": self.model,
            "state": state,
            "questions": questions,
        }

        url = f"{self.base_url}/v1/systemone"
        response = await self._client.post(url, json=payload)
        response.raise_for_status()
        return cast(dict[str, Any], response.json())

    async def evaluate_admission(
        self,
        context: str,
    ) -> LLMResponse:
        """Admission gate specific wrapper.

        Asks a single "admit_or_suppress" question and converts the
        JEV response to our standard LLMResponse format.
        """
        result = await self.judge(
            state=context,
            questions={
                "admit_or_suppress": {
                    "type": "choice",
                    "instructions": "Should we START an investigation or SUPPRESS this event?",
                    "criteria": {
                        "START": "Novel, actionable alert that warrants investigation",
                        "SUPPRESS": "Duplicate, low-value, or already-covered alert",
                    },
                }
            },
        )

        answer = result["answers"]["admit_or_suppress"]
        choice = answer["choice"]
        confidence = answer.get("confidence", 0.5)

        # Map to our standard LLMResponse
        verdict = "START" if choice == "START" else "SUPPRESS_LOW_VALUE"

        return LLMResponse(
            content=json.dumps(
                {
                    "decision": verdict,
                    "reason": f"JEV model judged this alert with confidence {confidence}",
                    "confidence": confidence,
                }
            ),
            tool_calls=[],
            reasoning=f"JEV probabilities: {json.dumps(answer.get('probabilities', {}))}",
            input_tokens=result.get("usage", {}).get("input_tokens", 0),
            output_tokens=result.get("usage", {}).get("output_tokens", 0),
            model=result.get("model", self.model),
        )

    async def chat(
        self,
        messages: list[LLMMessage],
        tools: list[ToolSpec] | None = None,
    ) -> LLMResponse:
        """Wrap evaluate_admission so ``call_llm`` works with both clients.

        Extracts the last user message content as the *context* to judge.
        Non-user messages are ignored for JEV evaluation.
        """
        context_parts = [m.content or "" for m in messages if m.role in ("user", "system")]
        context = "\n".join(context_parts).strip()
        if not context:
            context = "No context provided."
        return await self.evaluate_admission(context)

    async def close(self) -> None:
        await self._client.aclose()
