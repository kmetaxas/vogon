"""apps/ws/routing.py - WebSocket URL routing."""

from django.urls import re_path

from . import consumers

websocket_urlpatterns = [
    re_path(
        r"ws/sessions/(?P<session_id>[0-9a-f-]+)/$",
        consumers.SessionConsumer.as_asgi(),
    ),
]
