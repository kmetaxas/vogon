from unittest.mock import AsyncMock, MagicMock, patch

from django.test import TestCase

from services.llm.base import LLMMessage, LLMResponse
from services.llm.jev_client import JEVClient


def _jev_response(choice: str, probabilities: dict, confidence: float, input_tokens: int):
    resp = MagicMock()
    resp.json.return_value = {
        "model": "nimble",
        "answers": {
            "admit_or_suppress": {
                "type": "choice",
                "choice": choice,
                "probabilities": probabilities,
                "confidence": confidence,
            }
        },
        "usage": {"input_tokens": input_tokens, "output_tokens": 1},
    }
    return resp


class JEVClientTests(TestCase):
    async def test_judge_posts_to_systemone_endpoint(self):
        client = JEVClient(base_url="http://localhost:11434", model="nimble")
        resp = MagicMock()
        resp.json.return_value = {
            "model": "nimble",
            "answers": {
                "label": {
                    "type": "choice",
                    "choice": "bug",
                    "probabilities": {"billing": 0.011, "bug": 0.979, "account": 0.010},
                    "confidence": 0.892,
                }
            },
            "usage": {"input_tokens": 168, "output_tokens": 1},
        }

        with patch.object(client._client, "post", new_callable=AsyncMock, return_value=resp):
            result = await client.judge(
                state="Our checkout has returned 500 errors since 9am.",
                questions={
                    "label": {
                        "type": "choice",
                        "instructions": "Which label fits this ticket?",
                        "criteria": {"billing": None, "bug": None, "account": None},
                    }
                },
            )

        self.assertEqual(result["answers"]["label"]["choice"], "bug")
        self.assertEqual(result["usage"]["input_tokens"], 168)

    async def test_evaluate_admission_returns_llm_response(self):
        client = JEVClient(base_url="http://localhost:11434", model="nimble")
        resp = _jev_response("START", {"START": 0.95, "SUPPRESS": 0.05}, 0.95, 100)

        with patch.object(client._client, "post", new_callable=AsyncMock, return_value=resp):
            result = await client.evaluate_admission("High CPU alert on prod-db-01")

        self.assertIsInstance(result, LLMResponse)
        self.assertIn("START", result.content or "")
        self.assertEqual(result.input_tokens, 100)
        self.assertEqual(result.output_tokens, 1)
        self.assertEqual(result.model, "nimble")

    async def test_chat_extracts_context_from_messages(self):
        client = JEVClient(base_url="http://localhost:11434", model="nimble")
        resp = _jev_response("SUPPRESS", {"START": 0.1, "SUPPRESS": 0.9}, 0.9, 50)

        with patch.object(client._client, "post", new_callable=AsyncMock, return_value=resp):
            messages = [
                LLMMessage(role="system", content="You are an assistant."),
                LLMMessage(role="user", content="Disk full on staging"),
            ]
            result = await client.chat(messages, tools=[])

        self.assertIsInstance(result, LLMResponse)
        self.assertIn("SUPPRESS", result.content or "")
        self.assertEqual(result.input_tokens, 50)

    async def test_chat_ignores_non_user_system_messages_for_context(self):
        client = JEVClient(base_url="http://localhost:11434", model="nimble")
        resp = _jev_response("START", {"START": 1.0, "SUPPRESS": 0.0}, 1.0, 10)

        with patch.object(client._client, "post", new_callable=AsyncMock, return_value=resp):
            messages = [
                LLMMessage(role="assistant", content="previous answer"),
                LLMMessage(role="user", content="new alert"),
            ]
            result = await client.chat(messages)

        self.assertIsInstance(result, LLMResponse)
        self.assertIn("START", result.content or "")

    async def test_close_closes_httpx_client(self):
        client = JEVClient(base_url="http://localhost:11434", model="nimble")
        with patch.object(client._client, "aclose", new_callable=AsyncMock) as mock_close:
            await client.close()
            mock_close.assert_awaited_once()
