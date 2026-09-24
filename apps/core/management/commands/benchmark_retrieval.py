"""Management command to benchmark capability discovery retrieval."""

from django.core.management.base import BaseCommand

from apps.core.models import Organization
from apps.marvins.discovery import DiscoveryEngine
from services.discovery.evaluation.runner import BenchmarkRunner


class Command(BaseCommand):
    help = "Run retrieval benchmark against current discovery implementation"

    def add_arguments(self, parser):
        parser.add_argument(
            "--config",
            default="keyword",
            help="Benchmark configuration name (e.g. keyword, hybrid)",
        )
        parser.add_argument(
            "--output",
            default=None,
            help="Output file path for markdown report (default: stdout)",
        )
        parser.add_argument(
            "--org",
            default=None,
            help="Organization slug to test against (default: first org)",
        )

    def handle(self, *args, **options):
        config_name = options["config"]
        output_path = options["output"]

        # Resolve organization
        if options["org"]:
            try:
                org = Organization.objects.get(slug=options["org"])
            except Organization.DoesNotExist:
                self.stderr.write(f"Organization not found: {options['org']}")
                return
        else:
            org = Organization.objects.first()
            if not org:
                self.stderr.write("No organizations found. Create one first.")
                return

        self.stdout.write(f"Running benchmark '{config_name}' against org '{org.name}'")

        runner = BenchmarkRunner("services/discovery/evaluation/test_cases.yaml")
        engine = DiscoveryEngine()
        result = runner.run(engine, organization=org, config_name=config_name)

        report = result.to_markdown()

        if output_path:
            with open(output_path, "w") as f:
                f.write(report)
            self.stdout.write(self.style.SUCCESS(f"Report written to {output_path}"))
        else:
            self.stdout.write(report)
