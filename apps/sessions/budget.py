"""SessionBudget Pydantic model for per-session execution budget tracking."""

from __future__ import annotations

from typing import Any, Dict

from django.db.models.expressions import F  # noqa: F401
from django.db.transaction import atomic
from pydantic import BaseModel, Field

DEFAULT_EXECUTION_BUDGET: Dict[str, Any] = {
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

    @classmethod
    def from_session(cls, session: Any) -> "SessionBudget":
        """Build a :class:`SessionBudget` from a ``TSession`` instance.

        Reads ``session.execution_budget`` (a dict or None). If it is empty or
        missing, defaults are used.
        """
        budget: Dict[str, Any] = session.execution_budget or {}
        return cls(**budget)

    def to_dict(self) -> Dict[str, int]:
        """Return a plain dict suitable for saving to the JSONField."""
        return self.model_dump()


class BudgetExceeded(Exception):
    pass


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
