"""Utilities for interacting with Temporal troubleshooting workflows."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Mapping
from typing import Any

from asgiref.sync import async_to_sync
from temporalio.client import WorkflowHandle

from services.temporal_workers.client import get_temporal_client
from services.temporal_workers.workflows import TroubleshootWorkflow

logger = logging.getLogger(__name__)


async def start_troubleshoot_workflow(
    session_id: str,
    thread_id: str,
) -> WorkflowHandle[Any, Any]:
    """Start the troubleshooting workflow for a session/thread pair."""
    temporal_client = await get_temporal_client()
    workflow_id = f"tsession-{session_id}"
    return await temporal_client.start_workflow(
        TroubleshootWorkflow.run,
        args=[session_id, thread_id],
        id=workflow_id,
        task_queue=os.environ.get("TEMPORAL_TASK_QUEUE", "vogon"),
    )


async def _do_send_message(
    session_id: str,
    message: Mapping[str, Any],
) -> None:
    """Inner signal sender (no retry)."""
    temporal_client = await get_temporal_client()
    workflow_handle = temporal_client.get_workflow_handle(f"tsession-{session_id}")
    await workflow_handle.signal(TroubleshootWorkflow.user_message, dict(message))


async def send_message_to_workflow(
    session_id: str,
    message: Mapping[str, Any],
) -> None:
    """Signal a running troubleshooting workflow with a user message.

    Retries on transient connection errors so a brief Temporal hiccup
    does not drop the message silently.
    """
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            await _do_send_message(session_id, message)
            return
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            logger.warning(
                "Temporal signal attempt %s failed for session %s: %s",
                attempt + 1,
                session_id,
                exc,
            )
            if attempt < 2:
                await asyncio.sleep(0.5 * (attempt + 1))
    raise RuntimeError(
        f"Failed to signal Temporal workflow for session {session_id} after 3 attempts"
    ) from last_exc


async def complete_session_workflow(session_id: str) -> None:
    temporal_client = await get_temporal_client()
    workflow_handle = temporal_client.get_workflow_handle(f"tsession-{session_id}")
    await workflow_handle.signal(TroubleshootWorkflow.complete)


def start_troubleshoot_workflow_sync(
    session_id: str,
    thread_id: str,
) -> WorkflowHandle[Any, Any]:
    """Synchronous wrapper for starting a troubleshooting workflow."""
    return async_to_sync(start_troubleshoot_workflow)(session_id, thread_id)


def send_message_to_workflow_sync(
    session_id: str,
    message: Mapping[str, Any],
) -> None:
    """Synchronous wrapper for signaling a troubleshooting workflow."""
    async_to_sync(send_message_to_workflow)(session_id, message)


def complete_session_workflow_sync(session_id: str) -> None:
    async_to_sync(complete_session_workflow)(session_id)
