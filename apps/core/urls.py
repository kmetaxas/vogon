from django.urls import path

from . import views

app_name = "core"

urlpatterns = [
    path("", views.IndexView.as_view(), name="index"),
    path("dashboard/", views.DashboardView.as_view(), name="dashboard"),
    path("profile/", views.ProfileView.as_view(), name="profile"),
    path("settings/", views.SettingsView.as_view(), name="settings"),
    path("org/setup/", views.OrganizationSetupView.as_view(), name="org-setup"),
    path("org/", views.OrganizationDetailView.as_view(), name="org-detail"),
    path("org/invite/", views.OrganizationInviteView.as_view(), name="org-invite"),
    path(
        "org/members/<int:user_id>/remove/",
        views.OrganizationMemberRemoveView.as_view(),
        name="org-member-remove",
    ),
    path(
        "org/members/<int:user_id>/role/",
        views.OrganizationMemberRoleUpdateView.as_view(),
        name="org-member-role-update",
    ),
]
