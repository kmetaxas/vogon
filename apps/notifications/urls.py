from django.urls import path

from apps.notifications import views

app_name = "notifications"

urlpatterns = [
    path("", views.NotificationsSettingsView.as_view(), name="settings"),
    path("channels/", views.ChannelListView.as_view(), name="channel-list"),
    path("channels/create/", views.ChannelCreateView.as_view(), name="channel-create"),
    path(
        "channels/<uuid:pk>/update/",
        views.ChannelUpdateView.as_view(),
        name="channel-update",
    ),
    path(
        "channels/<uuid:pk>/delete/",
        views.ChannelDeleteView.as_view(),
        name="channel-delete",
    ),
    path("policies/", views.PolicyListView.as_view(), name="policy-list"),
    path("policies/create/", views.PolicyCreateView.as_view(), name="policy-create"),
    path(
        "policies/<uuid:pk>/update/",
        views.PolicyUpdateView.as_view(),
        name="policy-update",
    ),
    path(
        "policies/<uuid:pk>/delete/",
        views.PolicyDeleteView.as_view(),
        name="policy-delete",
    ),
    path(
        "policies/<uuid:pk>/routes/",
        views.RouteConfigView.as_view(),
        name="route-config",
    ),
]
