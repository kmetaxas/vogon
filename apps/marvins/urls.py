from django.urls import path

from . import views

app_name = "marvins"

urlpatterns = [
    path("", views.MarvinListView.as_view(), name="marvin-list"),
    path(
        "registration-keys/",
        views.MarvinRegistrationKeyListView.as_view(),
        name="registration-key-list",
    ),
    path(
        "registration-keys/create/",
        views.MarvinRegistrationKeyCreateView.as_view(),
        name="registration-key-create",
    ),
    path(
        "registration-keys/<uuid:key_id>/delete/",
        views.MarvinRegistrationKeyDeleteView.as_view(),
        name="registration-key-delete",
    ),
    path("resources/", views.ResourceListView.as_view(), name="resource-list"),
    path("resources/create/", views.ResourceCreateView.as_view(), name="resource-create"),
    path(
        "resources/<uuid:resource_id>/edit/", views.ResourceEditView.as_view(), name="resource-edit"
    ),
    path("<uuid:marvin_id>/", views.MarvinDetailView.as_view(), name="marvin-detail"),
    path(
        "<uuid:marvin_id>/delete/",
        views.MarvinDeleteView.as_view(),
        name="marvin-delete",
    ),
    path(
        "<uuid:marvin_id>/config/",
        views.MarvinConfigUpdateView.as_view(),
        name="marvin-config-update",
    ),
    path(
        "<uuid:marvin_id>/attach-resource/",
        views.MarvinAttachResourceView.as_view(),
        name="marvin-attach-resource",
    ),
    path(
        "<uuid:marvin_id>/detach-resource/",
        views.MarvinDetachResourceView.as_view(),
        name="marvin-detach-resource",
    ),
]
