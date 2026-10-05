from django.contrib import admin

from apps.personalities.models import Personality


@admin.register(Personality)
class PersonalityAdmin(admin.ModelAdmin):
    list_display = ["name", "scope", "organization", "category", "created_at"]
    list_filter = ("scope",)
    search_fields = ("name", "category")
