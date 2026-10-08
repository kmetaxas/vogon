# pyright: reportAttributeAccessIssue=false, reportUnknownVariableType=false

"""Temporal Schedule management for Checks."""

import os
from datetime import UTC, datetime, timedelta

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
)

TASK_QUEUE = os.environ.get("TEMPORAL_TASK_QUEUE", "vogon")


class CheckScheduler:
    @classmethod
    def _schedule_id(cls, check_id: str) -> str:
        return f"check-{check_id}"

    @classmethod
    async def create_schedule(cls, check) -> dict:
        if not check.enabled:
            return {"schedule_id": None, "status": "skipped_disabled"}

        if check.schedule_type == "event":
            return {"schedule_id": None, "status": "skipped_event_type"}

        client: Client = await get_temporal_client()
        schedule_id = cls._schedule_id(str(check.id))

        handle = await client.create_schedule(schedule_id, cls._build_schedule(check))
        return {"schedule_id": handle.id, "status": "created"}

    @classmethod
    async def update_schedule(cls, check) -> dict:
        if check.schedule_type == "event":
            return {"schedule_id": None, "status": "skipped_event_type"}

        client: Client = await get_temporal_client()
        schedule_id = cls._schedule_id(str(check.id))

        try:
            handle = client.get_schedule_handle(schedule_id)
            description = await handle.describe()

            if not check.enabled and not description.schedule.state.paused:
                await handle.pause()
                return {"schedule_id": schedule_id, "status": "paused"}

            if check.enabled and description.schedule.state.paused:
                await handle.unpause()
                return {"schedule_id": schedule_id, "status": "resumed"}

            schedule = cls._build_schedule(check)
            await handle.update(lambda _input: ScheduleUpdate(schedule=schedule))
            return {"schedule_id": schedule_id, "status": "updated"}
        except Exception:
            if check.enabled:
                return await cls.create_schedule(check)
            return {"schedule_id": None, "status": "skipped_disabled"}

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
    async def trigger_now(
        cls, check, dry_run: bool = False, execution_id: str | None = None
    ) -> dict:
        client: Client = await get_temporal_client()
        workflow_cls = AutonomousInvestigationWorkflow
        workflow_id = f"check-{check.id}-manual-{datetime.now(UTC).isoformat()}"

        handle = await client.start_workflow(
            workflow_cls.run,
            id=workflow_id,
            args=[str(check.id), None, dry_run, None, execution_id],
            task_queue=TASK_QUEUE,
        )
        return {"workflow_id": handle.id, "status": "triggered"}

    @classmethod
    def _build_schedule(cls, check) -> Schedule:
        workflow_cls = AutonomousInvestigationWorkflow
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
