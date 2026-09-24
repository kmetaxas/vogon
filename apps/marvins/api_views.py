from rest_framework import permissions, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.marvins.models import Capability, Marvin, MarvinConfig, Resource, ResourceType
from apps.marvins.serializers import (
    CapabilitySerializer,
    MarvinConfigSerializer,
    MarvinSerializer,
    ResourceSerializer,
    ResourceTypeSerializer,
)


class OrganizationFilterMixin:
    def get_queryset(self):
        qs = self.queryset
        user = self.request.user
        if not user.is_authenticated:
            return qs.none()
        if user.is_superuser:
            return qs
        return qs.filter(organization__in=user.organizations.all())


class ResourceTypeViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = ResourceType.objects.all()
    serializer_class = ResourceTypeSerializer
    permission_classes = [permissions.IsAuthenticated]


class ResourceViewSet(OrganizationFilterMixin, viewsets.ModelViewSet):
    queryset = Resource.objects.all()
    serializer_class = ResourceSerializer
    permission_classes = [permissions.IsAuthenticated]

    @action(detail=True, methods=["post"])
    def attach_to_marvin(self, request, pk=None):
        resource = self.get_object()
        marvin_id = request.data.get("marvin_id")
        if not marvin_id:
            return Response({"error": "marvin_id is required"}, status=400)
        from apps.marvins.models import Marvin

        try:
            marvin = Marvin.objects.get(id=marvin_id, organization=resource.organization)
        except Marvin.DoesNotExist:
            return Response({"error": "Marvin not found"}, status=404)
        marvin.attached_resources.add(resource)
        return Response({"status": "attached"})

    @action(detail=True, methods=["post"])
    def detach_from_marvin(self, request, pk=None):
        resource = self.get_object()
        marvin_id = request.data.get("marvin_id")
        if not marvin_id:
            return Response({"error": "marvin_id is required"}, status=400)
        from apps.marvins.models import Marvin

        try:
            marvin = Marvin.objects.get(id=marvin_id, organization=resource.organization)
        except Marvin.DoesNotExist:
            return Response({"error": "Marvin not found"}, status=404)
        marvin.attached_resources.remove(resource)
        return Response({"status": "detached"})


class MarvinConfigViewSet(viewsets.ModelViewSet):
    queryset = MarvinConfig.objects.all()
    serializer_class = MarvinConfigSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        qs = self.queryset
        user = self.request.user
        if not user.is_authenticated:
            return qs.none()
        if user.is_superuser:
            return qs
        return qs.filter(marvin__organization__in=user.organizations.all())


class MarvinViewSet(OrganizationFilterMixin, viewsets.ModelViewSet):
    queryset = Marvin.objects.all()
    serializer_class = MarvinSerializer
    permission_classes = [permissions.IsAuthenticated]


class CapabilityViewSet(OrganizationFilterMixin, viewsets.ModelViewSet):
    queryset = Capability.objects.all()
    serializer_class = CapabilitySerializer
    permission_classes = [permissions.IsAuthenticated]
