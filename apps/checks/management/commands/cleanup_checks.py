"""Management command to clean up old Check execution records."""

from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.checks.models import CheckActionLog, CheckExecution, CheckVersion


class Command(BaseCommand):
    help = "Delete old CheckExecution, CheckActionLog, and CheckVersion records beyond retention."

    def handle(self, *args, **options):
        default_retention = getattr(settings, "CHECK_DEFAULT_RETENTION_DAYS", 90)
        cutoff = timezone.now() - timedelta(days=default_retention)

        # Delete old CheckExecution records
        exec_deleted, _ = CheckExecution.objects.filter(triggered_at__lt=cutoff).delete()

        # Delete old CheckActionLog records
        log_deleted, _ = CheckActionLog.objects.filter(created_at__lt=cutoff).delete()

        # Delete old CheckVersion records (keep at least the latest version per check)
        version_deleted = 0
        for check in CheckExecution.objects.values_list("check", flat=True).distinct():
            latest_version = (
                CheckVersion.objects.filter(check_id=check).order_by("-version_number").first()
            )
            if latest_version:
                deleted, _ = (
                    CheckVersion.objects.filter(
                        check_id=check,
                        created_at__lt=cutoff,
                    )
                    .exclude(id=latest_version.id)
                    .delete()
                )
                version_deleted += deleted

        self.stdout.write(
            f"Cleanup complete: {exec_deleted} executions, {log_deleted} action logs, "
            f"{version_deleted} versions deleted."
        )
