# pyright: reportAttributeAccessIssue=false

import uuid

from django.conf import settings
from django.db import models

from apps.core.models import Organization


class Personality(models.Model):
    """A reusable AI personality (system prompt) for troubleshooting sessions.

    Personalities are scoped in one of two ways:

    * **Organization-scoped** (``scope="organization"``): owned by a single
      ``Organization`` and visible only within that tenant. ``organization``
      is set and ``created_by`` records the author.
    * **System-scoped** (``scope="system"``): global, built-in personalities
      with ``organization=None``. These are managed exclusively via the Django
      admin and are visible to every organization.

    The ``unique_together`` constraint on ``(organization, name)`` relies on
    PostgreSQL treating NULLs as distinct, so multiple system-scoped
    personalities may share a name without colliding.
    """

    objects = models.Manager()

    class Scope(models.TextChoices):
        ORGANIZATION = "organization", "Organization"
        SYSTEM = "system", "System"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="personalities",
        help_text="Owning organization. Null for system-scoped personalities.",
    )
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    prompt_text = models.TextField()
    category = models.CharField(max_length=100, blank=True)
    tags = models.JSONField(default=list, blank=True)
    scope = models.CharField(
        max_length=20,
        choices=Scope.choices,
        default=Scope.ORGANIZATION,
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="personalities",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        unique_together = ["organization", "name"]

    def __str__(self):
        return str(self.name)
