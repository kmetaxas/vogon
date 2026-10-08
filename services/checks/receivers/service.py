# pyright: reportAttributeAccessIssue=false, reportAssignmentType=false

"""Inbound webhook receiver service for CheckReceivers.

Provides :meth:`ReceiverService.ingest_event`, which runs a normalized event
through the deduplication, correlation, and admission pipeline, persists a
:class:`~apps.checks.models.ReceiverEvent`, and emits a notification whenever
the event is suppressed (duplicate or admission gate rejection).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from typing import Any, cast

from asgiref.sync import async_to_sync
from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction

from apps.checks.enums import ReceiverDisposition
from apps.checks.models import CheckExecution, CheckReceiver, ReceiverEvent
from services.checks.receivers.alertmanager import AlertmanagerAdapter, NormalizedEvent
from services.checks.receivers.correlation import CorrelationEngine
from services.checks.receivers.dedup import DeduplicationEngine
from services.checks.receivers.dispatch import dispatch_to_temporal
from services.notifications.service import NotificationService

logger = logging.getLogger(__name__)


class ReceiverService:
    """Service for ingesting normalized events into ReceiverEvents."""

    @classmethod
    def ingest_event(
        cls,
        receiver: CheckReceiver,
        event: NormalizedEvent,
        raw_payload: dict[str, Any] | None = None,
    ) -> ReceiverEvent:
        """Persist a normalized event and run it through the receiver pipeline.

        The pipeline is: deduplication -> correlation -> admission gate. Events
        that are suppressed (duplicate or admission rejection) are marked
        ``SUPPRESSED`` and a notification is emitted. Correlated events are
        ``ATTACHED`` to an existing execution and are *not* notified.

        Args:
            receiver: The CheckReceiver that received the webhook.
            event: Normalized event from an adapter.
            raw_payload: The original webhook payload, stored verbatim on the
                created ``ReceiverEvent``. When omitted, the serialized
                normalized event is used as a fallback.

        Returns:
            The created ReceiverEvent instance.
        """
        with transaction.atomic():
            receiver = cls._locked_receiver(receiver)
            if not receiver.enabled:
                logger.info("Receiver %s is disabled; skipping event", receiver.id)
                return cls._create_skipped_event(
                    receiver, event, ReceiverDisposition.FAILED.value, raw_payload=raw_payload
                )
            if not receiver.check.enabled:
                logger.info("Check %s is disabled; skipping receiver event", receiver.check_id)
                return cls._create_skipped_event(
                    receiver, event, ReceiverDisposition.FAILED.value, raw_payload=raw_payload
                )
            if receiver.source_type != getattr(event, "source", receiver.source_type):
                logger.warning(
                    "Receiver %s source_type=%s received event source=%s; processing anyway",
                    receiver.id,
                    receiver.source_type,
                    getattr(event, "source", ""),
                )

            receiver_event = cls._create_receiver_event(receiver, event, raw_payload=raw_payload)
            cls._transition(receiver_event, ReceiverDisposition.NORMALIZED.value)

            duplicate, existing = DeduplicationEngine.check_duplicate(receiver, event)
            if duplicate:
                DeduplicationEngine.suppress_duplicate(receiver, receiver_event, existing)
                return receiver_event

            if DeduplicationEngine.is_resolved_update(receiver, event):
                cls._transition(receiver_event, ReceiverDisposition.RESOLVED.value)
                return receiver_event

            if event.alert_status == "resolved":
                cls._transition(receiver_event, ReceiverDisposition.RESOLVED.value)
                return receiver_event

            cls._transition(receiver_event, ReceiverDisposition.DEDUPLICATED.value)

            correlated, execution = CorrelationEngine.find_correlation(receiver, event)
            if correlated:
                receiver_event.check_execution = execution
                receiver_event.correlation_identity = (
                    CorrelationEngine.extract_correlation_identity(event)
                )
                receiver_event.disposition = ReceiverDisposition.ATTACHED.value
                receiver_event.save(
                    update_fields=[
                        "disposition",
                        "check_execution",
                        "correlation_identity",
                        "updated_at",
                    ]
                )
                return receiver_event

            cls._transition(receiver_event, ReceiverDisposition.CORRELATED.value)

            try:
                from services.checks.receivers.dispatch import dispatch_admission_workflow

                async_to_sync(dispatch_admission_workflow)(receiver, receiver_event)
            except Exception:
                logger.exception(
                    "Failed to dispatch admission workflow for receiver event %s",
                    receiver_event.id,
                )
                cls._transition(receiver_event, ReceiverDisposition.FAILED.value)
            return receiver_event

    @classmethod
    def process_payload(
        cls, receiver: CheckReceiver, payload: dict[str, Any]
    ) -> list[ReceiverEvent]:
        events = AlertmanagerAdapter.parse(payload)
        return [cls.ingest_event(receiver, event, raw_payload=payload) for event in events]

    @classmethod
    def notify_suppressed_event(
        cls,
        receiver: CheckReceiver,
        receiver_event: ReceiverEvent,
        reason: str,
    ) -> None:
        """Emit a notification for a suppressed event.

        Notification failures are logged and swallowed so that a webhook
        response is never failed by the notification subsystem.

        Args:
            receiver: The CheckReceiver that received the event.
            receiver_event: The persisted event that was suppressed.
            reason: Human-readable suppression reason (e.g. ``"duplicate"``).
        """
        try:
            summary = cls._event_summary(receiver_event)
            correlation_identity = str(getattr(receiver_event, "correlation_identity", "") or "")
            payload = {
                "reason": reason,
                "receiver": receiver.name,
                "event": summary,
                "fingerprint": receiver_event.external_fingerprint,
                "correlation_identity": correlation_identity,
            }
            body = json.dumps(payload)
            NotificationService.create_notification(
                organization=receiver.organization,
                severity="warning",
                attention="normal",
                title=f"Event suppressed: {summary}",
                summary=(
                    f"Event {receiver_event.external_fingerprint} was suppressed. Reason: {reason}"
                ),
                details=body,
                source_type="check_receiver",
                source_id=str(receiver_event.id),
                context={"notification_type": "event_suppressed", **payload},
            )
            logger.info(
                "Created suppression notification for receiver event %s (reason=%s)",
                receiver_event.id,
                reason,
            )
        except Exception:
            logger.exception(
                "Failed to create suppression notification for receiver event %s",
                receiver_event.id,
            )

    @classmethod
    def _create_receiver_event(
        cls,
        receiver: CheckReceiver,
        event: NormalizedEvent,
        raw_payload: dict[str, Any] | None = None,
    ) -> ReceiverEvent:
        status = (
            ReceiverEvent.Status.FIRING
            if event.alert_status == "firing"
            else ReceiverEvent.Status.RESOLVED
        )
        raw = raw_payload if raw_payload is not None else cls._event_payload(event)
        return ReceiverEvent.objects.create(
            receiver=receiver,
            check=receiver.check,
            external_fingerprint=event.external_fingerprint,
            source_type=receiver.source_type,
            status=status,
            raw_payload=raw,
            normalized_payload=cls._event_payload(event),
            correlation_identity=CorrelationEngine.extract_correlation_identity(event),
            disposition=ReceiverDisposition.RECEIVED.value,
        )

    @classmethod
    def _create_skipped_event(
        cls,
        receiver: CheckReceiver,
        event: NormalizedEvent,
        disposition: str,
        raw_payload: dict[str, Any] | None = None,
    ) -> ReceiverEvent:
        receiver_event = cls._create_receiver_event(receiver, event, raw_payload=raw_payload)
        cls._transition(receiver_event, disposition)
        return receiver_event

    @staticmethod
    def _locked_receiver(receiver: CheckReceiver) -> CheckReceiver:
        return (
            CheckReceiver.objects.select_for_update()
            .select_related("check", "organization", "check__organization")
            .get(pk=receiver.pk)
        )

    @classmethod
    def _start_execution(
        cls,
        receiver: CheckReceiver,
        receiver_event: ReceiverEvent,
    ) -> ReceiverEvent:
        if not CheckExecution.can_execute(receiver.check.organization):
            logger.warning(
                "Concurrency limit reached for organization %s; receiver event %s failed",
                receiver.check.organization_id,
                receiver_event.id,
            )
            cls._transition(receiver_event, ReceiverDisposition.FAILED.value)
            return receiver_event

        receiver_running = CheckExecution.objects.filter(
            check=receiver.check,
            execution_status=CheckExecution.ExecutionStatus.RUNNING,
        ).count()
        if receiver_running >= receiver.max_active_executions:
            logger.warning(
                "Receiver %s max_active_executions (%d) reached; receiver event %s failed",
                receiver.id,
                receiver.max_active_executions,
                receiver_event.id,
            )
            cls._transition(receiver_event, ReceiverDisposition.FAILED.value)
            return receiver_event

        execution = CheckExecution.objects.create(
            check=receiver.check,
            execution_status=CheckExecution.ExecutionStatus.QUEUED,
        )
        receiver_event.check_execution = execution
        receiver_event.save(update_fields=["check_execution", "updated_at"])

        try:
            async_to_sync(dispatch_to_temporal)(receiver, str(execution.id))
        except Exception as exc:
            logger.exception(
                "Failed to dispatch Temporal workflow for receiver event %s execution %s",
                receiver_event.id,
                execution.id,
            )
            execution.execution_status = CheckExecution.ExecutionStatus.FAILED
            execution.error_message = str(exc)
            execution.save(update_fields=["execution_status", "error_message"])
            cls._transition(receiver_event, ReceiverDisposition.FAILED.value)
            return receiver_event

        cls._transition(receiver_event, ReceiverDisposition.STARTED.value)
        return receiver_event

    @staticmethod
    def _transition(
        receiver_event: ReceiverEvent,
        disposition: str,
        *,
        decided_at: Any | None = None,
    ) -> None:
        receiver_event.disposition = disposition
        update_fields = ["disposition", "updated_at"]
        if decided_at is not None:
            receiver_event.decided_at = decided_at
            update_fields.append("decided_at")
        receiver_event.save(update_fields=update_fields)

    @staticmethod
    def _event_payload(event: Any) -> dict[str, Any]:
        if is_dataclass(event) and not isinstance(event, type):
            payload = asdict(cast(Any, event))
        elif isinstance(event, Mapping):
            payload = dict(event)
        else:
            payload = {key: value for key, value in vars(event).items() if not key.startswith("_")}
        result: dict[str, Any] = json.loads(json.dumps(payload, cls=DjangoJSONEncoder))
        return result

    @staticmethod
    def _event_summary(receiver_event: ReceiverEvent) -> str:
        payload: Any = receiver_event.normalized_payload or {}
        if isinstance(payload, Mapping):
            annotations = payload.get("annotations")
            if isinstance(annotations, Mapping):
                summary = annotations.get("summary") or annotations.get("description")
                if summary:
                    return str(summary)
            alert_name = payload.get("alert_name")
            if alert_name:
                return str(alert_name)
        return str(receiver_event.external_fingerprint)
