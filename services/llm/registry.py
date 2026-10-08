from django.conf import settings

from services.llm.base import LLMClient
from services.llm.openai_compat import OpenAICompatibleClient


def _is_jev_endpoint(base_url: str | None) -> bool:
    """Detect whether a base URL points to a JEV (/v1/systemone) endpoint."""
    if not base_url:
        return False
    return "/systemone" in base_url


def _jev_base_url(base_url: str) -> str:
    """Strip /v1/systemone (or /systemone) from the URL to get the JEV base."""
    for suffix in ("/v1/systemone", "/systemone"):
        if base_url.endswith(suffix):
            return base_url[: -len(suffix)]
    for marker in ("/v1/systemone", "/systemone"):
        if marker in base_url:
            return base_url.split(marker)[0]
    return base_url


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
        base_url = settings.LLM_BASE_URL
        if _is_jev_endpoint(base_url):
            from services.llm.jev_client import JEVClient

            return JEVClient(
                base_url=_jev_base_url(base_url),
                model=settings.LLM_MODEL,
                timeout=getattr(settings, "ADMISSION_LLM_TIMEOUT_SECONDS", 30),
            )
        return OpenAICompatibleClient(
            base_url=base_url,
            api_key=settings.LLM_API_KEY,
            model=settings.LLM_MODEL,
            timeout=settings.LLM_HTTP_TIMEOUT_SECONDS,
            temperature=settings.LLM_TEMPERATURE,
            max_tokens=settings.LLM_MAX_TOKENS,
        )

    base_url = provider.base_url or settings.LLM_BASE_URL
    if _is_jev_endpoint(base_url):
        from services.llm.jev_client import JEVClient

        return JEVClient(
            base_url=_jev_base_url(base_url),
            model=provider.model,
            timeout=getattr(settings, "ADMISSION_LLM_TIMEOUT_SECONDS", 30),
        )

    return OpenAICompatibleClient(
        base_url=base_url,
        api_key=provider.api_key or settings.LLM_API_KEY,
        model=provider.model,
        timeout=settings.LLM_HTTP_TIMEOUT_SECONDS,
        **provider.config,
    )
