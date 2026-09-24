"""Management command to regenerate embeddings for all capabilities."""

import asyncio

from django.core.management.base import BaseCommand
from django.db.models import F

from apps.marvins.models import Capability
from services.embeddings.generator import EmbeddingGenerator
from services.embeddings.registry import EmbeddingProviderFactory


class Command(BaseCommand):
    help = "Regenerate embeddings for all capabilities"

    def add_arguments(self, parser):
        parser.add_argument(
            "--batch-size",
            type=int,
            default=100,
            help="Number of capabilities to embed per batch",
        )
        parser.add_argument(
            "--bump-version",
            action="store_true",
            help="Increment search_document_version to force regeneration",
        )

    def handle(self, *args, **options):
        batch_size = options["batch_size"]

        if options["bump_version"]:
            Capability.objects.all().update(
                search_document_version=F("search_document_version") + 1
            )

        provider = EmbeddingProviderFactory.get_default_provider()
        generator = EmbeddingGenerator(provider)
        print(f"Provider={provider} and generator: {generator}")

        capabilities = Capability.objects.filter(enabled=True).order_by("id")
        total = capabilities.count()
        self.stdout.write(f"Generating embeddings for {total} capabilities...")

        succeeded = 0
        failed = 0
        batch: list[Capability] = []
        for capability in capabilities.iterator():
            batch.append(capability)
            if len(batch) >= batch_size:
                s, f = asyncio.run(generator.generate_batch(batch))
                succeeded += s
                failed += f
                batch = []

        if batch:
            s, f = asyncio.run(generator.generate_batch(batch))
            succeeded += s
            failed += f

        self.stdout.write(self.style.SUCCESS(f"Generated {succeeded} embeddings, {failed} failed."))
