# pyright: reportAttributeAccessIssue=false
"""Sync notification provider schemas from the registry into the database."""

from django.core.management.base import BaseCommand

from apps.notifications.models import NotificationProvider
from services.notifications.registry import registry


class Command(BaseCommand):
    help = "Sync notification provider schemas from the registry to the database."

    def handle(self, *args, **options):
        count = 0
        for provider_type in registry.list_providers():
            provider_cls = registry.get(provider_type)
            NotificationProvider.objects.update_or_create(
                provider_type=provider_type,
                defaults={
                    "display_name": provider_cls.display_name,
                    "json_schema": provider_cls.config_schema,
                },
            )
            count += 1

        self.stdout.write(self.style.SUCCESS(f"Synced {count} notification provider(s)."))
