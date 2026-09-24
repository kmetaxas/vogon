from django.apps import AppConfig


class MarvinsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.marvins"

    def ready(self):
        import apps.marvins.signals  # noqa: F401
