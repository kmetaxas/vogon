import secrets
import string
import uuid

# pyright: reportAttributeAccessIssue=false
from django.db import models

from apps.core.models import Organization, User
from apps.marvins.fields import VectorField


def _generate_marvin_key() -> str:
    """Generate a long alphanumeric registration key (no symbols)."""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(64))


class ResourceType(models.Model):
    """Registry of supported external resource kinds."""

    objects = models.Manager()

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100, unique=True)
    display_name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    config_json_schema = models.JSONField(
        default=dict,
        help_text="JSON Schema that every Resource of this type must satisfy.",
    )
    capability_names = models.JSONField(
        default=list,
        help_text="List of capability names that can use this resource (e.g. ['kubernetes']).",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    scope_type = models.CharField(
        max_length=20,
        choices=[
            ("cluster", "Cluster"),
            ("namespace", "Namespace"),
            ("service", "Service"),
            ("host", "Host"),
            ("environment", "Environment"),
            ("generic", "Generic"),
        ],
        default="generic",
        help_text="What kind of logical boundary this resource represents.",
    )

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return str(self.display_name or self.name)


class Resource(models.Model):
    """An organization-scoped external system configuration."""

    objects = models.Manager()

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="resources",
    )
    name = models.CharField(max_length=255)
    resource_type = models.ForeignKey(
        ResourceType,
        on_delete=models.PROTECT,
        related_name="resources",
    )
    description = models.TextField(blank=True)
    config = models.JSONField(
        default=dict,
        help_text="Connection parameters validated against ResourceType.config_json_schema.",
    )
    secrets_encrypted = models.JSONField(
        default=dict,
        blank=True,
        help_text="Sensitive values encrypted at rest (optional, v2).",
    )
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        unique_together = ["organization", "name"]

    def __str__(self):
        return f"{self.name} ({self.resource_type.name})"


class MarvinResourceAttachment(models.Model):
    """Through model linking Marvin to Resource with audit fields."""

    objects = models.Manager()

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    marvin = models.ForeignKey(
        "Marvin",
        on_delete=models.CASCADE,
        related_name="resource_attachments",
    )
    resource = models.ForeignKey(
        Resource,
        on_delete=models.CASCADE,
        related_name="marvin_attachments",
    )
    attached_at = models.DateTimeField(auto_now_add=True)
    attached_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )

    class Meta:
        unique_together = ["marvin", "resource"]

    def __str__(self):
        return f"{self.marvin.name} <-> {self.resource.name}"


class Capability(models.Model):
    objects = models.Manager()

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="capabilities",
    )
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    json_schema = models.JSONField(
        default=dict,
        help_text="JSON Schema for capability parameters",
    )
    keywords = models.JSONField(
        default=list,
        blank=True,
        help_text="Search keywords for find_tools",
    )
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    execution_scope = models.CharField(
        max_length=20,
        choices=[
            ("marvin-local", "Marvin Local"),
            ("host", "Host"),
            ("cluster", "Cluster"),
            ("namespace", "Namespace"),
            ("service", "Service"),
            ("environment", "Environment"),
        ],
        default="marvin-local",
        help_text="Logical scope this capability operates on.",
    )
    selection_strategy = models.CharField(
        max_length=20,
        choices=[
            ("one", "One"),
            ("all", "All"),
            ("preferred", "Preferred"),
            ("leader", "Leader"),
            ("quorum", "Quorum"),
        ],
        default="one",
        help_text="How many executors to select from eligible Marvins.",
    )
    execution_policy = models.JSONField(
        default=dict,
        blank=True,
        help_text="Server-side execution limits. See ExecutionPolicy schema.",
    )

    use_cases = models.JSONField(
        default=list,
        blank=True,
        help_text="Retrieval-oriented 'useful for' descriptions. List of strings.",
    )
    aliases = models.JSONField(
        default=list,
        blank=True,
        help_text="Alternative names and abbreviations. List of strings.",
    )
    tags = models.JSONField(
        default=list,
        blank=True,
        help_text="Structured tags for filtering. List of strings.",
    )
    provider = models.CharField(
        max_length=50,
        blank=True,
        db_index=True,
        help_text=(
            "Capability provider (e.g. kafka, kubernetes, network). Denormalized from name prefix."
        ),
    )
    search_document = models.TextField(
        blank=True,
        help_text="Canonical retrieval-oriented document constructed from metadata.",
    )
    search_document_version = models.PositiveIntegerField(
        default=0,
        help_text="Monotonic version of search_document. Triggers re-embedding on change.",
    )
    embedding = VectorField(
        dimensions=None,
        null=True,
        blank=True,
        help_text="Vector embedding of search_document.",
    )
    embedding_model = models.CharField(
        max_length=100,
        blank=True,
        help_text="Embedding model used (e.g. 'text-embedding-3-small').",
    )
    embedding_version = models.PositiveIntegerField(
        default=0,
        help_text="Embedding generation version (monotonic, increments on model change).",
    )
    needs_re_embedding = models.BooleanField(
        default=False,
        db_index=True,
        help_text="True when search_document_version > embedding_version; set by save().",
    )

    class Meta:
        ordering = ["name"]
        unique_together = ["organization", "name"]

    def __str__(self):
        return str(self.name)

    def save(self, *args, **kwargs):
        name_str = str(self.name) if self.name else ""
        if name_str and not self.provider:
            self.provider = name_str.split(".")[0] if "." in name_str else ""
        self._build_search_document()
        self.needs_re_embedding = self.search_document_version > self.embedding_version
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            update_fields = list(update_fields)
            if "search_document" not in update_fields:
                update_fields.append("search_document")
            if "search_document_version" not in update_fields:
                update_fields.append("search_document_version")
            if "needs_re_embedding" not in update_fields:
                update_fields.append("needs_re_embedding")
            kwargs["update_fields"] = update_fields
        super().save(*args, **kwargs)

    def _build_search_document(self) -> None:
        use_cases = list(getattr(self, "use_cases", None) or [])
        aliases = list(getattr(self, "aliases", None) or [])
        tags = list(getattr(self, "tags", None) or [])
        parts = [
            f"Capability: {self.name}",
            f"Provider: {self.provider}",
            f"Description: {self.description}",
        ]
        if use_cases:
            parts.append("\nUseful for:")
            for uc in use_cases:
                parts.append(f"- {uc}")
        if self.json_schema and isinstance(self.json_schema, dict):
            props = self.json_schema.get("properties", {})
            if props:
                parts.append("\nParameters:")
                for pname, pdef in props.items():
                    desc = pdef.get("description", "")
                    if desc:
                        parts.append(f"- {pname}: {desc}")
        if tags:
            parts.append(f"\nTags: {', '.join(tags)}")
        if aliases:
            parts.append(f"Aliases: {', '.join(aliases)}")
        new_doc = "\n".join(parts)
        if self.search_document != new_doc:
            self.search_document = new_doc
            self.search_document_version += 1


class Marvin(models.Model):
    objects = models.Manager()

    class Status(models.TextChoices):
        ONLINE = "online", "Online"
        OFFLINE = "offline", "Offline"
        BUSY = "busy", "Busy"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="marvins",
    )
    name = models.CharField(max_length=255)
    client_id = models.CharField(max_length=255, unique=True)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.OFFLINE,
    )
    last_seen = models.DateTimeField(null=True, blank=True)
    capabilities = models.ManyToManyField(
        Capability,
        related_name="marvins",
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Agent metadata reported at registration
    agent_version = models.CharField(max_length=100, blank=True)
    labels = models.JSONField(default=list, blank=True)

    # Host metadata for routing
    hostname = models.CharField(max_length=255, blank=True)
    local_ip = models.CharField(max_length=100, blank=True)
    provider = models.CharField(max_length=50, blank=True)
    region = models.CharField(max_length=100, blank=True)
    availability_zone = models.CharField(max_length=100, blank=True)
    vm_id = models.CharField(max_length=255, blank=True)
    os = models.CharField(max_length=50, blank=True)
    os_version = models.CharField(max_length=100, blank=True)
    arch = models.CharField(max_length=50, blank=True)

    # Per-Marvin capability configurations.
    # Maps capability name -> config dict (e.g. {"kinit": {"user": "foo", "realm": "EXAMPLE.COM"}})
    capability_configs = models.JSONField(default=dict, blank=True)

    # Many-to-many to Resource via through model
    attached_resources = models.ManyToManyField(
        Resource,
        related_name="marvins",
        blank=True,
        through="MarvinResourceAttachment",
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.name} ({self.status})"


class MarvinConfig(models.Model):
    """Desired configuration for a Marvin agent, managed from the control plane."""

    objects = models.Manager()

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    marvin = models.OneToOneField(
        Marvin,
        on_delete=models.CASCADE,
        related_name="online_config",
    )
    global_config = models.JSONField(
        default=dict,
        blank=True,
        help_text="Marvin-wide settings (e.g. log_level, default_timeout_seconds).",
    )
    capability_overrides = models.JSONField(
        default=dict,
        blank=True,
        help_text="Capability-specific overrides set from the control plane.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Config for {self.marvin.name}"


class MarvinRegistrationKey(models.Model):
    objects = models.Manager()

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="marvin_registration_keys",
    )
    key = models.CharField(
        max_length=64,
        unique=True,
        default=_generate_marvin_key,
        editable=False,
        help_text="Auto-generated alphanumeric registration key for Marvin agents.",
    )
    name = models.CharField(
        max_length=255,
        blank=True,
        help_text="Optional descriptive name for this key (e.g., 'Production Cluster').",
    )
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        key_prefix = self.key[:8]  # type: ignore[index]
        return f"{self.name or key_prefix}... ({self.organization.name})"


class TargetScope(models.Model):
    """A reusable, named selector constraining a troubleshooting session."""

    objects = models.Manager()

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="target_scopes",
    )
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=100)
    description = models.TextField(blank=True)
    selector = models.JSONField(
        default=dict,
        help_text="JSON TargetSelector dict. AND semantics across keys.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ["organization", "slug"]
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.organization.slug})"


class ResolvedTargetSet(models.Model):
    """Immutable snapshot of Marvins matched for a capability at a point in time."""

    objects = models.Manager()

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="resolved_target_sets",
    )
    capability = models.ForeignKey(
        Capability,
        on_delete=models.CASCADE,
        related_name="resolved_target_sets",
    )
    selector = models.JSONField(default=dict, help_text="The selector that produced this set.")
    session_scope = models.ForeignKey(
        TargetScope,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="resolved_target_sets",
    )
    snapshot = models.JSONField(
        default=list,
        help_text="List of Marvin UUIDs at resolution time. Immutable.",
    )
    snapshot_metadata = models.JSONField(
        default=dict,
        help_text="Summary: count, sample labels, fanout_class, etc.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(
        help_text="TTL after which this snapshot is considered stale.",
    )
    resolved_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="resolved_target_sets",
    )

    class Meta:
        indexes = [
            models.Index(fields=["organization", "expires_at"]),
            models.Index(fields=["capability", "created_at"]),
        ]

    def __str__(self):
        return f"TargetSet for {self.capability.name} ({self.organization.name})"
