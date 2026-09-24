"""OpenAI-compatible embedding provider."""

from __future__ import annotations

from openai import AsyncOpenAI

from services.embeddings.base import EmbeddingResult


class OpenAICompatibleEmbeddingProvider:
    """Adapter for OpenAI-compatible embedding endpoints (OpenAI, Ollama, vLLM)."""

    def __init__(self, base_url: str, api_key: str, model: str, dimensions: int | None = None):
        self._client = AsyncOpenAI(base_url=base_url, api_key=api_key or "ollama")
        self._model = model
        self._dimensions = dimensions

    async def embed(self, text: str) -> EmbeddingResult:
        resp = await self._client.embeddings.create(model=self._model, input=text)
        vec = list(resp.data[0].embedding)
        await self._client.close()
        return EmbeddingResult(embedding=vec, model=self._model, dimensions=len(vec))

    async def embed_batch(self, texts: list[str]) -> list[EmbeddingResult]:
        resp = await self._client.embeddings.create(model=self._model, input=texts)
        results = [
            EmbeddingResult(
                embedding=list(data.embedding),
                model=self._model,
                dimensions=len(data.embedding),
            )
            for data in resp.data
        ]
        await self._client.close()
        return results

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def dimensions(self) -> int:
        return self._dimensions or 1536
