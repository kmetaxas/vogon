"""Embedding generation for capabilities."""

# pyright: reportAttributeAccessIssue=false

from __future__ import annotations

import logging
from typing import Iterable

from asgiref.sync import sync_to_async

from apps.marvins.models import Capability
from services import metrics
from services.embeddings.base import EmbeddingProvider

logger = logging.getLogger(__name__)


def _save_embedding(capability: Capability, embedding: list[float], model: str) -> None:
    capability.embedding = embedding
    capability.embedding_model = model
    capability.embedding_version = capability.search_document_version
    capability.save(update_fields=["embedding", "embedding_model", "embedding_version"])


class EmbeddingGenerator:
    """Generate and persist embeddings for capabilities."""

    def __init__(self, provider: EmbeddingProvider):
        self.provider = provider

    async def generate_for_capability(self, capability: Capability) -> bool:
        if not capability.search_document:
            logger.warning(
                "Capability %s has no search_document, skipping embedding", capability.name
            )
            return False
        try:
            with metrics.timer("embedding.generate_single"):
                result = await self.provider.embed(str(capability.search_document))
            await sync_to_async(_save_embedding)(capability, result.embedding, result.model)
            metrics.incr("embedding.success")
            return True
        except Exception as exc:
            logger.error("Failed to embed capability %s: %s", capability.name, exc)
            metrics.incr("embedding.failure")
            return False

    async def generate_batch(self, capabilities: Iterable[Capability]) -> tuple[int, int]:
        caps = [c for c in capabilities if c.search_document]
        if not caps:
            return 0, 0
        try:
            with metrics.timer("embedding.generate_batch"):
                results = await self.provider.embed_batch([str(c.search_document) for c in caps])
            succeeded = 0
            for cap, result in zip(caps, results):
                await sync_to_async(_save_embedding)(cap, result.embedding, result.model)
                succeeded += 1
            metrics.incr("embedding.success", succeeded)
            if len(caps) > succeeded:
                metrics.incr("embedding.failure", len(caps) - succeeded)
            return succeeded, len(caps) - succeeded
        except Exception as exc:
            logger.error("Batch embedding failed: %s", exc)
            metrics.incr("embedding.failure", len(caps))
            return 0, len(caps)
