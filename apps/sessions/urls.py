# pyright: reportAttributeAccessIssue=false

from django.urls import path

from . import views

app_name = "sessions"

urlpatterns = [
    path("", views.SessionListView.as_view(), name="session-list"),
    path("new/", views.SessionCreateView.as_view(), name="session-create"),
    path("active/", views.ActiveSessionListView.as_view(), name="active-list"),
    path("history/", views.SessionHistoryListView.as_view(), name="history-list"),
    path("<uuid:session_id>/", views.SessionDetailView.as_view(), name="session-detail"),
    path("<uuid:session_id>/status/", views.SessionStatusView.as_view(), name="session-status"),
    path(
        "<uuid:session_id>/complete/", views.SessionCompleteView.as_view(), name="session-complete"
    ),
    path(
        "<uuid:session_id>/architecture/",
        views.ArchitectureRequestUploadView.as_view(),
        name="architecture-upload",
    ),
    # Thread-scoped routes
    path(
        "<uuid:session_id>/threads/",
        views.ThreadNavPartialView.as_view(),
        name="thread-nav-partial",
    ),
    path(
        "<uuid:session_id>/threads/<uuid:thread_id>/",
        views.ThreadDetailPartialView.as_view(),
        name="thread-detail-partial",
    ),
    path(
        "<uuid:session_id>/threads/<uuid:thread_id>/card/",
        views.ThreadFollowingCardPartialView.as_view(),
        name="thread-card-partial",
    ),
    path(
        "<uuid:session_id>/threads/<uuid:thread_id>/messages/",
        views.ThreadMessageListView.as_view(),
        name="thread-message-list",
    ),
    path(
        "<uuid:session_id>/threads/<uuid:thread_id>/send/",
        views.ThreadSendMessageView.as_view(),
        name="thread-send-message",
    ),
    path(
        "<uuid:session_id>/threads/<uuid:thread_id>/follow/",
        views.ThreadFollowView.as_view(),
        name="thread-follow",
    ),
    path(
        "<uuid:session_id>/threads/<uuid:thread_id>/unfollow/",
        views.ThreadUnfollowView.as_view(),
        name="thread-unfollow",
    ),
]
