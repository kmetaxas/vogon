"""Prometheus standard tools for the LLM agent."""

import httpx
from django.conf import settings

from services.crypto import maybe_decrypt


def _auth_headers() -> dict[str, str]:
    token = maybe_decrypt(settings.PROMETHEUS_AUTH_TOKEN)
    if token:
        return {"Authorization": f"Bearer {token}"}
    return {}


async def get_prometheus_alerts() -> dict:
    if not settings.PROMETHEUS_BASE_URL:
        return {"alerts": [], "error": "Prometheus not configured"}
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(
            f"{settings.PROMETHEUS_BASE_URL}/api/v1/alerts",
            headers=_auth_headers(),
        )
        r.raise_for_status()
        alerts = r.json()["data"]["alerts"]
        return {
            "alerts": [
                {
                    "labels": a["labels"],
                    "state": a["state"],
                    "annotations": a.get("annotations", {}),
                }
                for a in alerts
            ]
        }


async def query_prometheus(query: str) -> dict:
    if not settings.PROMETHEUS_BASE_URL:
        return {"results": [], "error": "Prometheus not configured"}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(
            f"{settings.PROMETHEUS_BASE_URL}/api/v1/query",
            params={"query": query},
            headers=_auth_headers(),
        )
        r.raise_for_status()
        return r.json()["data"]
