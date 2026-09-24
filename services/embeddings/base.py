"""Base types for embedding providers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class EmbeddingResult:
    embedding: list[float]
    model: str
    dimensions: int


class EmbeddingProvider(Protocol):
    """Protocol for embedding providers."""

    async def embed(self, text: str) -> EmbeddingResult: ...

    async def embed_batch(self, texts: list[str]) -> list[EmbeddingResult]: ...

    @property
    def model_name(self) -> str: ...

    @property
    def dimensions(self) -> int: ...
