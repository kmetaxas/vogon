from django.urls import path

from apps.checks.views import (
    CheckCreateView,
    CheckDeleteView,
    CheckDetailView,
    CheckDryRunView,
    CheckEditView,
    CheckExecutionDetailView,
    CheckListView,
    CheckToggleView,
    CheckTriggerView,
    ReceiverCreateView,
    ReceiverDeleteView,
    ReceiverDetailView,
    ReceiverEditView,
    ReceiverListView,
    ReceiverRegenerateSecretView,
    ReceiverToggleEnabledView,
)

app_name = "checks"

urlpatterns = [
    path("", CheckListView.as_view(), name="check-list"),
    path("<uuid:check_id>/", CheckDetailView.as_view(), name="check-detail"),
    path("<uuid:check_id>/edit/", CheckEditView.as_view(), name="check-edit"),
    path("<uuid:check_id>/toggle/", CheckToggleView.as_view(), name="check-toggle"),
    path("<uuid:check_id>/trigger/", CheckTriggerView.as_view(), name="check-trigger"),
    path("<uuid:check_id>/dry-run/", CheckDryRunView.as_view(), name="check-dry-run"),
    path("<uuid:check_id>/delete/", CheckDeleteView.as_view(), name="check-delete"),
    path(
        "<uuid:check_id>/executions/<uuid:execution_id>/",
        CheckExecutionDetailView.as_view(),
        name="check-execution-detail",
    ),
    path("new/", CheckCreateView.as_view(), name="check-create"),
    path("receivers/", ReceiverListView.as_view(), name="receiver-list"),
    path("receivers/new/", ReceiverCreateView.as_view(), name="receiver-create"),
    path("receivers/<uuid:receiver_id>/", ReceiverDetailView.as_view(), name="receiver-detail"),
    path("receivers/<uuid:receiver_id>/edit/", ReceiverEditView.as_view(), name="receiver-edit"),
    path(
        "receivers/<uuid:receiver_id>/delete/", ReceiverDeleteView.as_view(), name="receiver-delete"
    ),
    path(
        "receivers/<uuid:receiver_id>/regenerate-secret/",
        ReceiverRegenerateSecretView.as_view(),
        name="receiver-regenerate-secret",
    ),
    path(
        "receivers/<uuid:receiver_id>/toggle/",
        ReceiverToggleEnabledView.as_view(),
        name="receiver-toggle",
    ),
]
