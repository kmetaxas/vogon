"""Embedding provider factory with environment-based configuration."""

from __future__ import annotations

from django.conf import settings

from services.embeddings.base import EmbeddingProvider
from services.embeddings.openai_compat import OpenAICompatibleEmbeddingProvider


class EmbeddingProviderFactory:
    """Resolve an embedding provider from Django settings."""

    @staticmethod
    def get_default_provider() -> EmbeddingProvider:
        provider_type = getattr(settings, "EMBEDDING_PROVIDER_TYPE", "openai")
        if provider_type == "openai":
            return OpenAICompatibleEmbeddingProvider(
                base_url=settings.EMBEDDING_BASE_URL,
                api_key=settings.EMBEDDING_API_KEY,
                model=settings.EMBEDDING_MODEL,
                dimensions=getattr(settings, "EMBEDDING_DIMENSIONS", None),
            )
        raise ValueError(f"Unsupported embedding provider type: {provider_type}")
