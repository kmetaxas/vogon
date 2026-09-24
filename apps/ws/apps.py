"""apps/ws/apps.py - AppConfig for the ws app."""

from django.apps import AppConfig


class WsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.ws"

    def ready(self):
        import apps.ws.signals  # noqa: F401
