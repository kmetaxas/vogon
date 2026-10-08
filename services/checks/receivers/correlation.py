from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.utils import timezone

from apps.checks.models import CheckExecution, CheckReceiver

CORRELATION_KEYS = ("cluster", "namespace", "service", "environment", "job", "instance")


class CorrelationEngine:
    @classmethod
    def find_correlation(
        cls,
        receiver: CheckReceiver,
        normalized_event: Any,
    ) -> tuple[bool, CheckExecution | None]:
        labels = cls._event_labels(normalized_event)
        event_identity = cls.extract_correlation_identity(normalized_event)
        if not event_identity:
            return False, None

        cutoff = timezone.now() - timedelta(
            seconds=settings.CHECK_RECEIVER_CORRELATION_WINDOW_SECONDS
        )
        candidates = CheckExecution.objects.filter(
            check=receiver.check,
            execution_status=CheckExecution.ExecutionStatus.RUNNING,
            triggered_at__gte=cutoff,
        ).order_by("-triggered_at")

        for execution in candidates:
            if cls._labels_overlap(execution.resolved_targets, labels):
                return True, execution
        return False, None

    @classmethod
    def extract_correlation_identity(cls, event: Any) -> str:
        labels = cls._event_labels(event)
        pairs = [
            f"{key}={labels[key]}"
            for key in sorted(CORRELATION_KEYS)
            if labels.get(key) not in (None, "")
        ]
        return ",".join(pairs)

    @classmethod
    def _labels_overlap(cls, resolved_targets: Any, event_labels: Mapping[str, Any]) -> bool:
        target_labels = cls._collect_label_maps(resolved_targets)
        event_key_values = {
            key: str(value)
            for key, value in event_labels.items()
            if key in CORRELATION_KEYS and value not in (None, "")
        }
        if not target_labels or not event_key_values:
            return False

        for labels in target_labels:
            matched = [
                key
                for key, event_value in event_key_values.items()
                if key in labels and str(labels[key]) == event_value
            ]
            if matched:
                return True
        return False

    @classmethod
    def _collect_label_maps(cls, value: Any) -> list[Mapping[str, Any]]:
        if isinstance(value, Mapping):
            found: list[Mapping[str, Any]] = []
            labels = value.get("labels")
            if isinstance(labels, Mapping):
                found.append(labels)
            for child in value.values():
                found.extend(cls._collect_label_maps(child))
            return found
        if isinstance(value, (list, tuple)):
            found = []
            for child in value:
                found.extend(cls._collect_label_maps(child))
            return found
        return []

    @staticmethod
    def _event_labels(event: Any) -> Mapping[str, Any]:
        labels = getattr(event, "labels", {})
        return labels if isinstance(labels, Mapping) else {}
