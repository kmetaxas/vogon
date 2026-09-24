from rest_framework import permissions, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.models import Organization, OrganizationMembership
from apps.core.serializers import OrganizationSerializer


class OrganizationViewSet(viewsets.ModelViewSet):
    queryset = Organization.objects.all()
    serializer_class = OrganizationSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        qs = self.queryset
        user = self.request.user
        if not user.is_authenticated:
            return qs.none()
        if user.is_superuser:
            return qs
        return qs.filter(id__in=user.organizations.values_list("id", flat=True))

    @action(detail=True, methods=["post"], url_path="rotate-mcp-token")
    def rotate_mcp_token(self, request, pk=None):
        organization = self.get_object()
        user = request.user
        if not user.is_superuser:
            is_admin = OrganizationMembership.objects.filter(
                organization=organization,
                user=user,
                role__in=[
                    OrganizationMembership.Role.OWNER,
                    OrganizationMembership.Role.ADMIN,
                ],
            ).exists()
            if not is_admin:
                return Response({"detail": "Not authorized."}, status=403)

        token = organization.generate_mcp_token()
        return Response(
            {
                "organization_id": str(organization.id),
                "organization_slug": organization.slug,
                "mcp_api_token": token,
            }
        )
