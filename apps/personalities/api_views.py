from django.db.models import Q
from rest_framework import permissions, viewsets
from rest_framework.exceptions import PermissionDenied

from apps.personalities.models import Personality
from apps.personalities.serializers import PersonalitySerializer


class PersonalityViewSet(viewsets.ModelViewSet):
    queryset = Personality.objects.all()
    serializer_class = PersonalitySerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        if not user.is_authenticated:
            return Personality.objects.none()
        if user.is_superuser:
            return Personality.objects.all()
        organization = user.get_current_organization()
        if organization is None:
            return Personality.objects.filter(
                Q(scope=Personality.Scope.SYSTEM, organization__isnull=True)
            )
        return Personality.objects.filter(
            Q(organization=organization)
            | Q(scope=Personality.Scope.SYSTEM, organization__isnull=True)
        )

    def _is_admin(self, organization):
        if self.request.user.is_superuser:
            return True
        if organization is None:
            return False
        membership = self.request.user.memberships.filter(organization=organization).first()
        return bool(membership and membership.is_admin())

    def perform_create(self, serializer):
        organization = self.request.user.get_current_organization()
        if not self._is_admin(organization):
            raise PermissionDenied("Only organization admins can create personalities.")
        serializer.save(organization=organization, created_by=self.request.user)

    def perform_update(self, serializer):
        instance = serializer.instance
        user = self.request.user
        if instance.scope == Personality.Scope.SYSTEM and not user.is_superuser:
            raise PermissionDenied("System personalities can only be edited by superusers.")
        if not user.is_superuser and instance.organization != user.get_current_organization():
            raise PermissionDenied("You cannot edit personalities from another organization.")
        if not self._is_admin(instance.organization):
            raise PermissionDenied("Only organization admins can edit personalities.")
        serializer.save()

    def perform_destroy(self, instance):
        user = self.request.user
        if instance.scope == Personality.Scope.SYSTEM and not user.is_superuser:
            raise PermissionDenied("System personalities can only be deleted by superusers.")
        if not user.is_superuser and instance.organization != user.get_current_organization():
            raise PermissionDenied("You cannot delete personalities from another organization.")
        if not self._is_admin(instance.organization):
            raise PermissionDenied("Only organization admins can delete personalities.")
        instance.delete()
