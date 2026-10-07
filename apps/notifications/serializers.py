import json

from rest_framework import serializers

from apps.notifications.models import (
    Notification,
    NotificationChannel,
    NotificationDelivery,
    NotificationPolicy,
    NotificationRoute,
)


class CredentialsField(serializers.Field):
    """Accept credentials as a dict or a JSON/plain string.

    - dict: used as-is
    - string: parsed as JSON; if that yields a dict it is used, otherwise the
      string is wrapped as ``{"value": <string>}`` for backwards compatibility.
    """

    def to_internal_value(self, data):
        if isinstance(data, dict):
            return data
        if isinstance(data, str):
            try:
                parsed = json.loads(data)
            except (json.JSONDecodeError, ValueError):
                return {"value": data}
            if isinstance(parsed, dict):
                return parsed
            return {"value": data}
        self.fail("invalid")

    def to_representation(self, value):
        return value


class NotificationChannelSerializer(serializers.ModelSerializer):
    credentials = CredentialsField(write_only=True, required=False)

    class Meta:
        model = NotificationChannel
        fields = [
            "id",
            "name",
            "provider_type",
            "config",
            "credentials",
            "enabled",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def create(self, validated_data):
        credentials = validated_data.pop("credentials", "")
        instance = super().create(validated_data)
        if credentials:
            instance.credentials = credentials
            instance.save(update_fields=["_credentials_encrypted"])
        return instance

    def update(self, instance, validated_data):
        credentials = validated_data.pop("credentials", None)
        instance = super().update(instance, validated_data)
        if credentials is not None:
            instance.credentials = credentials
            instance.save(update_fields=["_credentials_encrypted"])
        return instance

    def validate(self, attrs):
        from services.notifications.registry import registry

        provider_type = attrs.get("provider_type")
        config = attrs.get("config", {})
        credentials = attrs.get("credentials", {})
        merged = {**config, **credentials}

        try:
            provider_cls = registry.get(provider_type)
            errors = provider_cls().validate_config(merged)
            if errors:
                raise serializers.ValidationError({"config": errors})
        except KeyError:
            pass  # Unknown provider type; model-level check handles this

        return attrs


class NotificationPolicySerializer(serializers.ModelSerializer):
    class Meta:
        model = NotificationPolicy
        fields = [
            "id",
            "name",
            "is_default",
            "dedup_window_seconds",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class NotificationRouteSerializer(serializers.ModelSerializer):
    class Meta:
        model = NotificationRoute
        fields = [
            "id",
            "policy",
            "severity",
            "attention",
            "channel",
            "enabled",
            "created_at",
        ]
        read_only_fields = ["id", "created_at"]


class NotificationDeliverySerializer(serializers.ModelSerializer):
    class Meta:
        model = NotificationDelivery
        fields = [
            "id",
            "channel",
            "status",
            "attempt_count",
            "last_attempt_at",
            "delivered_at",
            "error_message",
        ]
        read_only_fields = ["id"]


class NotificationSerializer(serializers.ModelSerializer):
    deliveries = NotificationDeliverySerializer(many=True, read_only=True)

    class Meta:
        model = Notification
        fields = [
            "id",
            "severity",
            "attention",
            "title",
            "summary",
            "details",
            "source_type",
            "source_id",
            "context",
            "dedup_key",
            "status",
            "created_at",
            "updated_at",
            "deliveries",
        ]
        read_only_fields = ["id", "dedup_key", "created_at", "updated_at"]
