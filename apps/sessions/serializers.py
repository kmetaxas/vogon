from rest_framework import serializers

from apps.sessions.models import (
    AgentEvent,
    ArchitectureRequest,
    Message,
    Thread,
    ThreadMembership,
    ToolCall,
    TSession,
)


class TSessionSerializer(serializers.ModelSerializer):
    class Meta:
        model = TSession
        fields = [
            "id",
            "organization",
            "title",
            "status",
            "temporal_workflow_id",
            "created_by",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class ThreadSerializer(serializers.ModelSerializer):
    user_username = serializers.CharField(source="user.username", read_only=True)
    is_followed = serializers.SerializerMethodField()
    can_edit = serializers.SerializerMethodField()

    class Meta:
        model = Thread
        fields = [
            "id",
            "tsession",
            "title",
            "user",
            "user_username",
            "temporal_workflow_id",
            "status",
            "visibility",
            "is_followed",
            "can_edit",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def get_is_followed(self, obj):
        user = self.context.get("request", None) and self.context["request"].user
        if not user:
            return False
        return obj.is_followed_by(user)

    def get_can_edit(self, obj):
        user = self.context.get("request", None) and self.context["request"].user
        if not user:
            return False
        return obj.can_edit(user)


class ThreadMembershipSerializer(serializers.ModelSerializer):
    thread_title = serializers.CharField(source="thread.title", read_only=True)
    user_username = serializers.CharField(source="user.username", read_only=True)

    class Meta:
        model = ThreadMembership
        fields = [
            "id",
            "thread",
            "thread_title",
            "user",
            "user_username",
            "is_following",
            "created_at",
        ]
        read_only_fields = ["id", "user", "user_username", "created_at"]


class MessageSerializer(serializers.ModelSerializer):
    class Meta:
        model = Message
        fields = [
            "id",
            "thread",
            "role",
            "content",
            "tool_call",
            "created_by",
            "created_at",
        ]
        read_only_fields = ["id", "created_at"]


class ToolCallSerializer(serializers.ModelSerializer):
    class Meta:
        model = ToolCall
        fields = [
            "id",
            "thread",
            "capability",
            "parameters",
            "result",
            "status",
            "labels",
            "marvin",
            "timeout_seconds",
            "requested_by",
            "created_at",
            "completed_at",
        ]
        read_only_fields = ["id", "created_at", "completed_at"]


class AgentEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = AgentEvent
        fields = [
            "id",
            "thread",
            "kind",
            "label",
            "detail",
            "tool_call",
            "created_at",
        ]
        read_only_fields = ["id", "created_at"]


class ArchitectureRequestSerializer(serializers.ModelSerializer):
    class Meta:
        model = ArchitectureRequest
        fields = [
            "id",
            "thread",
            "description",
            "status",
            "diagram",
            "markdown",
            "created_at",
            "fulfilled_at",
        ]
        read_only_fields = ["id", "created_at", "fulfilled_at"]
