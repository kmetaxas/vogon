# pyright: reportAttributeAccessIssue=false

"""Management command to migrate Check Temporal schedules to AutonomousInvestigationWorkflow.

Existing schedules created before CheckWorkflow was removed still point at the
now-deleted ``CheckWorkflow`` and will fail. This command recreates them pointing
at ``AutonomousInvestigationWorkflow``. It is idempotent and safe to re-run.
"""

import asyncio

from django.core.management.base import BaseCommand

from apps.checks.models import Check
from services.checks.scheduler import CheckScheduler
from services.temporal_workers.client import get_temporal_client

OLD_WORKFLOW = "CheckWorkflow"
NEW_WORKFLOW = "AutonomousInvestigationWorkflow"


class Command(BaseCommand):
    help = (
        "Migrate Temporal schedules pointing at CheckWorkflow to AutonomousInvestigationWorkflow."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would be done without modifying any schedules.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        try:
            asyncio.run(self._run(dry_run))
        except Exception as exc:  # noqa: BLE001 - never crash the command
            self.stderr.write(self.style.ERROR(f"Temporal unavailable: {exc}"))
            self.stdout.write("No schedules were changed.")

    async def _run(self, dry_run: bool) -> None:
        try:
            client = await get_temporal_client()
        except Exception as exc:  # noqa: BLE001 - Temporal offline
            self.stderr.write(self.style.ERROR(f"Could not connect to Temporal: {exc}"))
            self.stdout.write("No schedules were changed.")
            return

        migrated = 0
        skipped = 0
        missing = 0

        for check in Check.objects.all():
            schedule_id = CheckScheduler._schedule_id(str(check.id))
            try:
                handle = client.get_schedule_handle(schedule_id)
                description = await handle.describe()
            except Exception as exc:  # noqa: BLE001 - schedule not found / offline
                missing += 1
                self.stdout.write(f"[skip] {schedule_id}: no schedule ({exc})")
                continue

            current_workflow = description.schedule.action.workflow  # type: ignore[attr-defined]

            if current_workflow == NEW_WORKFLOW:
                skipped += 1
                self.stdout.write(f"[skip] {schedule_id}: already {NEW_WORKFLOW}")
                continue

            if current_workflow != OLD_WORKFLOW:
                skipped += 1
                self.stdout.write(f"[skip] {schedule_id}: unexpected workflow {current_workflow!r}")
                continue

            if dry_run:
                self.stdout.write(
                    f"[dry-run] {schedule_id}: would delete {OLD_WORKFLOW} and "
                    f"create {NEW_WORKFLOW}"
                )
                migrated += 1
                continue

            try:
                await handle.delete()
                await client.create_schedule(schedule_id, CheckScheduler._build_schedule(check))
            except Exception as exc:  # noqa: BLE001 - keep going on per-check errors
                self.stderr.write(self.style.ERROR(f"[error] {schedule_id}: {exc}"))
                continue

            migrated += 1
            self.stdout.write(f"[migrated] {schedule_id}: {OLD_WORKFLOW} -> {NEW_WORKFLOW}")

        prefix = "Would migrate" if dry_run else "Migrated"
        self.stdout.write(
            f"{prefix} {migrated} schedule(s); skipped {skipped}; no schedule for {missing}."
        )
