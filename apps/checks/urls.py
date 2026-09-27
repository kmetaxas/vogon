from django.urls import path

from apps.checks.views import CheckCreateView, CheckDetailView, CheckEditView, CheckListView

app_name = "checks"

urlpatterns = [
    path("", CheckListView.as_view(), name="check-list"),
    path("<uuid:check_id>/", CheckDetailView.as_view(), name="check-detail"),
    path("<uuid:check_id>/edit/", CheckEditView.as_view(), name="check-edit"),
    path("new/", CheckCreateView.as_view(), name="check-create"),
]
