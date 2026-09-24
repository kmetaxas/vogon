from django.core.exceptions import ObjectDoesNotExist
from django.core.management.base import BaseCommand

from apps.core.models import Organization
from apps.marvins.models import Capability, Marvin


class Command(BaseCommand):
    help = "Diagnose Marvin and Capability state in the database"

    def add_arguments(self, parser):
        parser.add_argument(
            "org",
            nargs="?",
            default=None,
            help="Organization name or ID to diagnose (omitted = all orgs)",
        )

    def diagnose_org(self, org: Organization):
        self.stdout.write(f"\n{'=' * 60}")
        self.stdout.write(f"Organization: {org.name} (id={org.id}, slug={org.slug})")
        self.stdout.write(f"{'=' * 60}")

        marvins = Marvin.objects.filter(organization=org).order_by("name")
        self.stdout.write(f"\nMarvins: {marvins.count()}")
        if marvins.count() == 0:
            self.stdout.write("   No Marvins registered for this org!")
        else:
            for m in marvins:
                status = m.status
                caps = list(m.capabilities.values_list("name", flat=True))
                self.stdout.write(f"   [{status}] {m.name}")
                self.stdout.write(f"      client_id: {m.client_id}")
                self.stdout.write(f"      labels: {m.labels}")
                self.stdout.write(f"      capabilities: {caps}")
                self.stdout.write(f"      last_seen: {m.last_seen}")

        capabilities = Capability.objects.filter(organization=org).order_by("name")
        self.stdout.write(f"\nCapabilities: {capabilities.count()}")
        if capabilities.count() == 0:
            self.stdout.write("   No capabilities registered for this org!")
        else:
            for cap in capabilities:
                online_count = cap.marvins.filter(status=Marvin.Status.ONLINE).count()
                total_count = cap.marvins.count()
                self.stdout.write(f"   {'enabled' if cap.enabled else 'disabled'} {cap.name}")
                self.stdout.write(f"      online_marvins: {online_count} / {total_count}")
                self.stdout.write(f"      description: {cap.description}")

        # Analysis
        self.stdout.write(f"\nAnalysis:")
        online_marvins = marvins.filter(status=Marvin.Status.ONLINE)
        if online_marvins.count() == 0:
            self.stdout.write("   FAIL: No Marvins are ONLINE. find_tools will return empty.")
            self.stdout.write("      Fix: Ensure a Marvin agent is connected via gRPC.")
            return

        enabled_caps = capabilities.filter(enabled=True)
        if enabled_caps.count() == 0:
            self.stdout.write("   FAIL: No capabilities are ENABLED. find_tools will return empty.")
            self.stdout.write("      Fix: Register capabilities through a Marvin agent.")
            return

        caps_with_online = enabled_caps.filter(marvins__status=Marvin.Status.ONLINE).distinct()

        if caps_with_online.count() == 0:
            self.stdout.write("   FAIL: Capabilities exist but NONE are linked to ONLINE Marvins.")
            self.stdout.write("      This usually means:")
            self.stdout.write("      1. Marvin registered but then disconnected (status=offline)")
            self.stdout.write(
                "      2. Capability was created manually but not via Marvin registration"
            )
            self.stdout.write("      3. Marvin registration succeeded but capability sync failed")
            return

        self.stdout.write(
            f"   PASS: {caps_with_online.count()} capability(s) available via "
            f"{online_marvins.count()} online Marvin(s)"
        )
        for cap in caps_with_online:
            names = list(
                cap.marvins.filter(status=Marvin.Status.ONLINE).values_list("name", flat=True)
            )
            self.stdout.write(f"      - '{cap.name}' via: {', '.join(names)}")

    def handle(self, *args, **options):
        org_arg = options["org"]

        if not org_arg:
            self.stdout.write("Diagnosing all organizations...\n")
            orgs = Organization.objects.all().order_by("name")
            if orgs.count() == 0:
                self.stdout.write("No organizations found in the database.")
                self.stdout.write("Create an org first via the shell.")
                return
            for org in orgs:
                self.diagnose_org(org)
        else:
            try:
                org = Organization.objects.get(id=org_arg)
            except ObjectDoesNotExist:
                try:
                    org = Organization.objects.get(name=org_arg)
                except ObjectDoesNotExist:
                    self.stderr.write(f"Organization not found: '{org_arg}'")
                    self.stdout.write("Available organizations:")
                    for o in Organization.objects.all().order_by("name"):
                        self.stdout.write(f"  - id={o.id}, name='{o.name}', slug={o.slug}")
                    return
            self.diagnose_org(org)

        self.stdout.write(f"\n{'=' * 60}")
        self.stdout.write("Done.")
