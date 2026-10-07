from __future__ import annotations

import hashlib
import json
import logging
from datetime import timedelta
from typing import Any

from django.apps import apps
from django.db.transaction import atomic
from django.utils import timezone

Organization: Any = apps.get_model("core", "Organization")
Notification: Any = apps.get_model("notifications", "Notification")
NotificationDelivery: Any = apps.get_model("notifications", "NotificationDelivery")
NotificationPolicy: Any = apps.get_model("notifications", "NotificationPolicy")
NotificationRoute: Any = apps.get_model("notifications", "NotificationRoute")
NotificationSuppression: Any = apps.get_model("notifications", "NotificationSuppression")

logger = logging.getLogger(__name__)


class PolicyResolver:
    @staticmethod
    def resolve(
        organization: Any,
        policy_name: str | None = None,
    ) -> Any | None:
        if policy_name:
            policy = NotificationPolicy.objects.filter(
                organization=organization,
                name=policy_name,
            ).first()
            if policy is not None:
                logger.info("Resolved named policy '%s' for org %s", policy_name, organization.id)
                return policy

        policy = NotificationPolicy.objects.filter(
            organization=organization,
            is_default=True,
        ).first()
        if policy is not None:
            logger.warning("Falling back to default policy for org %s", organization.id)
            return policy

        logger.error("No notification policy found for org %s", organization.id)
        return None


class Router:
    @staticmethod
    def route(notification: Any, policy: Any) -> list[Any]:
        deliveries: list[Any] = []
        routes = NotificationRoute.objects.filter(policy=policy, enabled=True).select_related(
            "channel"
        )

        for route in routes:
            severity_matches = route.severity is None or route.severity == notification.severity
            attention_matches = route.attention is None or route.attention == notification.attention
            if severity_matches and attention_matches:
                deliveries.append(
                    NotificationDelivery.objects.create(
                        notification=notification,
                        channel=route.channel,
                        status=NotificationDelivery.Status.PENDING,
                        attempt_count=0,
                    )
                )

        return deliveries


class NotificationService:
    @staticmethod
    @atomic
    def create_notification(
        *,
        organization: Any,
        severity: str,
        attention: str,
        title: str,
        summary: str,
        source_type: str,
        source_id: str = "",
        details: str = "",
        context: dict[str, Any] | None = None,
        dedup_key: str = "",
        policy_name: str | None = None,
        policy: Any | None = None,
    ) -> Any | None:
        policy = policy or PolicyResolver.resolve(organization, policy_name=policy_name)
        if policy is None:
            return None

        dedup_key = dedup_key or NotificationService.build_dedup_key(
            source_type=source_type,
            source_id=source_id,
            severity=severity,
            attention=attention,
            context=context or {},
        )

        notification = Notification.objects.create(
            organization=organization,
            policy=policy,
            severity=severity,
            attention=attention,
            title=title,
            summary=summary,
            details=details,
            source_type=source_type,
            source_id=source_id,
            context=context or {},
            dedup_key=dedup_key,
            status=Notification.Status.OPEN,
        )

        suppressing_notification = NotificationService.find_suppressing_notification(
            notification=notification,
            policy=policy,
        )
        if suppressing_notification is not None:
            notification.status = Notification.Status.SUPPRESSED
            notification.save(update_fields=["status", "updated_at"])
            NotificationSuppression.objects.create(
                organization=organization,
                dedup_key=dedup_key,
                notification=suppressing_notification,
                suppressed_notification=notification,
            )
            return notification

        deliveries = Router.route(notification, policy)
        if deliveries:
            try:
                NotificationService._start_delivery_workflow(notification)
            except Exception as exc:
                logger.error(
                    "Failed to start delivery workflow for notification %s: %s",
                    notification.id,
                    exc,
                    exc_info=True,
                )

        return notification

    @staticmethod
    def _start_delivery_workflow(notification: Any) -> None:
        """Start Temporal workflow for notification delivery (fire-and-forget)."""
        try:
            import asyncio

            from services.temporal_workers.client import get_temporal_client
            from services.temporal_workers.workflows import NotificationDeliveryWorkflow

            async def _start() -> None:
                client = await get_temporal_client()
                await client.start_workflow(
                    NotificationDeliveryWorkflow.run,
                    args=[str(notification.id)],
                    id=f"notification-{notification.id}",
                    task_queue="vogon",
                )

            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None

            if loop is not None and loop.is_running():
                loop.create_task(_start())
            else:
                # Synchronous context — run in a new task on a new loop
                asyncio.run(_start())
        except Exception as exc:
            logger.error(
                "Failed to start delivery workflow for notification %s: %s",
                notification.id,
                exc,
                exc_info=True,
            )

    @staticmethod
    def build_dedup_key(
        *,
        source_type: str,
        source_id: str,
        severity: str,
        attention: str,
        context: dict[str, Any],
    ) -> str:
        payload = json.dumps(
            {
                "source_type": source_type,
                "source_id": source_id,
                "severity": severity,
                "attention": attention,
                "context": context,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    @staticmethod
    def find_suppressing_notification(
        *,
        notification: Any,
        policy: Any | None,
    ) -> Any | None:
        if policy is None:
            return None

        cutoff = timezone.now() - timedelta(seconds=policy.dedup_window_seconds)
        return (
            Notification.objects.filter(
                organization=notification.organization,
                dedup_key=notification.dedup_key,
                status=Notification.Status.OPEN,
                created_at__gte=cutoff,
            )
            .exclude(id=notification.id)
            .order_by("-created_at")
            .first()
        )


__all__ = ["NotificationService", "PolicyResolver", "Router"]
