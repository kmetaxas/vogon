"""Check execution budget reservation/release helpers.

Mirrors :mod:`apps.sessions.budget` but operates on a ``Check`` instance and its
``execution_budget`` JSONField. Reuses :class:`~apps.sessions.budget.SessionBudget`
as the budget value object so both apps share the same schema.
"""

from __future__ import annotations

from typing import Any

from django.db.transaction import atomic

from apps.sessions.budget import BudgetExceeded, SessionBudget

__all__ = [
    "BudgetExceeded",
    "SessionBudget",
    "check_and_reserve_budget",
    "release_execution_budget",
]


def check_and_reserve_budget(check: Any, target_count: int) -> SessionBudget:
    """Reserve one execution and ``target_count`` targets against a Check.

    Locks the Check row, validates all limits, then increments the usage
    counters. Raises :class:`BudgetExceeded` if any limit would be exceeded.
    """
    with atomic():
        locked_check = check.__class__._default_manager.select_for_update().get(pk=check.pk)
        budget = SessionBudget.from_session(locked_check)

        if budget.executions_used >= budget.max_executions_per_session:
            raise BudgetExceeded("Check execution budget is exhausted.")
        if budget.targets_used + target_count > budget.max_targets_per_session:
            raise BudgetExceeded("Check target budget would be exceeded.")
        if budget.concurrent_running >= budget.max_concurrent_executions:
            raise BudgetExceeded("Check concurrent execution budget is exhausted.")

        budget.executions_used += 1
        budget.targets_used += target_count
        budget.concurrent_running += 1
        locked_check.execution_budget = budget.to_dict()
        locked_check.save(update_fields=["execution_budget"])

    check.execution_budget = budget.to_dict()
    return budget


def release_execution_budget(check: Any, target_count: int) -> SessionBudget:
    """Release one concurrent execution and ``target_count`` targets.

    Locks the Check row, decrements the usage counters (floored at zero), and
    persists the updated budget.
    """
    with atomic():
        locked_check = check.__class__._default_manager.select_for_update().get(pk=check.pk)
        budget = SessionBudget.from_session(locked_check)

        budget.concurrent_running = max(0, budget.concurrent_running - 1)
        budget.targets_used = max(0, budget.targets_used - target_count)
        locked_check.execution_budget = budget.to_dict()
        locked_check.save(update_fields=["execution_budget"])

    check.execution_budget = budget.to_dict()
    return budget
