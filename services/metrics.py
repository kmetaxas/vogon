"""Lightweight metrics for discovery and embedding operations.

This module provides counter and timer metrics that can be integrated with
Prometheus, StatsD, or structured logging. No external dependencies required.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from contextlib import contextmanager
from typing import Any

logger = logging.getLogger("vogon.metrics")


class _MetricsRegistry:
    """Thread-safe in-memory metrics registry."""

    def __init__(self):
        self._counters: dict[str, int] = defaultdict(int)
        self._timers: dict[str, list[float]] = defaultdict(list)

    def incr(self, name: str, value: int = 1) -> None:
        self._counters[name] += value

    def record(self, name: str, value: float) -> None:
        self._timers[name].append(value)

    def get_counter(self, name: str) -> int:
        return self._counters.get(name, 0)

    def get_timer_stats(self, name: str) -> dict[str, float] | None:
        values = self._timers.get(name)
        if not values:
            return None
        return {
            "count": len(values),
            "sum": sum(values),
            "avg": sum(values) / len(values),
            "min": min(values),
            "max": max(values),
        }

    def flush(self) -> dict[str, Any]:
        """Return current metrics and reset counters."""
        snapshot = {
            "counters": dict(self._counters),
            "timers": {name: self.get_timer_stats(name) for name in self._timers},
        }
        self._counters.clear()
        self._timers.clear()
        return snapshot


_registry = _MetricsRegistry()


def incr(name: str, value: int = 1) -> None:
    """Increment a counter metric."""
    _registry.incr(name, value)


@contextmanager
def timer(name: str):
    """Context manager to time an operation and record its duration in seconds."""
    start = time.perf_counter()
    try:
        yield
    finally:
        duration = time.perf_counter() - start
        _registry.record(name, duration)


def record(name: str, value: float) -> None:
    """Record a raw value for a timer metric."""
    _registry.record(name, value)


def get_counter(name: str) -> int:
    """Get current counter value."""
    return _registry.get_counter(name)


def get_timer_stats(name: str) -> dict[str, float] | None:
    """Get timer statistics."""
    return _registry.get_timer_stats(name)


def flush() -> dict[str, Any]:
    """Flush and return all metrics, resetting counters."""
    return _registry.flush()


def log_metrics() -> None:
    """Log current metrics at INFO level. Call periodically."""
    snapshot = flush()
    if snapshot["counters"] or any(v is not None for v in snapshot["timers"].values()):
        logger.info("metrics_snapshot", extra=snapshot)
