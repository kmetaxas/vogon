"""Test settings that use SQLite and skip channels_postgres migrations."""

from vogon.settings import *  # noqa: F401,F403

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    },
}

INSTALLED_APPS = [app for app in INSTALLED_APPS if app != "channels_postgres"]  # noqa: F405

CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels.layers.InMemoryChannelLayer",
    },
}
