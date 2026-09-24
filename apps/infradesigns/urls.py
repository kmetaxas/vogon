from django.urls import path

from . import views

app_name = "infradesigns"

urlpatterns = [
    path("", views.InfraDesignListView.as_view(), name="design-list"),
    path("create/", views.InfraDesignCreateView.as_view(), name="design-create"),
    path("<uuid:design_id>/", views.InfraDesignDetailView.as_view(), name="design-detail"),
    path("<uuid:design_id>/edit/", views.InfraDesignEditView.as_view(), name="design-edit"),
]
