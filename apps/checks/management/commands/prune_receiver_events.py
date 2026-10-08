"""Management command to prune old receiver events and admission decisions."""

from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.checks.models import ReceiverAdmissionDecision, ReceiverEvent


class Command(BaseCommand):
    help = "Delete ReceiverEvent and ReceiverAdmissionDecision records older than N days."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=getattr(settings, "CHECK_RECEIVER_EVENT_RETENTION_DAYS", 30),
            help="Retention window in days (default: 30).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be deleted without deleting anything.",
        )

    def handle(self, *args, **options):
        days = options["days"]
        dry_run = options["dry_run"]
        cutoff = timezone.now() - timedelta(days=days)

        events = ReceiverEvent.objects.filter(created_at__lt=cutoff)
        decisions = ReceiverAdmissionDecision.objects.filter(created_at__lt=cutoff)

        event_count = events.count()
        decision_count = decisions.count()

        if dry_run:
            self.stdout.write(
                f"Dry run: would delete {event_count} receiver event(s) and "
                f"{decision_count} admission decision(s) older than {days} day(s)."
            )
            return

        with transaction.atomic():
            events.delete()
            decisions.delete()

        self.stdout.write(
            self.style.SUCCESS(
                f"Pruned {event_count} receiver event(s) and {decision_count} "
                f"admission decision(s) older than {days} day(s)."
            )
        )
