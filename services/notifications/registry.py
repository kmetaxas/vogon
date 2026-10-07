"""Provider registry and base interface for notification delivery.

Notification providers are responsible for delivering a :class:`Notification`
to a recipient through a specific transport (email, webhook, Teams, ...).
Concrete providers are registered against a short ``provider_type`` string so
that the delivery layer can resolve them at runtime without importing every
implementation directly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar

from jsonschema import Draft7Validator


class BaseNotificationProvider(ABC):
    """Abstract base class for all notification providers.

    Subclasses implement the transport-specific delivery logic. Instances are
    expected to be cheap to construct and stateless; configuration is passed
    to :meth:`send` via the delivery record.
    """

    provider_type: ClassVar[str] = ""
    display_name: ClassVar[str] = ""
    config_schema: ClassVar[dict] = {"type": "object", "properties": {}}

    @abstractmethod
    def send(self, notification: Any, delivery: Any) -> dict:
        """Deliver ``notification`` using ``delivery`` configuration.

        Args:
            notification: The notification to deliver (``Notification`` model).
            delivery: The delivery record describing the target and config
                (``NotificationDelivery`` model).

        Returns:
            A result dict describing the outcome, e.g. ``{"status": "sent"}``
            or ``{"status": "failed", "error": "..."}``.
        """
        raise NotImplementedError

    def validate_config(self, config: dict) -> list[str]:
        """Validate a provider configuration dict.

        Args:
            config: Provider-specific configuration to validate.

        Returns:
            A list of human-readable error messages. An empty list means the
            configuration is valid.
        """
        validator = Draft7Validator(self.config_schema)
        errors = []
        for error in sorted(validator.iter_errors(config), key=lambda err: list(err.path)):
            location = ".".join(str(part) for part in error.path) or "(root)"
            errors.append(f"{location}: {error.message}")
        return errors


class ProviderRegistry:
    """Registry mapping ``provider_type`` strings to provider classes."""

    def __init__(self) -> None:
        self._providers: dict[str, type[BaseNotificationProvider]] = {}

    def register(self, provider_type: str, cls: type[BaseNotificationProvider]) -> None:
        """Register ``cls`` under ``provider_type``.

        Args:
            provider_type: Unique string identifier for the provider.
            cls: The provider class to register.

        Raises:
            TypeError: If ``cls`` is not a subclass of
                :class:`BaseNotificationProvider`.
            ValueError: If ``provider_type`` is empty or already registered.
        """
        if not provider_type:
            raise ValueError("provider_type must be a non-empty string")
        if not isinstance(cls, type) or not issubclass(cls, BaseNotificationProvider):
            raise TypeError(f"{cls!r} must be a subclass of BaseNotificationProvider")
        if provider_type in self._providers:
            raise ValueError(f"Provider type already registered: {provider_type}")
        self._providers[provider_type] = cls

    def get(self, provider_type: str) -> type[BaseNotificationProvider]:
        """Return the provider class registered under ``provider_type``.

        Args:
            provider_type: The identifier used at registration time.

        Returns:
            The registered provider class.

        Raises:
            KeyError: If no provider is registered for ``provider_type``.
        """
        try:
            return self._providers[provider_type]
        except KeyError:
            raise KeyError(f"No notification provider registered for: {provider_type}") from None

    def list_providers(self) -> list[str]:
        """Return the sorted list of registered provider type identifiers."""
        return sorted(self._providers)


# Module-level singleton used by the delivery layer.
registry = ProviderRegistry()
