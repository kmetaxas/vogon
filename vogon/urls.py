from django.contrib import admin
from django.urls import include, path

from apps.checks.webhook_views import CheckReceiverWebhookView

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/", include("allauth.urls")),
    path("api/", include("vogon.api_urls")),
    path(
        "api/check-receivers/<uuid:receiver_id>/<str:secret>/",
        CheckReceiverWebhookView.as_view(),
        name="check-receiver-webhook",
    ),
    path("marvins/", include("apps.marvins.urls")),
    path("sessions/", include("apps.sessions.urls")),
    path("designs/", include("apps.infradesigns.urls")),
    path("checks/", include("apps.checks.urls")),
    path("personalities/", include("apps.personalities.urls")),
    path("notifications/", include("apps.notifications.urls")),
    path("", include("apps.core.urls")),
]
