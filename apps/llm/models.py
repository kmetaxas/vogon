import uuid
from decimal import Decimal

from django.db import models

from apps.core.models import Organization


class LLMProvider(models.Model):
    class ProviderType(models.TextChoices):
        OLLAMA = "ollama", "Ollama (Cloud or local)"
        OPENAI = "openai", "OpenAI"
        OPENAI_COMPAT = "openai_compat", "OpenAI-compatible (vLLM, etc.)"
        ANTHROPIC = "anthropic", "Anthropic"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="llm_providers",
    )
    name = models.CharField(max_length=255)
    provider_type = models.CharField(
        max_length=30,
        choices=ProviderType.choices,
        default=ProviderType.OLLAMA,
    )
    base_url = models.CharField(
        max_length=500,
        blank=True,
        help_text="e.g. https://ollama.com/v1",
    )
    _api_key_encrypted = models.CharField(
        max_length=500,
        blank=True,
        db_column="api_key",
        help_text="Encrypted at rest via Fernet.",
    )
    model = models.CharField(max_length=255)
    is_default = models.BooleanField(default=False)
    is_jev = models.BooleanField(
        default=False,
        help_text=(
            "Whether this provider uses the JEV (/v1/systemone) API "
            "instead of standard chat completions."
        ),
    )
    is_jev_default = models.BooleanField(
        default=False,
        help_text=(
            "Whether this provider is the organization's default JEV provider for admission gates."
        ),
    )
    enabled = models.BooleanField(default=True)
    config = models.JSONField(
        default=dict,
        blank=True,
        help_text="temperature, max_tokens, top_p",
    )
    cost_per_1m_input_tokens = models.DecimalField(
        max_digits=12, decimal_places=6, default=Decimal("0.00")
    )
    cost_per_1m_output_tokens = models.DecimalField(
        max_digits=12, decimal_places=6, default=Decimal("0.00")
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization"],
                condition=models.Q(is_default=True),
                name="one_default_llm_per_org",
            ),
            models.UniqueConstraint(
                fields=["organization"],
                condition=models.Q(is_jev_default=True),
                name="one_jev_default_per_org",
            ),
        ]

    @property
    def api_key(self) -> str:
        from services.crypto import maybe_decrypt

        return maybe_decrypt(self._api_key_encrypted)

    @api_key.setter
    def api_key(self, value: str) -> None:
        from services.crypto import encrypt

        self._api_key_encrypted = encrypt(value)

    def __str__(self):
        return self.name
