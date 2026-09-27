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
