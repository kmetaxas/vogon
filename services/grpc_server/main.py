"""Entry point for the gRPC service with Temporal worker."""

# pyright: reportMissingImports=false, reportAttributeAccessIssue=false

import asyncio
import logging
import os

from temporalio.worker import Worker

from services.grpc_server.servicer import MarvinServicer, serve_grpc
from services.temporal_workers.activities import (
    build_llm_context,
    call_llm,
    check_completion,
    create_assistant_message,
    create_tool_call_messages,
    execute_capability,
    execute_llm_tool,
    gather_thread_context,
    generate_capability_embedding,
    get_user_messages,
    initialize_session,
    record_agent_event,
    set_grpc_service,
    set_session_status,
)
from services.temporal_workers.client import get_temporal_client
from services.temporal_workers.workflows import (
    CapabilityExecutionWorkflow,
    ThreadWorkflow,
    TroubleshootWorkflow,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s:%(name)s:%(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "vogon.settings")
from django import setup as django_setup  # noqa: E402

django_setup()


STALE_THRESHOLD_SECONDS = 60


def _mark_stale_offline():
    from datetime import timedelta

    from django.utils import timezone

    from apps.marvins.models import Marvin

    threshold = timezone.now() - timedelta(seconds=STALE_THRESHOLD_SECONDS)
    stale = Marvin.objects.filter(status=Marvin.Status.ONLINE, last_seen__lt=threshold)
    count = stale.update(status=Marvin.Status.OFFLINE)
    if count:
        logger.info(f"Marked {count} Marvin(s) offline due to missed heartbeats")


def _mark_all_online_offline():
    from apps.marvins.models import Marvin

    count = Marvin.objects.filter(status=Marvin.Status.ONLINE).update(status=Marvin.Status.OFFLINE)
    if count:
        logger.info(f"Startup cleanup: marked {count} Marvin(s) offline from prior run")


async def _cleanup_stale_marvins():
    from asgiref.sync import sync_to_async

    while True:
        await asyncio.sleep(30)
        await sync_to_async(_mark_stale_offline)()


async def run_grpc_and_temporal():
    from asgiref.sync import sync_to_async

    await sync_to_async(_mark_all_online_offline)()

    grpc_service = MarvinServicer()
    set_grpc_service(grpc_service)
    grpc_service.start_embedding_poll()

    temporal_client = await get_temporal_client()

    temporal_worker = Worker(
        temporal_client,
        task_queue=os.environ.get("TEMPORAL_TASK_QUEUE", "vogon"),
        workflows=[
            TroubleshootWorkflow,
            ThreadWorkflow,
            CapabilityExecutionWorkflow,
        ],
        activities=[
            build_llm_context,
            call_llm,
            create_tool_call_messages,
            execute_capability,
            execute_llm_tool,
            initialize_session,
            check_completion,
            gather_thread_context,
            create_assistant_message,
            get_user_messages,
            record_agent_event,
            generate_capability_embedding,
            set_session_status,
        ],
    )

    logger.info("Starting Temporal worker...")

    grpc_task = asyncio.create_task(serve_grpc(grpc_service))
    temporal_task = asyncio.create_task(temporal_worker.run())
    cleanup_task = asyncio.create_task(_cleanup_stale_marvins())

    try:
        await asyncio.gather(grpc_task, temporal_task, cleanup_task)
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        grpc_task.cancel()
        cleanup_task.cancel()
        await temporal_worker.shutdown()
        try:
            await grpc_task
        except asyncio.CancelledError:
            pass
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass


if __name__ == "__main__":
    asyncio.run(run_grpc_and_temporal())
