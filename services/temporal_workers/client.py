"""Temporal client configuration for Vogon."""

import os

from temporalio.client import Client

_temporal_client = None


async def get_temporal_client() -> Client:
    """Get or create the Temporal client."""
    global _temporal_client
    if _temporal_client is None:
        temporal_host = os.environ.get("TEMPORAL_HOST", "localhost:7233")
        temporal_namespace = os.environ.get("TEMPORAL_NAMESPACE", "default")

        _temporal_client = await Client.connect(
            temporal_host,
            namespace=temporal_namespace,
        )
    return _temporal_client


async def close_temporal_client():
    """Close the Temporal client connection."""
    global _temporal_client
    if _temporal_client is not None:
        await _temporal_client.close()
        _temporal_client = None
