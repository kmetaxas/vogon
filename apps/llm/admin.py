from django.contrib import admin

from apps.llm.models import LLMProvider


@admin.register(LLMProvider)
class LLMProviderAdmin(admin.ModelAdmin):
    list_display = ["name", "provider_type", "model", "is_default", "enabled"]
    list_filter = ["provider_type", "enabled"]
