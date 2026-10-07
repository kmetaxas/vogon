import asyncio
import logging
from typing import Any

from rest_framework import permissions, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.checks.models import (
    Check,
    CheckActionLog,
    CheckExecution,
    CheckVersion,
)
from apps.checks.serializers import (
    CheckActionLogSerializer,
    CheckExecutionSerializer,
    CheckSerializer,
    CheckVersionSerializer,
)
from services.checks.scheduler import CheckScheduler

logger = logging.getLogger(__name__)


class OrganizationFilterMixin:
    queryset: Any
    request: Any

    def get_queryset(self):
        qs = self.queryset
        user = self.request.user
        if not user.is_authenticated:
            return qs.none()
        if user.is_superuser:
            return qs
        return qs.filter(check__organization__in=user.organizations.all())


class CheckViewSet(viewsets.ModelViewSet):
    serializer_class = CheckSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        if not user.is_authenticated:
            return Check.objects.none()
        if user.is_superuser:
            return Check.objects.all()
        return Check.objects.filter(organization__in=user.organizations.all())

    def perform_create(self, serializer):
        check = serializer.save(organization=self.request.user.get_current_organization())
        if check.enabled:
            try:
                asyncio.run(CheckScheduler.create_schedule(check))
            except Exception:
                logger.warning(
                    "Temporal schedule creation failed for check %s", check.id, exc_info=True
                )
                # Temporal may be offline; never fail the request

    def perform_update(self, serializer):
        check = serializer.save()
        try:
            asyncio.run(CheckScheduler.update_schedule(check))
        except Exception:
            logger.warning("Temporal schedule update failed for check %s", check.id, exc_info=True)

    def perform_destroy(self, instance):
        try:
            asyncio.run(CheckScheduler.delete_schedule(str(instance.id)))
        except Exception:
            logger.warning(
                "Temporal schedule deletion failed for check %s", instance.id, exc_info=True
            )
        instance.delete()

    @action(methods=["post"], detail=True)
    def dry_run(self, request, pk=None):
        check = self.get_object()
        try:
            result = asyncio.run(CheckScheduler.trigger_now(check, dry_run=True))
            return Response(result)
        except Exception as exc:
            return Response({"status": "error", "error": str(exc)}, status=500)

    @action(methods=["post"], detail=True)
    def enable(self, request, pk=None):
        check = self.get_object()
        check.enabled = True
        check.save()
        try:
            result = asyncio.run(CheckScheduler.resume_schedule(str(check.id)))
            return Response(result)
        except Exception as exc:
            return Response({"status": "error", "error": str(exc)}, status=500)

    @action(methods=["post"], detail=True)
    def disable(self, request, pk=None):
        check = self.get_object()
        check.enabled = False
        check.save()
        try:
            result = asyncio.run(CheckScheduler.pause_schedule(str(check.id)))
            return Response(result)
        except Exception as exc:
            return Response({"status": "error", "error": str(exc)}, status=500)

    @action(methods=["post"], detail=True)
    def trigger(self, request, pk=None):
        check = self.get_object()
        try:
            result = asyncio.run(CheckScheduler.trigger_now(check))
            return Response(result)
        except Exception as exc:
            return Response({"status": "error", "error": str(exc)}, status=500)

    @action(methods=["get"], detail=True)
    def export(self, request, pk=None):
        """Export a Check definition as JSON."""
        check = self.get_object()
        data = {
            "version": "1.0",
            "check": {
                "name": check.name,
                "description": check.description,
                "schedule_type": check.schedule_type,
                "schedule_expression": check.schedule_expression,
                "timezone": check.timezone,
                "notification_config": check.notification_config,
                "notification_policy_name": check.notification_policy_name,
                "execution_budget": check.execution_budget,
            },
        }
        return Response(data)

    @action(methods=["post"], detail=False)
    def import_check(self, request):
        """Import a Check definition from JSON."""
        data = request.data
        check_data = data.get("check", {})

        name = check_data.get("name")
        if not name:
            return Response({"error": "name is required"}, status=400)

        organization = self.request.user.get_current_organization()

        if Check.objects.filter(organization=organization, name=name).exists():
            return Response({"error": f"Check with name '{name}' already exists"}, status=400)

        schedule_type = check_data.get("schedule_type", Check.ScheduleType.INTERVAL)
        schedule_expression = check_data.get("schedule_expression", "60")

        check = Check.objects.create(
            organization=organization,
            name=name,
            description=check_data.get("description", ""),
            instructions=check_data.get("instructions", ""),
            schedule_type=schedule_type,
            schedule_expression=schedule_expression,
            timezone=check_data.get("timezone", "UTC"),
            notification_config=check_data.get("notification_config", {}),
            notification_policy_name=check_data.get("notification_policy_name", ""),
            execution_budget=check_data.get("execution_budget", {}),
            created_by=self.request.user,
        )

        CheckVersion.objects.create(
            check=check,
            version_number=1,
            definition_snapshot=check_data,
        )

        return Response(
            {
                "status": "created",
                "check_id": str(check.id),
            },
            status=201,
        )


class CheckVersionViewSet(OrganizationFilterMixin, viewsets.ModelViewSet):
    queryset = CheckVersion.objects.all()
    serializer_class = CheckVersionSerializer
    permission_classes = [permissions.IsAuthenticated]


class CheckExecutionViewSet(OrganizationFilterMixin, viewsets.ModelViewSet):
    queryset = CheckExecution.objects.all()
    serializer_class = CheckExecutionSerializer
    permission_classes = [permissions.IsAuthenticated]


class CheckActionLogViewSet(OrganizationFilterMixin, viewsets.ModelViewSet):
    queryset = CheckActionLog.objects.all()
    serializer_class = CheckActionLogSerializer
    permission_classes = [permissions.IsAuthenticated]
