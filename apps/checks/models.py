# pyright: reportAttributeAccessIssue=false

import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models

from apps.core.models import Organization


class Check(models.Model):
    objects = models.Manager()

    class ScheduleType(models.TextChoices):
        INTERVAL = "interval", "Interval"
        CRON = "cron", "Cron"

    class ExecutionMode(models.TextChoices):
        DETERMINISTIC = "deterministic", "Deterministic"
        AI_ASSISTED = "ai_assisted", "AI Assisted"
        AUTONOMOUS = "autonomous", "Autonomous"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="checks",
    )
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    enabled = models.BooleanField(default=True)
    schedule_type = models.CharField(
        max_length=20,
        choices=ScheduleType.choices,
    )
    schedule_expression = models.CharField(
        max_length=255,
        help_text="e.g. '60' for interval seconds, or '0 9 * * *' for cron",
    )
    timezone = models.CharField(max_length=50, default="UTC")
    execution_mode = models.CharField(
        max_length=20,
        choices=ExecutionMode.choices,
    )
    evaluation_config = models.JSONField(
        default=dict,
        blank=True,
        help_text="Rules and thresholds for deterministic evaluation",
    )
    notification_config = models.JSONField(
        default=dict,
        blank=True,
        help_text="Action configuration: channels, conditions, recipients",
    )
    execution_budget = models.JSONField(
        default=dict,
        blank=True,
        help_text="Max iterations, token limits, timeout settings",
    )
    llm_provider = models.ForeignKey(
        "llm.LLMProvider",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="checks",
    )
    target_scope = models.ForeignKey(
        "marvins.TargetScope",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="checks",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="created_checks",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        unique_together = ["organization", "name"]

    def __str__(self):
        return f"{self.name} ({self.organization.slug})"


class CheckVersion(models.Model):
    objects = models.Manager()

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    check = models.ForeignKey(
        Check,
        on_delete=models.CASCADE,
        related_name="versions",
    )
    version_number = models.PositiveIntegerField()
    definition_snapshot = models.JSONField(
        default=dict,
        help_text="Immutable snapshot of Check config at creation time",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-version_number"]

    def __str__(self):
        return f"Version {self.version_number} of {self.check.name}"


class CheckExecution(models.Model):
    objects = models.Manager()

    class ExecutionStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"
        QUEUED = "queued", "Queued"

    class HealthState(models.TextChoices):
        HEALTHY = "healthy", "Healthy"
        DEGRADED = "degraded", "Degraded"
        CRITICAL = "critical", "Critical"
        UNKNOWN = "unknown", "Unknown"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    check = models.ForeignKey(
        Check,
        on_delete=models.CASCADE,
        related_name="executions",
    )
    version = models.ForeignKey(
        CheckVersion,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="executions",
    )
    triggered_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    execution_status = models.CharField(
        max_length=20,
        choices=ExecutionStatus.choices,
        default=ExecutionStatus.PENDING,
    )
    health_state = models.CharField(
        max_length=20,
        choices=HealthState.choices,
        default=HealthState.UNKNOWN,
    )
    evaluation_result = models.JSONField(default=dict, blank=True)
    evidence = models.JSONField(
        default=dict,
        blank=True,
        help_text="Collected capability results and LLM context",
    )
    resolved_targets = models.JSONField(
        default=dict,
        blank=True,
        help_text="Marvin IDs and metadata at execution time",
    )
    error_message = models.TextField(blank=True)
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    cost = models.DecimalField(max_digits=14, decimal_places=6, default=Decimal("0.00"))
    actions_triggered = models.JSONField(default=dict, blank=True)
    dry_run = models.BooleanField(default=False)

    class Meta:
        ordering = ["-triggered_at"]
        indexes = [
            models.Index(fields=["check", "execution_status"]),
            models.Index(fields=["check", "health_state"]),
        ]

    def __str__(self):
        return f"Execution {self.id} of {self.check.name} ({self.execution_status})"


class CheckHealthState(models.Model):
    objects = models.Manager()

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    check = models.OneToOneField(
        Check,
        on_delete=models.CASCADE,
        related_name="health_state",
    )
    current_state = models.CharField(
        max_length=20,
        choices=CheckExecution.HealthState.choices,
        default=CheckExecution.HealthState.UNKNOWN,
    )
    previous_state = models.CharField(
        max_length=20,
        choices=CheckExecution.HealthState.choices,
        default=CheckExecution.HealthState.UNKNOWN,
    )
    state_changed_at = models.DateTimeField(null=True, blank=True)
    consecutive_failures = models.PositiveIntegerField(default=0)
    consecutive_successes = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.check.name}: {self.current_state}"


class CheckActionLog(models.Model):
    objects = models.Manager()

    class ActionType(models.TextChoices):
        EMAIL = "email", "Email"
        WEBHOOK = "webhook", "Webhook"
        TEAMS = "teams", "Teams"
        ALERTMANAGER = "alertmanager", "Alertmanager"

    class DeliveryStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"
        SUPPRESSED = "suppressed", "Suppressed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    check = models.ForeignKey(
        Check,
        on_delete=models.CASCADE,
        related_name="action_logs",
    )
    execution = models.ForeignKey(
        CheckExecution,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="action_logs",
    )
    action_type = models.CharField(
        max_length=20,
        choices=ActionType.choices,
    )
    recipient = models.CharField(max_length=500, blank=True)
    payload = models.JSONField(default=dict, blank=True)
    delivery_status = models.CharField(
        max_length=20,
        choices=DeliveryStatus.choices,
        default=DeliveryStatus.PENDING,
    )
    sent_at = models.DateTimeField(null=True, blank=True)
    error_message = models.TextField(blank=True)
    retry_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.action_type} to {self.recipient} ({self.delivery_status})"
