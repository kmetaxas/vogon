from django.conf import settings

from services.llm.base import LLMClient
from services.llm.openai_compat import OpenAICompatibleClient


def get_llm_client(organization_id: str | None = None, provider_id: str | None = None) -> LLMClient:
    """Resolve provider: explicit provider_id → org default → org any → settings fallback."""
    provider = None
    if provider_id:
        try:
            from apps.llm.models import LLMProvider

            provider = LLMProvider.objects.filter(id=provider_id, enabled=True).first()
        except Exception:
            provider = None
    if provider is None and organization_id:
        try:
            from apps.llm.models import LLMProvider

            provider = (
                LLMProvider.objects.filter(organization_id=organization_id, enabled=True)
                .order_by("-is_default")
                .first()
            )
        except Exception:
            # DB not ready or model unavailable; fall back to settings.
            provider = None
    if provider is None:
        return OpenAICompatibleClient(
            base_url=settings.LLM_BASE_URL,
            api_key=settings.LLM_API_KEY,
            model=settings.LLM_MODEL,
            timeout=settings.LLM_HTTP_TIMEOUT_SECONDS,
            temperature=settings.LLM_TEMPERATURE,
            max_tokens=settings.LLM_MAX_TOKENS,
        )
    return OpenAICompatibleClient(
        base_url=provider.base_url or settings.LLM_BASE_URL,
        api_key=provider.api_key or settings.LLM_API_KEY,
        model=provider.model,
        timeout=settings.LLM_HTTP_TIMEOUT_SECONDS,
        **provider.config,
    )
