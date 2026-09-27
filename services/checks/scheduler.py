# pyright: reportAttributeAccessIssue=false, reportUnknownVariableType=false

"""Temporal Schedule management for Checks."""

import os
from datetime import datetime, timedelta

from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleIntervalSpec,
    ScheduleOverlapPolicy,
    SchedulePolicy,
    ScheduleSpec,
    ScheduleUpdate,
)

from services.temporal_workers.client import get_temporal_client
from services.temporal_workers.workflows import (
    AutonomousInvestigationWorkflow,
    CheckWorkflow,
)

TASK_QUEUE = os.environ.get("TEMPORAL_TASK_QUEUE", "vogon")


class CheckScheduler:
    @classmethod
    def _schedule_id(cls, check_id: str) -> str:
        return f"check-{check_id}"

    @classmethod
    def _workflow_for_mode(cls, mode: str) -> type:
        if mode == "autonomous":
            return AutonomousInvestigationWorkflow
        return CheckWorkflow

    @classmethod
    async def create_schedule(cls, check) -> dict:
        client: Client = await get_temporal_client()
        schedule_id = cls._schedule_id(str(check.id))

        handle = await client.create_schedule(schedule_id, cls._build_schedule(check))
        return {"schedule_id": handle.id, "status": "created"}

    @classmethod
    async def update_schedule(cls, check) -> dict:
        client: Client = await get_temporal_client()
        schedule_id = cls._schedule_id(str(check.id))

        try:
            handle = client.get_schedule_handle(schedule_id)
            schedule = cls._build_schedule(check)
            await handle.update(lambda _input: ScheduleUpdate(schedule=schedule))
            return {"schedule_id": schedule_id, "status": "updated"}
        except Exception:
            return await cls.create_schedule(check)

    @classmethod
    async def pause_schedule(cls, check_id: str) -> dict:
        client: Client = await get_temporal_client()
        schedule_id = cls._schedule_id(check_id)

        try:
            handle = client.get_schedule_handle(schedule_id)
            await handle.pause()
            return {"schedule_id": schedule_id, "status": "paused"}
        except Exception as exc:
            return {"schedule_id": schedule_id, "status": "error", "error": str(exc)}

    @classmethod
    async def resume_schedule(cls, check_id: str) -> dict:
        client: Client = await get_temporal_client()
        schedule_id = cls._schedule_id(check_id)

        try:
            handle = client.get_schedule_handle(schedule_id)
            await handle.unpause()
            return {"schedule_id": schedule_id, "status": "resumed"}
        except Exception as exc:
            return {"schedule_id": schedule_id, "status": "error", "error": str(exc)}

    @classmethod
    async def delete_schedule(cls, check_id: str) -> dict:
        client: Client = await get_temporal_client()
        schedule_id = cls._schedule_id(check_id)

        try:
            handle = client.get_schedule_handle(schedule_id)
            await handle.delete()
            return {"schedule_id": schedule_id, "status": "deleted"}
        except Exception as exc:
            return {"schedule_id": schedule_id, "status": "error", "error": str(exc)}

    @classmethod
    async def trigger_now(cls, check, dry_run: bool = False) -> dict:
        client: Client = await get_temporal_client()
        workflow_cls = cls._workflow_for_mode(check.execution_mode)
        workflow_id = f"check-{check.id}-manual-{datetime.utcnow().isoformat()}"

        result = await client.execute_workflow(
            workflow_cls.run,
            id=workflow_id,
            args=[str(check.id), None, dry_run],
            task_queue=TASK_QUEUE,
        )
        return {"workflow_id": result.get("workflow_id", workflow_id), "status": "triggered"}

    @classmethod
    def _build_schedule(cls, check) -> Schedule:
        workflow_cls = cls._workflow_for_mode(check.execution_mode)
        schedule_id = cls._schedule_id(str(check.id))
        return Schedule(
            action=ScheduleActionStartWorkflow(
                workflow_cls.run,
                id=schedule_id,
                args=[str(check.id), None, False],
                task_queue=TASK_QUEUE,
            ),
            spec=cls._build_spec(check),
            policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.BUFFER_ONE),
        )

    @classmethod
    def _build_spec(cls, check) -> ScheduleSpec:
        if check.schedule_type == "interval":
            seconds = int(check.schedule_expression)
            return ScheduleSpec(
                intervals=[ScheduleIntervalSpec(every=timedelta(seconds=seconds))],
            )
        if check.schedule_type == "cron":
            return ScheduleSpec(
                cron_expressions=[check.schedule_expression],
            )

        return ScheduleSpec(
            intervals=[ScheduleIntervalSpec(every=timedelta(seconds=60))],
        )
