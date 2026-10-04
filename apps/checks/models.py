# pyright: reportAttributeAccessIssue=false, reportOperatorIssue=false

import uuid
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from pydantic import ValidationError as PydanticValidationError

from apps.core.models import Organization

# Keys accepted inside a capability ``selector`` dict. Mirrors the fields of
# ``apps.marvins.selectors.TargetSelector`` so configs stay compatible with the
# discovery engine.
SELECTOR_LIST_FIELDS = (
    "hostname",
    "region",
    "availability_zone",
    "provider",
    "resource_ids",
    "marvin_ids",
)
SELECTOR_ALLOWED_KEYS = SELECTOR_LIST_FIELDS + ("labels",)


def validate_capability_selector(selector, *, index=None):
    """Validate an optional capability selector dict.

    The selector is optional: ``None`` and ``{}`` are accepted. When present it
    must be a JSON object whose keys are a subset of ``SELECTOR_ALLOWED_KEYS``.
    List fields accept either a single string or a list of strings; ``labels``
    must be an object mapping string keys to a string or list of strings.

    Returns the selector unchanged so callers can chain validation.
    """
    prefix = f"capabilities[{index}].selector: " if index is not None else "selector: "
    if selector is None:
        return selector
    if not isinstance(selector, dict):
        raise ValidationError(f"{prefix}must be a JSON object.")

    for key, value in selector.items():
        if key not in SELECTOR_ALLOWED_KEYS:
            raise ValidationError(f"{prefix}unknown key '{key}'.")
        if key == "labels":
            if not isinstance(value, dict):
                raise ValidationError(f"{prefix}'labels' must be a JSON object.")
            for label_key, label_value in value.items():
                if not isinstance(label_key, str):
                    raise ValidationError(f"{prefix}'labels' keys must be strings.")
                if isinstance(label_value, list):
                    if not all(isinstance(item, str) for item in label_value):
                        raise ValidationError(
                            f"{prefix}'labels.{label_key}' must be a string or list of strings."
                        )
                elif not isinstance(label_value, str):
                    raise ValidationError(
                        f"{prefix}'labels.{label_key}' must be a string or list of strings."
                    )
        elif isinstance(value, str):
            continue
        elif isinstance(value, list) and all(isinstance(item, str) for item in value):
            continue
        else:
            raise ValidationError(f"{prefix}'{key}' must be a string or list of strings.")

    return selector


def validate_capability_selectors(evaluation_config):
    """Validate the optional ``selector`` on every capability entry.

    Configs without a ``capabilities`` list, or whose entries omit ``selector``,
    are accepted unchanged for backward compatibility.
    """
    if not isinstance(evaluation_config, dict):
        return evaluation_config
    capabilities = evaluation_config.get("capabilities")
    if not capabilities:
        return evaluation_config
    if not isinstance(capabilities, list):
        raise ValidationError("evaluation_config.capabilities must be a list.")
    for index, capability in enumerate(capabilities):
        if not isinstance(capability, dict):
            raise ValidationError(f"capabilities[{index}] must be a JSON object.")
        if "selector" in capability:
            validate_capability_selector(capability.get("selector"), index=index)
    return evaluation_config


def normalize_execution_budget(budget):
    from apps.sessions.budget import SessionBudget

    if not budget:
        return SessionBudget().to_dict()

    if not isinstance(budget, dict):
        raise ValidationError("execution_budget must be a JSON object.")

    if "max_iterations" in budget:
        budget = {**budget, "max_executions_per_session": budget.pop("max_iterations")}

    try:
        return SessionBudget(**budget).to_dict()
    except PydanticValidationError as exc:
        raise ValidationError(f"execution_budget: {exc}") from exc


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
    instructions = models.TextField(
        blank=True,
        help_text=(
            "System prompt or instructions for the LLM when evaluating this check. "
            "Used in AI-Assisted and Autonomous modes."
        ),
    )
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

        validate_capability_selectors(self.evaluation_config)
        if self.execution_budget is not None:
            self.execution_budget = normalize_execution_budget(self.execution_budget)


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


class CheckCapabilityExecution(models.Model):
    """Per-Marvin execution tracking during a Check fanout."""

    objects = models.Manager()

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    check_execution = models.ForeignKey(
        CheckExecution,
        on_delete=models.CASCADE,
        related_name="capability_executions",
    )
    capability_name = models.CharField(max_length=255)
    marvin = models.ForeignKey(
        "marvins.Marvin",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="check_capability_executions",
    )
    target_set = models.ForeignKey(
        "marvins.ResolvedTargetSet",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="check_capability_executions",
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
    )
    result_json = models.JSONField(default=dict, blank=True)
    error_message = models.TextField(blank=True)
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    cost = models.DecimalField(max_digits=14, decimal_places=6, default=Decimal("0.00"))
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["check_execution", "status"]),
        ]

    def __str__(self):
        return f"{self.capability_name} ({self.status})"


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
