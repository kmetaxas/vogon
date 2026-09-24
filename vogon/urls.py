from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/", include("allauth.urls")),
    path("api/", include("vogon.api_urls")),
    path("marvins/", include("apps.marvins.urls")),
    path("sessions/", include("apps.sessions.urls")),
    path("designs/", include("apps.infradesigns.urls")),
    path("", include("apps.core.urls")),
]
