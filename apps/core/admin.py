from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from apps.core.models import (
    Organization,
    OrganizationInvitation,
    OrganizationMembership,
    User,
)


class OrganizationMembershipInline(admin.TabularInline):
    model = OrganizationMembership
    extra = 1
    autocomplete_fields = ["organization"]


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    inlines = list(BaseUserAdmin.inlines) + [OrganizationMembershipInline]
    list_display = BaseUserAdmin.list_display + ("get_orgs",)
    search_fields = BaseUserAdmin.search_fields + ("organizations__name",)

    @admin.display(description="Organizations")
    def get_orgs(self, obj):
        return ", ".join(o.name for o in obj.organizations.all())


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ["name", "slug", "email_domain", "created_at"]
    search_fields = ["name", "slug", "email_domain"]
    readonly_fields = ["id"]
    inlines = [OrganizationMembershipInline]


@admin.register(OrganizationMembership)
class OrganizationMembershipAdmin(admin.ModelAdmin):
    list_display = ["user", "organization", "role", "joined_at"]
    list_filter = ["role"]
    search_fields = ["user__username", "organization__name"]
    autocomplete_fields = ["user", "organization"]


@admin.register(OrganizationInvitation)
class OrganizationInvitationAdmin(admin.ModelAdmin):
    list_display = ["organization", "email", "code", "created_at", "accepted_at"]
    list_filter = ["organization", "accepted_at"]
    search_fields = ["email", "code", "organization__name"]
    readonly_fields = ["id", "created_at"]
    autocomplete_fields = ["organization", "created_by"]
