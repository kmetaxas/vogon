import logging
from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.marvins.models import ResolvedTargetSet

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Remove expired ResolvedTargetSet snapshots."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show how many would be deleted without deleting.",
        )

    def handle(self, *args, **options):
        expired = ResolvedTargetSet.objects.filter(expires_at__lt=timezone.now())
        count = expired.count()
        if options["dry_run"]:
            self.stdout.write(f"Would delete {count} expired target sets (dry-run).")
        else:
            expired.delete()
            self.stdout.write(f"Deleted {count} expired target sets.")
