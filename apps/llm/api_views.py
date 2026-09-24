from rest_framework import permissions, viewsets

from apps.llm.models import LLMProvider
from apps.llm.serializers import LLMProviderSerializer


class OrganizationFilterMixin:
    def get_queryset(self):
        qs = self.queryset
        user = self.request.user
        if not user.is_authenticated:
            return qs.none()
        if user.is_superuser:
            return qs
        return qs.filter(organization__in=user.organizations.all())


class LLMProviderViewSet(OrganizationFilterMixin, viewsets.ModelViewSet):
    queryset = LLMProvider.objects.all()
    serializer_class = LLMProviderSerializer
    permission_classes = [permissions.IsAuthenticated]
