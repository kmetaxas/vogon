from django.urls import path

from . import views

app_name = "personalities"

urlpatterns = [
    path("", views.PersonalityListView.as_view(), name="personality-list"),
    path("create/", views.PersonalityCreateView.as_view(), name="personality-create"),
    path(
        "<uuid:personality_id>/", views.PersonalityDetailView.as_view(), name="personality-detail"
    ),
    path(
        "<uuid:personality_id>/edit/", views.PersonalityEditView.as_view(), name="personality-edit"
    ),
    path(
        "<uuid:personality_id>/delete/",
        views.PersonalityDeleteView.as_view(),
        name="personality-delete",
    ),
]
