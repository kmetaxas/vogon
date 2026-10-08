from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import timedelta
from typing import Any, cast

import django.db.transaction
from django.core.serializers.json import DjangoJSONEncoder
from django.utils import timezone

from apps.checks.enums import ReceiverDisposition
from apps.checks.models import CheckReceiver, ReceiverEvent


class DeduplicationEngine:
    ACTIVE_DISPOSITIONS = (
        ReceiverDisposition.STARTED,
        ReceiverDisposition.ATTACHED,
        ReceiverDisposition.ADMISSION_DECIDED,
    )

    @classmethod
    def check_duplicate(
        cls,
        receiver: CheckReceiver,
        normalized_event: Any,
    ) -> tuple[bool, ReceiverEvent | None]:
        if cls._event_status(normalized_event) == ReceiverEvent.Status.RESOLVED:
            return False, None

        cutoff = timezone.now() - timedelta(seconds=receiver.dedup_window_seconds)
        external_fingerprint = cls._external_fingerprint(normalized_event)

        existing = (
            ReceiverEvent.objects.all()
            .select_for_update()
            .filter(
                receiver_id=receiver.id,
                external_fingerprint=external_fingerprint,
                disposition__in=cls.ACTIVE_DISPOSITIONS,
                created_at__gte=cutoff,
            )
            .order_by("-created_at")
            .first()
        )
        if existing is None:
            return False, None
        return True, existing

    @classmethod
    def suppress_duplicate(
        cls,
        receiver: CheckReceiver,
        receiver_event: ReceiverEvent,
        existing: ReceiverEvent | None,
    ) -> ReceiverEvent:
        """Mark *receiver_event* as a suppressed duplicate and notify.

        The event is linked to the *existing* active event via ``related_event``
        and a suppression notification is emitted. Notification failures are
        swallowed by :meth:`ReceiverService.notify_suppressed_event`.
        """
        receiver_event.disposition = ReceiverDisposition.SUPPRESSED
        receiver_event.related_event = existing
        receiver_event.save(update_fields=["disposition", "related_event", "updated_at"])

        from services.checks.receivers.service import ReceiverService

        ReceiverService.notify_suppressed_event(receiver, receiver_event, reason="duplicate")
        return receiver_event

    @classmethod
    def is_resolved_update(cls, receiver: CheckReceiver, normalized_event: Any) -> bool:
        if cls._event_status(normalized_event) != ReceiverEvent.Status.RESOLVED:
            return False

        external_fingerprint = cls._external_fingerprint(normalized_event)
        with django.db.transaction.atomic():
            firing_event = (
                ReceiverEvent.objects.all()
                .select_for_update()
                .filter(
                    receiver_id=receiver.id,
                    external_fingerprint=external_fingerprint,
                    status=ReceiverEvent.Status.FIRING,
                )
                .exclude(disposition=ReceiverDisposition.RESOLVED)
                .order_by("-created_at")
                .first()
            )
            if firing_event is not None:
                firing_event.disposition = ReceiverDisposition.RESOLVED
                firing_event.save(update_fields=["disposition", "updated_at"])
                return True

            return False

    @staticmethod
    def _external_fingerprint(normalized_event: Any) -> str:
        return str(getattr(normalized_event, "external_fingerprint", ""))

    @staticmethod
    def _event_status(normalized_event: Any) -> str:
        return str(getattr(normalized_event, "alert_status", "")).lower()

    @staticmethod
    def _event_payload(normalized_event: Any) -> dict[str, Any]:
        if is_dataclass(normalized_event):
            payload = asdict(normalized_event)  # type: ignore[arg-type]
        elif isinstance(normalized_event, dict):
            payload = dict(normalized_event)
        else:
            payload = {
                key: value
                for key, value in vars(normalized_event).items()
                if not key.startswith("_")
            }
        return cast(dict[str, Any], json.loads(json.dumps(payload, cls=DjangoJSONEncoder)))
