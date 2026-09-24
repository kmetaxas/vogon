from rest_framework import serializers

from apps.llm.models import LLMProvider


class LLMProviderSerializer(serializers.ModelSerializer):
    api_key = serializers.CharField(write_only=True, required=False, allow_blank=True)

    def validate(self, data):
        if data.get("is_default"):
            org = data.get("organization")
            if (
                LLMProvider.objects.filter(organization=org, is_default=True)
                .exclude(id=getattr(self.instance, "id", None))
                .exists()
            ):
                raise serializers.ValidationError(
                    {"is_default": "Organization already has a default provider."}
                )
        return data

    def create(self, validated_data):
        api_key = validated_data.pop("api_key", "")
        instance = super().create(validated_data)
        if api_key:
            instance.api_key = api_key
            instance.save(update_fields=["_api_key_encrypted"])
        return instance

    def update(self, instance, validated_data):
        api_key = validated_data.pop("api_key", None)
        instance = super().update(instance, validated_data)
        if api_key is not None:
            instance.api_key = api_key
            instance.save(update_fields=["_api_key_encrypted"])
        return instance

    class Meta:
        model = LLMProvider
        fields = [
            "id",
            "organization",
            "name",
            "provider_type",
            "base_url",
            "api_key",
            "model",
            "is_default",
            "enabled",
            "config",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]
