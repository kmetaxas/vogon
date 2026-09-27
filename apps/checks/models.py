# pyright: reportAttributeAccessIssue=false, reportOperatorIssue=false

import uuid
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
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

    def clean(self):
        from croniter import croniter

        super().clean()
        expression = (self.schedule_expression or "").strip()
        if not expression:
            raise ValidationError("Schedule expression cannot be empty.")

        if self.schedule_type == self.ScheduleType.CRON:
            if not croniter.is_valid(expression):
                raise ValidationError(f"Invalid cron expression: {expression}")
        elif self.schedule_type == self.ScheduleType.INTERVAL:
            try:
                seconds = int(expression)
            except (TypeError, ValueError):
                raise ValidationError("Interval must be a positive integer (seconds).")
            if seconds <= 0:
                raise ValidationError("Interval must be a positive integer (seconds).")


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

    @classmethod
    def last_for_check(cls, check_id: str) -> "CheckExecution | None":
        """Return the most recent execution for a Check."""
        try:
            return cls.objects.filter(check_id=check_id).order_by("-triggered_at").first()
        except Exception:
            return None

    @classmethod
    def last_successful_for_check(cls, check_id: str) -> "CheckExecution | None":
        """Return the most recent completed execution for a Check."""
        try:
            return (
                cls.objects.filter(
                    check_id=check_id,
                    execution_status=cls.ExecutionStatus.COMPLETED,
                )
                .order_by("-triggered_at")
                .first()
            )
        except Exception:
            return None

    @classmethod
    def is_missed(cls, check, expected_interval_seconds: int = 60) -> bool:
        """Detect if a Check has missed its expected execution window.

        A check is considered missed if:
        - It is enabled
        - Its last execution is older than expected_interval_seconds + 2x buffer
        """
        if not check.enabled:
            return False
        last = cls.last_for_check(str(check.id))
        if not last:
            return True  # Never executed
        from django.utils import timezone

        elapsed = (timezone.now() - last.triggered_at).total_seconds()
        return elapsed > (expected_interval_seconds * 3)

    @classmethod
    def concurrent_count_for_org(cls, organization) -> int:
        """Count currently running CheckExecutions for an organization."""
        try:
            return cls.objects.filter(
                check__organization=organization,
                execution_status=cls.ExecutionStatus.RUNNING,
            ).count()
        except Exception:
            return 0

    @classmethod
    def can_execute(cls, organization) -> bool:
        """Check if the organization can start a new Check execution.

        Respects CHECK_MAX_CONCURRENT_PER_ORG from settings.
        """
        from django.conf import settings

        max_concurrent = getattr(settings, "CHECK_MAX_CONCURRENT_PER_ORG", 5)
        current = cls.concurrent_count_for_org(organization)
        return current < max_concurrent


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
