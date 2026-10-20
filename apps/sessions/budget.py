"""SessionBudget Pydantic model for per-session execution budget tracking.

Tracks tool execution and target usage against a session's configured limits.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from django.db.models.expressions import F  # noqa: F401
from django.db.transaction import atomic
from pydantic import BaseModel, Field

DEFAULT_EXECUTION_BUDGET: dict[str, Any] = {
    "max_executions_per_session": 100,
    "max_targets_per_session": 200,
    "max_concurrent_executions": 5,
    "executions_used": 0,
    "targets_used": 0,
    "concurrent_running": 0,
}


class SessionBudget(BaseModel):
    """Tracks execution and target usage against a session's configured limits.

    Persisted to ``TSession.execution_budget`` (a ``JSONField``) via ``to_dict``
    and reconstructed from that dict via :meth:`from_session`.
    """

    max_executions_per_session: int = Field(default=100, ge=1)
    max_targets_per_session: int = Field(default=200, ge=1)
    max_concurrent_executions: int = Field(default=5, ge=1)
    executions_used: int = Field(default=0, ge=0)
    targets_used: int = Field(default=0, ge=0)
    concurrent_running: int = Field(default=0, ge=0)
    max_tokens_per_session: int = Field(default=0, ge=0)
    max_cost_per_session: Decimal = Field(default=Decimal("0.00"))
    max_context_tokens: int = Field(default=0, ge=0)
    tokens_used: int = Field(default=0, ge=0)
    cost_used: Decimal = Field(default=Decimal("0.00"))
    context_tokens_used: int = Field(default=0, ge=0)

    @classmethod
    def from_session(cls, session: Any) -> SessionBudget:
        """Build a :class:`SessionBudget` from a ``TSession`` instance.

        Reads ``session.execution_budget`` (a dict or None). If it is empty or
        missing, defaults are used.
        """
        budget: dict[str, Any] = session.execution_budget or {}
        return cls(**budget)

    def to_dict(self) -> dict[str, Any]:
        """Return a plain dict suitable for saving to the JSONField."""
        raw = self.model_dump()
        if "max_cost_per_session" in raw:
            raw["max_cost_per_session"] = str(raw["max_cost_per_session"])
        if "cost_used" in raw:
            raw["cost_used"] = str(raw["cost_used"])
        return raw


class BudgetExceeded(Exception):  # noqa: N818
    pass


class OrgConcurrencyLimitExceeded(Exception):  # noqa: N818
    """Raised when an organization's concurrent execution ceiling is reached."""

    pass


def check_org_concurrent_execution_limit(organization):
    """Check if the organization has reached its concurrent execution ceiling."""
    from apps.sessions.models import ToolCall

    in_progress_count = ToolCall.objects.filter(
        thread__tsession__organization=organization,
        status__in=[ToolCall.Status.PENDING, ToolCall.Status.IN_PROGRESS],
    ).count()
    if in_progress_count >= organization.max_concurrent_executions:
        raise OrgConcurrencyLimitExceeded(
            f"Organization has reached its concurrent execution limit "
            f"({organization.max_concurrent_executions})."
        )


def check_and_reserve_budget(session: Any, target_count: int) -> SessionBudget:
    with atomic():
        locked_session = session.__class__._default_manager.select_for_update().get(pk=session.pk)
        budget = SessionBudget.from_session(locked_session)

        if budget.executions_used >= budget.max_executions_per_session:
            raise BudgetExceeded("Session execution budget is exhausted.")
        if budget.targets_used + target_count > budget.max_targets_per_session:
            raise BudgetExceeded("Session target budget would be exceeded.")
        if budget.concurrent_running >= budget.max_concurrent_executions:
            raise BudgetExceeded("Session concurrent execution budget is exhausted.")

        budget.executions_used += 1
        budget.targets_used += target_count
        budget.concurrent_running += 1
        locked_session.execution_budget = budget.to_dict()
        locked_session.save(update_fields=["execution_budget"])

    session.execution_budget = budget.to_dict()
    return budget


def release_execution_budget(session: Any, target_count: int) -> SessionBudget:
    with atomic():
        locked_session = session.__class__._default_manager.select_for_update().get(pk=session.pk)
        budget = SessionBudget.from_session(locked_session)

        budget.concurrent_running = max(0, budget.concurrent_running - 1)
        budget.targets_used = max(0, budget.targets_used - target_count)
        locked_session.execution_budget = budget.to_dict()
        locked_session.save(update_fields=["execution_budget"])

    session.execution_budget = budget.to_dict()
    return budget


def reset_execution_budget(session: Any) -> SessionBudget:
    with atomic():
        locked_session = session.__class__._default_manager.select_for_update().get(pk=session.pk)
        budget = SessionBudget.from_session(locked_session)

        budget.executions_used = 0
        budget.targets_used = 0
        budget.concurrent_running = 0
        locked_session.execution_budget = budget.to_dict()
        locked_session.save(update_fields=["execution_budget"])

    session.execution_budget = budget.to_dict()
    return budget
