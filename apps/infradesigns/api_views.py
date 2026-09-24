from rest_framework import viewsets

from apps.core.mixins import OrganizationQuerySetMixin
from apps.infradesigns.models import InfrastructureDesign
from apps.infradesigns.serializers import InfrastructureDesignSerializer


class InfrastructureDesignViewSet(OrganizationQuerySetMixin, viewsets.ModelViewSet):
    queryset = InfrastructureDesign.objects.all()
    serializer_class = InfrastructureDesignSerializer

    def perform_create(self, serializer):
        serializer.save(
            organization=self.request.user.get_current_organization(), created_by=self.request.user
        )
