# pyright: reportMissingImports=false

from services.notifications.providers.email import EmailProvider
from services.notifications.providers.slack import SlackProvider
from services.notifications.providers.teams import TeamsProvider
from services.notifications.registry import (
    BaseNotificationProvider,
    ProviderRegistry,
    registry,
)

from .service import NotificationService, PolicyResolver, Router

registry.register(EmailProvider.provider_type, EmailProvider)
registry.register(SlackProvider.provider_type, SlackProvider)
registry.register(TeamsProvider.provider_type, TeamsProvider)

__all__ = [
    "BaseNotificationProvider",
    "EmailProvider",
    "NotificationService",
    "PolicyResolver",
    "ProviderRegistry",
    "Router",
    "SlackProvider",
    "TeamsProvider",
    "registry",
]
