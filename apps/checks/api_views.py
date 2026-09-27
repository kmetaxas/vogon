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
        serializer.save(organization=self.request.user.get_current_organization())

    @action(methods=["post"], detail=True)
    def dry_run(self, request, pk=None):
        check = self.get_object()
        # TODO: trigger CheckWorkflow in dry-run mode
        return Response({"status": "dry_run_triggered", "check_id": str(check.id)})


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
