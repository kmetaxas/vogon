import logging
from django.core.management.base import BaseCommand

from apps.marvins.models import Capability, ResourceType
from apps.sessions.models import TSession

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Populate default discovery fields on existing data after migration."

    def handle(self, *args, **options):
        self.stdout.write("Populating Capability defaults...")
        cap_count = Capability.objects.filter(execution_scope="").update(
            execution_scope="marvin-local",
            selection_strategy="one",
        )
        self.stdout.write(f"  Updated {cap_count} Capabilities")

        self.stdout.write("Populating ResourceType defaults...")
        rt_count = ResourceType.objects.filter(scope_type="").update(
            scope_type="generic",
        )
        self.stdout.write(f"  Updated {rt_count} ResourceTypes")

        self.stdout.write("Populating TSession execution_budget defaults...")
        session_count = 0
        for session in TSession.objects.filter(execution_budget={}):
            session.execution_budget = {
                "max_executions_per_session": 100,
                "max_targets_per_session": 200,
                "max_concurrent_executions": 5,
                "executions_used": 0,
                "targets_used": 0,
                "concurrent_running": 0,
            }
            session.save(update_fields=["execution_budget"])
            session_count += 1
        self.stdout.write(f"  Updated {session_count} TSessions")

        self.stdout.write(self.style.SUCCESS("Done."))
