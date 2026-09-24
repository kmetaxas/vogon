"""Execution policies for Marvin capabilities."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, Field


class ExecutionPolicy(BaseModel):
    """Server-side execution limits for a capability."""

    model_config = {"extra": "ignore"}

    cost_class: str = Field(
        default="medium",
        pattern="^(low|medium|high|critical)$",
        description="Cost tier for this capability.",
    )
    default_max_targets: int = Field(
        default=10,
        ge=1,
        le=10000,
        description="Default max number of target hosts.",
    )
    absolute_max_targets: int = Field(
        default=100,
        ge=1,
        le=10000,
        description="Hard max number of target hosts.",
    )
    default_concurrency: int = Field(
        default=5,
        ge=1,
        le=100,
        description="Default concurrency for execution.",
    )
    timeout_seconds: int = Field(
        default=300,
        ge=1,
        le=3600,
        description="Timeout for execution in seconds.",
    )
    requires_resource: bool = Field(
        default=False,
        description="Whether execution requires a named resource.",
    )

    @classmethod
    def from_capability(cls, capability: Any) -> ExecutionPolicy:
        """Build an ExecutionPolicy from a Capability instance.

        Reads ``capability.execution_policy``; empty or None dicts fall back
        to field defaults.
        """
        policy: Mapping[str, Any] | None = capability.execution_policy
        if not policy:
            return cls()
        return cls(**policy)
