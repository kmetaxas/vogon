# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportGeneralTypeIssues=false, reportIncompatibleMethodOverride=false, reportOperatorIssue=false, reportIndexIssue=false

import uuid

from django.core.exceptions import ValidationError
from django.db import models

from apps.core.models import Organization
from apps.notifications.crypto_fields import EncryptedJSONField


class NotificationProvider(models.Model):
    objects = models.Manager()

    provider_type = models.CharField(max_length=30, unique=True)
    display_name = models.CharField(max_length=100)
    json_schema = models.JSONField(default=dict)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return str(self.display_name)


class NotificationChannel(models.Model):
    objects = models.Manager()

    class ProviderType(models.TextChoices):
        EMAIL = "email", "Email"
        TEAMS = "teams", "Microsoft Teams"
        SLACK = "slack", "Slack"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="notification_channels",
    )
    name = models.CharField(max_length=255)
    provider_type = models.CharField(max_length=30, choices=ProviderType.choices)
    config = models.JSONField(
        default=dict,
        blank=True,
        help_text="Non-secret configuration such as display name.",
    )
    _credentials_encrypted = models.TextField(
        blank=True,
        db_column="credentials",
        help_text="Encrypted JSON dict of secret fields.",
    )
    credentials = EncryptedJSONField("_credentials_encrypted")
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        unique_together = ("organization", "name")

    def __str__(self):
        return self.name

    @property
    def merged_config(self) -> dict:
        config = self.config if isinstance(self.config, dict) else {}
        credentials = self.credentials if isinstance(self.credentials, dict) else {}
        return {**config, **credentials}

    @property
    def config_schema(self) -> dict:
        from services.notifications.registry import registry

        try:
            provider = registry.get(self.provider_type)
        except KeyError:
            return {"type": "object", "properties": {}}
        schema = getattr(provider, "config_schema", None)
        if isinstance(schema, dict):
            return schema
        return {"type": "object", "properties": {}}

    def clean(self):
        super().clean()
        from services.notifications.registry import registry

        try:
            provider = registry.get(self.provider_type)()
        except KeyError:
            return
        errors = provider.validate_config(self.merged_config)
        if errors:
            raise ValidationError({"config": errors})


class NotificationPolicy(models.Model):
    objects = models.Manager()

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="notification_policies",
    )
    name = models.CharField(max_length=255)
    is_default = models.BooleanField(default=False)
    dedup_window_seconds = models.PositiveIntegerField(default=300)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        unique_together = ("organization", "name")
        constraints = [
            models.UniqueConstraint(
                fields=["organization"],
                condition=models.Q(is_default=True),
                name="one_default_policy_per_org",
            )
        ]

    def __str__(self):
        return self.name


class NotificationRoute(models.Model):
    objects = models.Manager()

    class Severity(models.TextChoices):
        INFO = "info", "Info"
        WARNING = "warning", "Warning"
        CRITICAL = "critical", "Critical"

    class Attention(models.TextChoices):
        NORMAL = "normal", "Normal"
        IMMEDIATE = "immediate", "Immediate"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    policy = models.ForeignKey(
        NotificationPolicy,
        on_delete=models.CASCADE,
        related_name="routes",
    )
    severity = models.CharField(
        max_length=20,
        blank=True,
        null=True,
        choices=Severity.choices,
    )
    attention = models.CharField(
        max_length=20,
        blank=True,
        null=True,
        choices=Attention.choices,
    )
    channel = models.ForeignKey(
        NotificationChannel,
        on_delete=models.CASCADE,
        related_name="routes",
    )
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.policy.name} -> {self.channel.name}"


class Notification(models.Model):
    objects = models.Manager()

    class Severity(models.TextChoices):
        INFO = "info", "Info"
        WARNING = "warning", "Warning"
        CRITICAL = "critical", "Critical"

    class Attention(models.TextChoices):
        NORMAL = "normal", "Normal"
        IMMEDIATE = "immediate", "Immediate"

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        SUPPRESSED = "suppressed", "Suppressed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="notifications",
    )
    policy = models.ForeignKey(
        NotificationPolicy,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="notifications",
    )
    severity = models.CharField(max_length=20, choices=Severity.choices)
    attention = models.CharField(max_length=20, choices=Attention.choices)
    title = models.CharField(max_length=500)
    summary = models.TextField()
    details = models.TextField(blank=True)
    source_type = models.CharField(max_length=100)
    source_id = models.CharField(max_length=255, blank=True)
    context = models.JSONField(default=dict, blank=True)
    dedup_key = models.CharField(max_length=64, db_index=True)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.OPEN,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.title


class NotificationDelivery(models.Model):
    objects = models.Manager()

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SENDING = "sending", "Sending"
        DELIVERED = "delivered", "Delivered"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    notification = models.ForeignKey(
        Notification,
        on_delete=models.CASCADE,
        related_name="deliveries",
    )
    channel = models.ForeignKey(
        NotificationChannel,
        on_delete=models.CASCADE,
        related_name="deliveries",
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
    )
    attempt_count = models.PositiveIntegerField(default=0)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    external_provider_id = models.CharField(max_length=255, blank=True)
    error_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.notification.title} -> {self.channel.name}"


class NotificationSuppression(models.Model):
    objects = models.Manager()

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="notification_suppressions",
    )
    dedup_key = models.CharField(max_length=64, db_index=True)
    notification = models.ForeignKey(
        Notification,
        on_delete=models.CASCADE,
        related_name="caused_suppressions",
    )
    suppressed_notification = models.ForeignKey(
        Notification,
        on_delete=models.CASCADE,
        related_name="was_suppressed_by",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Suppression: {self.dedup_key[:16]}..."
