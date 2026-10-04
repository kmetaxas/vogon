from django.urls import path

from apps.checks.views import (
    CheckCreateView,
    CheckDetailView,
    CheckDryRunView,
    CheckEditView,
    CheckExecutionDetailView,
    CheckListView,
    CheckToggleView,
    CheckTriggerView,
)

app_name = "checks"

urlpatterns = [
    path("", CheckListView.as_view(), name="check-list"),
    path("<uuid:check_id>/", CheckDetailView.as_view(), name="check-detail"),
    path("<uuid:check_id>/edit/", CheckEditView.as_view(), name="check-edit"),
    path("<uuid:check_id>/toggle/", CheckToggleView.as_view(), name="check-toggle"),
    path("<uuid:check_id>/trigger/", CheckTriggerView.as_view(), name="check-trigger"),
    path("<uuid:check_id>/dry-run/", CheckDryRunView.as_view(), name="check-dry-run"),
    path(
        "<uuid:check_id>/executions/<uuid:execution_id>/",
        CheckExecutionDetailView.as_view(),
        name="check-execution-detail",
    ),
    path("new/", CheckCreateView.as_view(), name="check-create"),
]
