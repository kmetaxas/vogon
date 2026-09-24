# pyright: reportAttributeAccessIssue=false

import uuid

from django.conf import settings
from django.db import models

from apps.core.models import Organization
from apps.marvins.fields import VectorField


class InfrastructureDesign(models.Model):
    """A reusable infrastructure topology design stored in the control plane.

    Each design is scoped to an Organization, embeddable for LLM retrieval,
    and carries a mermaid diagram + Marvin label selector for targeting.
    """

    objects = models.Manager()

    class Environment(models.TextChoices):
        PRODUCTION = "production", "Production"
        STAGING = "staging", "Staging"
        DEVELOPMENT = "development", "Development"
        TESTING = "testing", "Testing"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="infrastructure_designs",
    )
    name = models.CharField(max_length=255)
    description = models.TextField(
        blank=True,
        help_text="Textual description of the infrastructure. Used for embedding search.",
    )
    environment = models.CharField(
        max_length=20,
        choices=Environment.choices,
        default=Environment.PRODUCTION,
        help_text="Deployment environment this design represents.",
    )
    mermaid_topology = models.TextField(
        blank=True,
        help_text="Mermaid diagram source describing the infrastructure topology.",
    )
    marvin_selector = models.JSONField(
        default=dict,
        blank=True,
        help_text=(
            "JSON label selector dict (same format as TargetScope.selector). "
            "AND semantics across keys."
        ),
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="infrastructure_designs",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # -- Embedding fields (mirrors Capability pattern) --
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
        ordering = ["-created_at"]
        unique_together = ["organization", "name"]

    def __str__(self):
        return f"{self.name} ({self.environment})"

    def save(self, *args, **kwargs):
        self._build_search_document()
        self.needs_re_embedding = self.search_document_version > self.embedding_version
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            update_fields = list(update_fields)
            for fld in ("search_document", "search_document_version", "needs_re_embedding"):
                if fld not in update_fields:
                    update_fields.append(fld)
            kwargs["update_fields"] = update_fields
        super().save(*args, **kwargs)

    def _build_search_document(self) -> None:
        parts = [
            f"Design: {self.name}",
            f"Environment: {self.get_environment_display()}",
            f"Description: {self.description}",
        ]
        selector = self.marvin_selector or {}
        labels = selector.get("labels")
        if labels and isinstance(labels, dict):
            label_parts = [f"{k}={v}" for k, v in labels.items() if v]
            if label_parts:
                parts.append(f"Marvin labels: {', '.join(label_parts)}")
        new_doc = "\n".join(parts)
        if self.search_document != new_doc:
            self.search_document = new_doc
            self.search_document_version += 1
