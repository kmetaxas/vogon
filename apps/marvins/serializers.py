from typing import Any

from rest_framework import serializers

from apps.marvins.models import (
    Capability,
    Marvin,
    MarvinConfig,
    Resource,
    ResourceType,
)


class ResourceTypeSerializer(serializers.ModelSerializer):
    class Meta:
        model = ResourceType
        fields = [
            "id",
            "name",
            "display_name",
            "description",
            "config_json_schema",
            "capability_names",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class ResourceSerializer(serializers.ModelSerializer):
    resource_type_name = serializers.CharField(source="resource_type.name", read_only=True)

    class Meta:
        model = Resource
        fields = [
            "id",
            "organization",
            "name",
            "resource_type",
            "resource_type_name",
            "description",
            "config",
            "secrets_encrypted",
            "enabled",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class MarvinConfigSerializer(serializers.ModelSerializer):
    class Meta:
        model = MarvinConfig
        fields = [
            "id",
            "marvin",
            "global_config",
            "capability_overrides",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "marvin", "created_at", "updated_at"]


class CapabilitySerializer(serializers.ModelSerializer):
    class Meta:
        model = Capability
        fields = [
            "id",
            "organization",
            "name",
            "description",
            "json_schema",
            "keywords",
            "enabled",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class _LabelsField(serializers.Field):
    """Serialize Marvin labels list as a key:value dict."""

    def to_representation(self, value: list[str] | None) -> dict[str, str | None]:
        out: dict[str, str | None] = {}
        for label in value or []:
            if ":" in label:
                k, v = label.split(":", 1)
                out[k.strip()] = v.strip()
            else:
                out[label.strip()] = None
        return out


class MarvinSerializer(serializers.ModelSerializer):
    capabilities = CapabilitySerializer(many=True, read_only=True)
    online_config = MarvinConfigSerializer(read_only=True)
    attached_resources = ResourceSerializer(many=True, read_only=True)
    labels = _LabelsField()
    host_metadata = serializers.SerializerMethodField()

    def get_host_metadata(self, obj: Marvin) -> dict[str, Any]:
        return {
            "client_id": obj.client_id,
            "hostname": obj.hostname,
            "provider": obj.provider,
            "region": obj.region,
            "availability_zone": obj.availability_zone,
            "local_ip": obj.local_ip,
            "vm_id": obj.vm_id,
            "os": obj.os,
            "os_version": obj.os_version,
            "arch": obj.arch,
        }

    class Meta:
        model = Marvin
        fields = [
            "id",
            "organization",
            "name",
            "status",
            "last_seen",
            "capabilities",
            "labels",
            "host_metadata",
            "capability_configs",
            "attached_resources",
            "online_config",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]
