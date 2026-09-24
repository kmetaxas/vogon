from django.core.exceptions import ObjectDoesNotExist
from django.core.management.base import BaseCommand

from apps.core.models import Organization
from apps.marvins.models import Capability, Marvin
from services.temporal_workers.activities import _find_tools


class Command(BaseCommand):
    help = "Test find_tools manually without triggering an agent run"

    def add_arguments(self, parser):
        parser.add_argument(
            "org",
            nargs="?",
            default=None,
            help="Organization name or ID to test against",
        )

    def list_orgs(self):
        self.stdout.write("Available organizations:")
        for org in Organization.objects.all().order_by("name"):
            self.stdout.write(f"  - id={org.id}, name='{org.name}', slug={org.slug}")

    def show_marvin_state(self, org: Organization):
        self.stdout.write(f"\n--- Marvin state for org '{org.name}' ({org.id}) ---")

        marvins = Marvin.objects.filter(organization=org)
        self.stdout.write(f"Total Marvins: {marvins.count()}")
        if marvins.count() == 0:
            self.stdout.write("   No Marvins registered for this org!")
        else:
            for m in marvins.order_by("name"):
                status_icon = "ONLINE" if m.status == Marvin.Status.ONLINE else "OFFLINE"
                caps = list(m.capabilities.values_list("name", flat=True))
                self.stdout.write(f"   {status_icon} {m.name}")
                self.stdout.write(f"      client_id: {m.client_id}")
                self.stdout.write(f"      labels: {m.labels}")
                self.stdout.write(f"      capabilities: {caps}")
                self.stdout.write(f"      last_seen: {m.last_seen}")

        capabilities = Capability.objects.filter(organization=org)
        self.stdout.write(f"\nTotal Capabilities: {capabilities.count()}")
        for cap in capabilities.order_by("name"):
            enabled_icon = "enabled" if cap.enabled else "disabled"
            online_marvins = cap.marvins.filter(status=Marvin.Status.ONLINE).count()
            self.stdout.write(f"   {enabled_icon} {cap.name} (online_marvins={online_marvins})")
            self.stdout.write(f"      description: {cap.description}")
            self.stdout.write(f"      keywords: {cap.keywords}")

    def test_find_tools(self, org: Organization):
        self.stdout.write(f"\n--- Testing _find_tools for org '{org.name}' ({org.id}) ---")
        org_id = str(org.id)

        self.stdout.write("\n1. No filters:")
        result = _find_tools(org_id)
        self.stdout.write(f"   Results: {result}")

        self.stdout.write("\n2. Query='restart':")
        result = _find_tools(org_id, query="restart")
        self.stdout.write(f"   Results: {result}")

        self.stdout.write("\n3. Labels='env:production':")
        result = _find_tools(org_id, labels="env:production")
        self.stdout.write(f"   Results: {result}")

        self.stdout.write("\n4. capability_name='restart':")
        result = _find_tools(org_id, capability_name="restart")
        self.stdout.write(f"   Results: {result}")

        self.stdout.write("\n5. limit=1:")
        result = _find_tools(org_id, limit=1)
        self.stdout.write(f"   Results: {result}")

    def handle(self, *args, **options):
        org_arg = options["org"]

        if not org_arg:
            self.list_orgs()
            self.stdout.write("\nUsage: python manage.py test_find_tools <org_name_or_id>")
            return

        try:
            org = Organization.objects.get(id=org_arg)
        except ObjectDoesNotExist:
            try:
                org = Organization.objects.get(name=org_arg)
            except ObjectDoesNotExist:
                self.stderr.write(f"Organization not found: '{org_arg}'")
                self.list_orgs()
                return

        self.show_marvin_state(org)
        self.test_find_tools(org)
