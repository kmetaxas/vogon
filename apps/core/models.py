# pyright: reportAttributeAccessIssue=false

import hashlib
import secrets
import uuid

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models


class Organization(models.Model):
    objects = models.Manager()

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    slug = models.SlugField(unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    mcp_api_token_hash = models.CharField(
        max_length=255,
        blank=True,
        help_text="SHA-256 hash of the MCP API token for this organization.",
    )
    email_domain = models.CharField(
        max_length=255,
        blank=True,
        help_text=(
            "If set, only users with email addresses matching this domain "
            "can join the organization (e.g. 'example.com')."
        ),
    )

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return str(self.name)

    def generate_mcp_token(self) -> str:
        token = f"vogon_{self.slug}_{secrets.token_hex(16)}"
        self.mcp_api_token_hash = hashlib.sha256(token.encode()).hexdigest()
        self.save(update_fields=["mcp_api_token_hash"])
        return token

    def validate_mcp_token(self, token: str) -> bool:
        if not token or not self.mcp_api_token_hash:
            return False
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        return secrets.compare_digest(token_hash, str(self.mcp_api_token_hash))


class User(AbstractUser):
    """Custom user model with direct organization link."""

    organizations = models.ManyToManyField(
        "core.Organization",
        through="core.OrganizationMembership",
        related_name="users",
    )

    USERNAME_FIELD = "username"
    REQUIRED_FIELDS = ["email"]

    class Meta:
        db_table = "auth_user"

    def get_current_organization(self):
        """Return the first organization the user belongs to."""
        return self.organizations.first()


class OrganizationMembership(models.Model):
    objects = models.Manager()

    class Role(models.TextChoices):
        OWNER = "owner", "Owner"
        ADMIN = "admin", "Admin"
        MEMBER = "member", "Member"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="memberships",
    )
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="memberships",
    )
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.MEMBER)
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ["user", "organization"]

    def __str__(self):
        return f"{self.user.username} – {self.organization.name} ({self.role})"

    def is_admin(self):
        return self.role in (self.Role.OWNER, self.Role.ADMIN)


class OrganizationInvitation(models.Model):
    objects = models.Manager()

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="invitations",
    )
    email = models.EmailField()
    code = models.CharField(max_length=64, unique=True, db_index=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="sent_invitations",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    accepted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="accepted_invitations",
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Invitation to {self.organization.name} for {self.email}"
