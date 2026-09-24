from django.core.management.base import BaseCommand, CommandError

from apps.core.models import Organization


class Command(BaseCommand):
    help = "Rotate an organization's MCP API token"

    def add_arguments(self, parser):
        parser.add_argument("org_slug", help="Organization slug")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would happen without generating or storing a token",
        )

    def handle(self, *args, **options):
        org_slug = options["org_slug"]
        dry_run = options["dry_run"]

        org = Organization.objects.filter(slug=org_slug).first()
        if org is None:
            raise CommandError(f"Organization not found: {org_slug}")

        if dry_run:
            self.stdout.write(f"Dry run: would rotate MCP API token for {org.name} ({org.slug})")
            return

        token = org.generate_mcp_token()
        self.stdout.write(self.style.SUCCESS(f"Rotated MCP API token for {org.name}"))
        self.stdout.write("Plaintext token (shown once):")
        self.stdout.write(token)
