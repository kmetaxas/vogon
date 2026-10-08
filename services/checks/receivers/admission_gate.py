from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Mapping
from typing import Any, cast

from asgiref.sync import async_to_sync, sync_to_async  # type: ignore[attr-defined]
from django.conf import settings

from apps.checks.enums import AdmissionMode
from apps.checks.models import CheckReceiver, ReceiverAdmissionDecision, ReceiverEvent
from apps.llm.models import LLMProvider
from services.temporal_workers.activities import call_llm

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are an event admission gate for an AI-powered troubleshooting system.\n"
    "Your job is to judge whether an incoming alert warrants starting an investigation.\n"
    "\n"
    "Rules:\n"
    "- START if the alert is novel, actionable, and not already being investigated\n"
    "- SUPPRESS if it's a duplicate, low-value noise, or already covered by an active "
    "investigation\n"
    "- Consider the check's purpose, recent alert history, and active investigations\n"
    "- Provide your reasoning before giving the final verdict\n"
    "\n"
    "Output a JSON verdict with:\n"
    '  {"decision": "START" | "SUPPRESS_LOW_VALUE", "reason": "explanation", '
    '"confidence": 0.0-1.0}'
)

DEFAULT_GATING_PROMPT = """Check Context:
- Name: {check_name}
- Description: {check_description}
- Instructions: {check_instructions}

Incoming Event:
- Alert: {alert_name}
- Status: {status}
- Severity: {severity}
- Instance: {instance}
- Namespace: {namespace}
- Service: {service}
- Cluster: {cluster}
- Summary: {summary}

Recent Events (last 10):
{recent_events}

Active Investigations:
{active_investigations}

Judgment: Should we START an investigation for this event?"""

TIMEOUT_REASON_FAIL_OPEN = "Admission gate timed out / failed — fail-open"
TIMEOUT_REASON_FAIL_CLOSED = "Admission gate timed out / failed — fail-closed"


class GateContextBuilder:
    def __init__(self, receiver: CheckReceiver, event: Any, receiver_event: ReceiverEvent):
        self.receiver = receiver
        self.event = event
        self.receiver_event = receiver_event

    def build(self) -> dict[str, str]:
        return {
            "check_name": self._check_name(),
            "check_description": self._check_description(),
            "check_instructions": self._check_instructions(),
            "alert_name": self._alert_name(),
            "status": self._status(),
            "severity": self._severity(),
            "instance": self._instance(),
            "namespace": self._namespace(),
            "service": self._service(),
            "cluster": self._cluster(),
            "summary": self._summary(),
            "recent_events": self._recent_events(),
            "active_investigations": self._active_investigations(),
        }

    def _check_name(self) -> str:
        return str(getattr(self.receiver.check, "name", "Unknown"))

    def _check_description(self) -> str:
        return str(getattr(self.receiver.check, "description", "") or "")

    def _check_instructions(self) -> str:
        return str(getattr(self.receiver.check, "instructions", "") or "")

    def _alert_name(self) -> str:
        return str(
            getattr(self.event, "alert_name", "") or self._event_payload_value("alert_name", "")
        )

    def _status(self) -> str:
        return str(
            getattr(self.event, "alert_status", "") or self._event_payload_value("status", "")
        )

    def _severity(self) -> str:
        return str(self._event_labels().get("severity", "unknown"))

    def _instance(self) -> str:
        return str(self._event_labels().get("instance", "unknown"))

    def _namespace(self) -> str:
        return str(self._event_labels().get("namespace", "unknown"))

    def _service(self) -> str:
        return str(self._event_labels().get("service", "unknown"))

    def _cluster(self) -> str:
        return str(self._event_labels().get("cluster", "unknown"))

    def _summary(self) -> str:
        annotations = getattr(self.event, "annotations", None)
        if isinstance(annotations, Mapping):
            return str(annotations.get("summary") or annotations.get("description") or "")
        payload = getattr(self.receiver_event, "normalized_payload", {})
        if isinstance(payload, Mapping):
            payload_annotations = payload.get("annotations", {})
            if isinstance(payload_annotations, Mapping):
                return str(
                    payload_annotations.get("summary")
                    or payload_annotations.get("description")
                    or ""
                )
        return ""

    def _event_labels(self) -> dict[str, Any]:
        labels = getattr(self.event, "labels", None)
        if isinstance(labels, Mapping):
            return dict(labels)
        payload = getattr(self.receiver_event, "normalized_payload", {})
        if isinstance(payload, Mapping):
            payload_labels = payload.get("labels")
            if isinstance(payload_labels, Mapping):
                return dict(payload_labels)
        return {}

    def _event_payload_value(self, key: str, default: str) -> Any:
        payload = getattr(self.receiver_event, "normalized_payload", {})
        if isinstance(payload, Mapping):
            return payload.get(key, default)
        return default

    def _recent_events(self) -> str:
        events = (
            ReceiverEvent.objects.filter(receiver=self.receiver)
            .exclude(id=self.receiver_event.id)
            .order_by("-created_at")[:10]
        )
        lines = [
            f"- {event.external_fingerprint} ({event.disposition}) at {event.created_at}"
            for event in events
        ]
        return "\n".join(lines) if lines else "None"

    def _active_investigations(self) -> str:
        from apps.checks.models import CheckExecution

        executions = CheckExecution.objects.filter(
            check=self.receiver.check,
            execution_status__in=["running", "queued"],
        ).order_by("-triggered_at")[:5]
        lines = [
            f"- {execution.id} ({execution.execution_status}) triggered at {execution.triggered_at}"
            for execution in executions
        ]
        return "\n".join(lines) if lines else "None"


class AdmissionGateService:
    TIMEOUT_SECONDS = getattr(settings, "ADMISSION_LLM_TIMEOUT_SECONDS", 3)
    DECISION_MAP = {
        "START": "start",
        "SUPPRESS_LOW_VALUE": "suppress_low_value",
    }

    @classmethod
    def evaluate(
        cls,
        receiver: CheckReceiver,
        normalized_event: Any,
        event_context: Any | None = None,
    ) -> ReceiverAdmissionDecision:
        receiver_event = cls._receiver_event(normalized_event, event_context)
        return async_to_sync(cls.evaluate_async)(receiver, receiver_event)

    @classmethod
    async def evaluate_async(
        cls,
        receiver: CheckReceiver,
        receiver_event: ReceiverEvent,
    ) -> ReceiverAdmissionDecision:
        model_name = await sync_to_async(cls._model_name)(receiver)

        if receiver.admission_mode == AdmissionMode.ALWAYS:
            return await sync_to_async(cls._persist_decision)(
                receiver=receiver,
                receiver_event=receiver_event,
                decision="start",
                reason="Admission mode is ALWAYS",
                model="n/a",
                confidence=None,
                timed_out=False,
                reasoning=None,
            )

        try:
            context = await sync_to_async(
                lambda: GateContextBuilder(
                    receiver,
                    receiver_event.normalized_payload,
                    receiver_event,
                ).build()
            )()
            prompt = cls._build_prompt(receiver, context)
            system_msg = (
                str(receiver.gating_prompt).strip() if receiver.gating_prompt else SYSTEM_PROMPT
            )
            receiver_provider_id = getattr(receiver, "admission_llm_provider_id", None)
            provider_id = str(receiver_provider_id) if receiver_provider_id else None

            response = await asyncio.wait_for(
                call_llm(
                    thread_id=None,
                    messages=[
                        {"role": "system", "content": system_msg},
                        {"role": "user", "content": prompt},
                    ],
                    llm_provider_id=provider_id,
                    organization_id=str(getattr(receiver, "organization_id")),
                ),
                timeout=cls.TIMEOUT_SECONDS,
            )

            content = response.get("content", "") if isinstance(response, Mapping) else ""
            reasoning = response.get("reasoning", "") if isinstance(response, Mapping) else ""
            model_name = (
                str(response.get("model") or model_name)
                if isinstance(response, Mapping)
                else model_name
            )
            parsed = cls._parse_response(content)
        except Exception:
            logger.exception("Admission gate failed for receiver %s", receiver.id)
            return await sync_to_async(cls._timeout_decision)(receiver_event, receiver, model_name)

        return await sync_to_async(cls._persist_decision)(
            receiver=receiver,
            receiver_event=receiver_event,
            decision=parsed["decision"],
            reason=parsed["reason"],
            model=model_name,
            confidence=parsed["confidence"],
            timed_out=False,
            reasoning=reasoning,
        )

    @classmethod
    def _build_prompt(cls, receiver: CheckReceiver, context: dict[str, str]) -> str:
        return DEFAULT_GATING_PROMPT.format(**context)

    @classmethod
    def _parse_response(cls, content: str) -> dict[str, Any]:
        match = re.search(r'\{.*"decision".*\}', content, re.DOTALL)
        payload = json.loads(match.group(0) if match else content)
        if not isinstance(payload, dict):
            raise ValueError("Admission gate response must be a JSON object")

        raw_decision = payload.get("decision")
        if raw_decision not in cls.DECISION_MAP:
            raise ValueError(f"Unknown admission decision: {raw_decision}")

        reason = payload.get("reason", "")
        confidence = payload.get("confidence", 0.5)
        if not isinstance(reason, str):
            reason = str(reason)
        if not isinstance(confidence, int | float) or isinstance(confidence, bool):
            confidence = 0.5
        confidence = max(0.0, min(1.0, float(confidence)))

        return {
            "decision": cls.DECISION_MAP[raw_decision],
            "reason": reason,
            "confidence": confidence,
        }

    @classmethod
    def _persist_decision(
        cls,
        *,
        receiver: CheckReceiver,
        receiver_event: ReceiverEvent,
        decision: str,
        reason: str,
        model: str,
        confidence: float | None,
        timed_out: bool,
        reasoning: str | None = None,
    ) -> ReceiverAdmissionDecision:
        persisted = ReceiverAdmissionDecision.objects.create(
            receiver_event=receiver_event,
            decision=decision,
            reason=reason,
            model=model,
            confidence=confidence,
            timed_out=timed_out,
            candidate_investigations={},
            reasoning=reasoning or "",
        )
        if str(decision).startswith("suppress"):
            cls._notify_suppression(receiver, receiver_event, reason)
        return persisted

    @staticmethod
    def _notify_suppression(
        receiver: CheckReceiver,
        receiver_event: ReceiverEvent,
        reason: str,
    ) -> None:
        from services.checks.receivers.service import ReceiverService

        ReceiverService.notify_suppressed_event(receiver, receiver_event, reason=reason)

    @classmethod
    def _timeout_decision(
        cls,
        receiver_event: ReceiverEvent,
        receiver: CheckReceiver,
        model_name: str,
    ) -> ReceiverAdmissionDecision:
        if receiver.fail_open_on_timeout:
            decision = "start"
            reason = TIMEOUT_REASON_FAIL_OPEN
        else:
            decision = "suppress_low_value"
            reason = TIMEOUT_REASON_FAIL_CLOSED
        return cls._persist_decision(
            receiver=receiver,
            receiver_event=receiver_event,
            decision=decision,
            reason=reason,
            model=model_name,
            confidence=None,
            timed_out=True,
            reasoning=None,
        )

    @staticmethod
    def _receiver_event(normalized_event: Any, event_context: Any | None) -> ReceiverEvent:
        if isinstance(event_context, ReceiverEvent):
            return event_context
        if isinstance(event_context, Mapping):
            receiver_event = event_context.get("receiver_event") or event_context.get("event")
            if isinstance(receiver_event, ReceiverEvent):
                return receiver_event
        if isinstance(normalized_event, ReceiverEvent):
            return normalized_event
        raise ValueError("AdmissionGateService requires a ReceiverEvent in event_context")

    @staticmethod
    def _model_name(receiver: CheckReceiver) -> str:
        provider = cast(LLMProvider | None, getattr(receiver, "admission_llm_provider", None))
        if provider is None:
            provider = (
                LLMProvider.objects.filter(organization=receiver.organization, enabled=True)  # type: ignore[attr-defined]
                .order_by("-is_default")
                .first()
            )
        return cast(str, provider.model) if provider else ""
