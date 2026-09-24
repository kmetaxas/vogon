from rest_framework import serializers

from apps.infradesigns.models import InfrastructureDesign


class InfrastructureDesignSerializer(serializers.ModelSerializer):
    class Meta:
        model = InfrastructureDesign
        fields = [
            "id",
            "name",
            "description",
            "environment",
            "mermaid_topology",
            "marvin_selector",
            "created_by",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_by", "created_at", "updated_at"]
