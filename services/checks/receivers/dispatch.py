# pyright: reportAttributeAccessIssue=false

"""Temporal dispatch utilities for Check receivers."""

import logging
from datetime import UTC, datetime

from temporalio.client import WorkflowHandle

from apps.checks.models import CheckReceiver, ReceiverEvent
from services.temporal_workers.client import get_temporal_client
from services.temporal_workers.workflows import (
    AutonomousInvestigationWorkflow,
    ReceiverAdmissionWorkflow,
)

TASK_QUEUE = "vogon"
logger = logging.getLogger(__name__)


async def dispatch_to_temporal(
    receiver: CheckReceiver,
    execution_id: str,
) -> WorkflowHandle:
    """Fire-and-forget dispatch of AutonomousInvestigationWorkflow with an existing execution.

    The caller is expected to have already created a ``CheckExecution``
    (status=QUEUED) and pass its ID here. The workflow will transition the
    execution to RUNNING instead of creating a new one.
    """
    client = await get_temporal_client()
    workflow_id = f"check-{receiver.check.id}-receiver-{datetime.now(UTC).isoformat()}"
    handle: WorkflowHandle = await client.start_workflow(
        AutonomousInvestigationWorkflow.run,
        id=workflow_id,
        args=[
            str(receiver.check.id),
            None,  # version_id
            False,  # dry_run
            None,  # max_iterations
            execution_id,
        ],
        task_queue=TASK_QUEUE,
    )
    logger.info(
        "Dispatched AutonomousInvestigationWorkflow for receiver=%s execution=%s workflow=%s",
        receiver.id,
        execution_id,
        handle.id,
    )
    return handle


async def dispatch_admission_workflow(
    receiver: CheckReceiver,
    receiver_event: ReceiverEvent,
) -> WorkflowHandle:
    client = await get_temporal_client()
    workflow_id = f"receiver-admission-{receiver.id}-{receiver_event.id}"
    handle: WorkflowHandle = await client.start_workflow(
        ReceiverAdmissionWorkflow.run,
        id=workflow_id,
        args=[str(receiver.id), str(receiver_event.id)],
        task_queue=TASK_QUEUE,
    )
    logger.info(
        "Dispatched ReceiverAdmissionWorkflow for receiver=%s event=%s workflow=%s",
        receiver.id,
        receiver_event.id,
        handle.id,
    )
    return handle
