from rest_framework import permissions, viewsets

from apps.core.mixins import OrganizationQuerySetMixin
from apps.notifications.models import (
    Notification,
    NotificationChannel,
    NotificationPolicy,
    NotificationRoute,
)
from apps.notifications.serializers import (
    NotificationChannelSerializer,
    NotificationPolicySerializer,
    NotificationRouteSerializer,
    NotificationSerializer,
)
from services.notifications.service import NotificationService


class NotificationViewSet(OrganizationQuerySetMixin, viewsets.ModelViewSet):
    serializer_class = NotificationSerializer
    permission_classes = [permissions.IsAuthenticated]
    queryset = Notification.objects.all().prefetch_related("deliveries")

    def perform_create(self, serializer):
        NotificationService.create_notification(
            organization=self.request.user.get_current_organization(),
            severity=serializer.validated_data["severity"],
            attention=serializer.validated_data["attention"],
            title=serializer.validated_data["title"],
            summary=serializer.validated_data["summary"],
            details=serializer.validated_data.get("details", ""),
            source_type=serializer.validated_data["source_type"],
            source_id=serializer.validated_data.get("source_id", ""),
            context=serializer.validated_data.get("context", {}),
        )


class NotificationChannelViewSet(OrganizationQuerySetMixin, viewsets.ModelViewSet):
    serializer_class = NotificationChannelSerializer
    permission_classes = [permissions.IsAuthenticated]
    queryset = NotificationChannel.objects.all()

    def perform_create(self, serializer):
        serializer.save(organization=self.request.user.get_current_organization())


class NotificationPolicyViewSet(OrganizationQuerySetMixin, viewsets.ModelViewSet):
    serializer_class = NotificationPolicySerializer
    permission_classes = [permissions.IsAuthenticated]
    queryset = NotificationPolicy.objects.all()

    def perform_create(self, serializer):
        serializer.save(organization=self.request.user.get_current_organization())


class NotificationRouteViewSet(viewsets.ModelViewSet):
    serializer_class = NotificationRouteSerializer
    permission_classes = [permissions.IsAuthenticated]
    queryset = NotificationRoute.objects.all().select_related("policy", "channel")

    def get_queryset(self):
        qs = self.queryset
        user = self.request.user
        if not user.is_authenticated:
            return qs.none()
        if user.is_superuser:
            return qs
        return qs.filter(policy__organization__in=user.organizations.all())
