import uuid

from django.conf import settings
from django.db import models

from apps.core.models import Organization
from apps.marvins.models import Capability


class TSession(models.Model):
    objects = models.Manager()

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        ACTIVE = "active", "Active"
        PAUSED = "paused", "Paused"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="tsessions",
    )
    title = models.CharField(max_length=255)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
    )
    temporal_workflow_id = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="tsessions",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    target_scope = models.ForeignKey(
        "marvins.TargetScope",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tsessions",
        help_text="Optional scope constraining all executions in this session.",
    )
    execution_budget = models.JSONField(
        default=dict,
        blank=True,
        help_text="Session-level execution budget. See SessionBudget schema.",
    )
    llm_provider = models.ForeignKey(
        "llm.LLMProvider",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tsessions",
        help_text="Optional LLM provider override for this session. Falls back to org default.",
    )

    class Meta:
        ordering = ["-created_at"]

    @classmethod
    def active_for_user(cls, user):
        if not getattr(user, "is_authenticated", False):
            return cls.objects.none()

        return cls.objects.filter(
            organization__in=user.organizations.all(),
            status__in=[cls.Status.PENDING, cls.Status.ACTIVE, cls.Status.PAUSED],
        )

    @classmethod
    def completed_for_user(cls, user):
        if not getattr(user, "is_authenticated", False):
            return cls.objects.none()

        return cls.objects.filter(
            organization__in=user.organizations.all(),
            status__in=[cls.Status.COMPLETED, cls.Status.FAILED],
        )

    def can_view(self, user):
        if not getattr(user, "is_authenticated", False):
            return False

        return user.organizations.filter(id=self.organization_id).exists()

    def __str__(self):
        return self.title

    def threads_visible_to(self, user):
        return Thread.visible_to_user(self, user)


class Thread(models.Model):
    objects = models.Manager()
    Status = TSession.Status

    class Visibility(models.TextChoices):
        PUBLIC = "public", "Public"
        PRIVATE = "private", "Private"
        SHARED = "shared", "Shared"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tsession = models.ForeignKey(
        TSession,
        on_delete=models.CASCADE,
        related_name="threads",
    )
    title = models.CharField(max_length=255, blank=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="threads",
    )
    temporal_workflow_id = models.CharField(max_length=255, blank=True)
    status = models.CharField(
        max_length=20,
        choices=TSession.Status.choices,
        default=TSession.Status.PENDING,
    )
    visibility = models.CharField(
        max_length=20,
        choices=Visibility.choices,
        default=Visibility.PUBLIC,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    @classmethod
    def get_or_create_for_user(cls, tsession, user):
        return cls.objects.get_or_create(tsession=tsession, user=user)

    def latest_messages(self):
        return self.messages.order_by("created_at")

    def can_view(self, user):
        if not getattr(user, "is_authenticated", False):
            return False
        if self.user_id == user.id:
            return True
        if self.visibility == self.Visibility.PUBLIC:
            return user.organizations.filter(id=self.tsession.organization_id).exists()
        if self.visibility == self.Visibility.SHARED:
            return self.memberships.filter(user=user).exists()
        return False

    def can_edit(self, user):
        if not getattr(user, "is_authenticated", False):
            return False
        return self.user_id == user.id

    def is_followed_by(self, user):
        if not getattr(user, "is_authenticated", False):
            return False
        return self.memberships.filter(user=user, is_following=True).exists()

    @classmethod
    def visible_to_user(cls, tsession, user):
        if not getattr(user, "is_authenticated", False):
            return cls.objects.none()
        qs = cls.objects.filter(tsession=tsession, user=user)
        qs |= cls.objects.filter(
            tsession=tsession,
            visibility=cls.Visibility.PUBLIC,
            tsession__organization__in=user.organizations.all(),
        )
        qs |= cls.objects.filter(
            tsession=tsession,
            visibility=cls.Visibility.SHARED,
            memberships__user=user,
        )
        return qs.distinct()

    def __str__(self):
        return f"Thread {self.id} in {self.tsession.title}"


class ThreadMembership(models.Model):
    """Explicit membership granting a user access to a thread."""

    objects = models.Manager()

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    thread = models.ForeignKey(
        Thread,
        on_delete=models.CASCADE,
        related_name="memberships",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="thread_memberships",
    )
    is_following = models.BooleanField(
        default=True,
        help_text="Whether the user wants this thread visible in their UI.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ["thread", "user"]
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.user.username} – {self.thread}"


class ToolCall(models.Model):
    objects = models.Manager()

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        IN_PROGRESS = "in_progress", "In Progress"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    thread = models.ForeignKey(
        Thread,
        on_delete=models.CASCADE,
        related_name="tool_calls",
    )
    capability = models.ForeignKey(
        Capability,
        on_delete=models.SET_NULL,
        null=True,
        related_name="tool_calls",
    )
    parameters = models.JSONField(default=dict)
    result = models.JSONField(default=dict, blank=True)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
    )
    labels = models.JSONField(default=dict, blank=True)
    marvin = models.ForeignKey(
        "marvins.Marvin",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tool_calls",
    )
    timeout_seconds = models.IntegerField(null=True, blank=True)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="requested_tool_calls",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    target_set = models.ForeignKey(
        "marvins.ResolvedTargetSet",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tool_calls",
        help_text="The resolved target set used for this execution.",
    )
    execution_mode = models.CharField(
        max_length=20,
        choices=[
            ("single", "Single"),
            ("fanout", "Fan-out"),
        ],
        default="single",
    )
    policy_snapshot = models.JSONField(
        default=dict,
        blank=True,
        help_text="ExecutionPolicy at time of dispatch (immutable audit).",
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"ToolCall {self.id} ({self.status})"


class Message(models.Model):
    objects = models.Manager()

    class Role(models.TextChoices):
        USER = "user", "User"
        ASSISTANT = "assistant", "Assistant"
        SYSTEM = "system", "System"
        TOOL = "tool", "Tool"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    thread = models.ForeignKey(
        Thread,
        on_delete=models.CASCADE,
        related_name="messages",
    )
    role = models.CharField(
        max_length=20,
        choices=Role.choices,
        default=Role.USER,
    )
    content = models.TextField()
    tool_call = models.ForeignKey(
        ToolCall,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="messages",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="messages",
    )
    metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text="Extra LLM-specific data: tool_call_id, tool_calls array, name, etc.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.get_role_display()} message in {self.thread}"


class ArchitectureRequest(models.Model):
    class Status(models.TextChoices):
        OPEN = "open", "Open"
        FULFILLED = "fulfilled", "Fulfilled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    thread = models.ForeignKey(
        Thread,
        on_delete=models.CASCADE,
        related_name="architecture_requests",
    )
    description = models.TextField()
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.OPEN,
    )
    diagram = models.FileField(upload_to="architecture/", null=True, blank=True)
    markdown = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    fulfilled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"ArchitectureRequest {self.id} ({self.status})"


class AgentEvent(models.Model):
    class Kind(models.TextChoices):
        THINKING = "thinking", "Thinking"
        TOOL_CALL_STARTED = "tool_call_started", "Tool Call Started"
        TOOL_RESULT = "tool_result", "Tool Result"
        ERROR = "error", "Error"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    thread = models.ForeignKey(
        Thread,
        on_delete=models.CASCADE,
        related_name="agent_events",
    )
    kind = models.CharField(max_length=30, choices=Kind.choices)
    label = models.CharField(max_length=255)
    detail = models.JSONField(default=dict, blank=True)
    tool_call = models.ForeignKey(
        ToolCall,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"AgentEvent {self.id} ({self.kind})"


class Execution(models.Model):
    """A single sub-execution within a ToolCall (fan-out unit)."""

    objects = models.Manager()

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tool_call = models.ForeignKey(
        ToolCall,
        on_delete=models.CASCADE,
        related_name="executions",
    )
    marvin = models.ForeignKey(
        "marvins.Marvin",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    result_index = models.PositiveIntegerField(
        default=0,
        help_text="Index within the fan-out result array.",
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
    )
    result = models.JSONField(default=dict, blank=True)
    error_message = models.TextField(blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["result_index"]
        indexes = [
            models.Index(fields=["tool_call", "status"]),
            models.Index(fields=["tool_call", "result_index"]),
        ]
