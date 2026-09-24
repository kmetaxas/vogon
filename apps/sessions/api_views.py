from django.db.models import Q
from rest_framework import permissions, viewsets
from rest_framework.exceptions import PermissionDenied

from apps.sessions.models import (
    AgentEvent,
    ArchitectureRequest,
    Message,
    Thread,
    ThreadMembership,
    ToolCall,
    TSession,
)
from apps.sessions.serializers import (
    AgentEventSerializer,
    ArchitectureRequestSerializer,
    MessageSerializer,
    ThreadMembershipSerializer,
    ThreadSerializer,
    ToolCallSerializer,
    TSessionSerializer,
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


class TSessionViewSet(OrganizationFilterMixin, viewsets.ModelViewSet):
    queryset = TSession.objects.all()
    serializer_class = TSessionSerializer
    permission_classes = [permissions.IsAuthenticated]


class ThreadViewSet(viewsets.ModelViewSet):
    serializer_class = ThreadSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        if not user.is_authenticated:
            return Thread.objects.none()
        if user.is_superuser:
            return Thread.objects.all()

        session_id = self.request.query_params.get("session")
        if session_id:
            try:
                session = TSession.objects.get(id=session_id)
            except TSession.DoesNotExist:
                return Thread.objects.none()
            return Thread.visible_to_user(session, user)

        return Thread.objects.filter(
            Q(user=user)
            | Q(
                visibility=Thread.Visibility.PUBLIC,
                tsession__organization__in=user.organizations.all(),
            )
            | Q(
                visibility=Thread.Visibility.SHARED,
                memberships__user=user,
            )
        ).distinct()


class ThreadMembershipViewSet(viewsets.ModelViewSet):
    serializer_class = ThreadMembershipSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        if not user.is_authenticated:
            return ThreadMembership.objects.none()
        qs = ThreadMembership.objects.filter(user=user)
        thread_id = self.request.query_params.get("thread")
        if thread_id:
            qs = qs.filter(thread_id=thread_id)
        return qs.select_related("thread", "user")

    def perform_create(self, serializer):
        thread = serializer.validated_data["thread"]
        user = self.request.user
        if not thread.can_view(user):
            raise PermissionDenied("You do not have access to this thread.")
        serializer.save(user=user)


class MessageViewSet(viewsets.ModelViewSet):
    queryset = Message.objects.all()
    serializer_class = MessageSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        qs = self.queryset
        user = self.request.user
        if not user.is_authenticated:
            return qs.none()
        thread_id = self.request.query_params.get("thread")
        if thread_id:
            qs = qs.filter(thread_id=thread_id)
        return qs.filter(thread__tsession__organization__in=user.organizations.all())


class ToolCallViewSet(viewsets.ModelViewSet):
    queryset = ToolCall.objects.all()
    serializer_class = ToolCallSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        qs = self.queryset
        user = self.request.user
        if not user.is_authenticated:
            return qs.none()
        thread_id = self.request.query_params.get("thread")
        if thread_id:
            qs = qs.filter(thread_id=thread_id)
        return qs.filter(thread__tsession__organization__in=user.organizations.all())


class AgentEventViewSet(viewsets.ModelViewSet):
    queryset = AgentEvent.objects.all()
    serializer_class = AgentEventSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        qs = self.queryset
        user = self.request.user
        if not user.is_authenticated:
            return qs.none()
        thread_id = self.request.query_params.get("thread")
        if thread_id:
            qs = qs.filter(thread_id=thread_id)
        return qs.filter(thread__tsession__organization__in=user.organizations.all())


class ArchitectureRequestViewSet(viewsets.ModelViewSet):
    queryset = ArchitectureRequest.objects.all()
    serializer_class = ArchitectureRequestSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        qs = self.queryset
        user = self.request.user
        if not user.is_authenticated:
            return qs.none()
        thread_id = self.request.query_params.get("thread")
        if thread_id:
            qs = qs.filter(thread_id=thread_id)
        return qs.filter(thread__tsession__organization__in=user.organizations.all())
