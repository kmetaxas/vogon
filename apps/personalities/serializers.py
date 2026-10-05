from rest_framework import serializers

from apps.personalities.models import Personality


class PersonalitySerializer(serializers.ModelSerializer):
    class Meta:
        model = Personality
        fields = [
            "id",
            "name",
            "description",
            "prompt_text",
            "category",
            "tags",
            "scope",
            "organization",
            "created_by",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "created_at",
            "updated_at",
            "created_by",
            "scope",
            "organization",
        ]
