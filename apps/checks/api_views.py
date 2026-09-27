import asyncio

from rest_framework import permissions, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.checks.models import (
    Check,
    CheckActionLog,
    CheckExecution,
    CheckHealthState,
    CheckVersion,
)
from apps.checks.serializers import (
    CheckActionLogSerializer,
    CheckExecutionSerializer,
    CheckHealthStateSerializer,
    CheckSerializer,
    CheckVersionSerializer,
)
from services.checks.scheduler import CheckScheduler


class OrganizationFilterMixin:
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
                pass  # Temporal may be offline; never fail the request

    def perform_update(self, serializer):
        check = serializer.save()
        try:
            asyncio.run(CheckScheduler.update_schedule(check))
        except Exception:
            pass

    def perform_destroy(self, instance):
        try:
            asyncio.run(CheckScheduler.delete_schedule(str(instance.id)))
        except Exception:
            pass
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
                "execution_mode": check.execution_mode,
                "evaluation_config": check.evaluation_config,
                "notification_config": check.notification_config,
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
        execution_mode = check_data.get("execution_mode", Check.ExecutionMode.DETERMINISTIC)

        check = Check.objects.create(
            organization=organization,
            name=name,
            description=check_data.get("description", ""),
            schedule_type=schedule_type,
            schedule_expression=schedule_expression,
            timezone=check_data.get("timezone", "UTC"),
            execution_mode=execution_mode,
            evaluation_config=check_data.get("evaluation_config", {}),
            notification_config=check_data.get("notification_config", {}),
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


class CheckHealthStateViewSet(OrganizationFilterMixin, viewsets.ModelViewSet):
    queryset = CheckHealthState.objects.all()
    serializer_class = CheckHealthStateSerializer
    permission_classes = [permissions.IsAuthenticated]


class CheckActionLogViewSet(OrganizationFilterMixin, viewsets.ModelViewSet):
    queryset = CheckActionLog.objects.all()
    serializer_class = CheckActionLogSerializer
    permission_classes = [permissions.IsAuthenticated]
