# pyright: reportAttributeAccessIssue=false, reportArgumentType=false
"""apps/ws/tests.py - Tests for WebSocket consumers."""

import pytest
from asgiref.sync import sync_to_async
from channels.layers import get_channel_layer
from channels.routing import URLRouter
from channels.testing import WebsocketCommunicator
from django.test import override_settings

from apps.core.models import Organization, User
from apps.sessions.models import Thread, ThreadMembership, TSession
from apps.ws.routing import websocket_urlpatterns


class _TestAuthMiddleware:
    """Minimal middleware that preserves user from scope or sets AnonymousUser."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        scope = dict(scope)
        if "user" not in scope:
            from django.contrib.auth.models import AnonymousUser

            scope["user"] = AnonymousUser()
        return await self.app(scope, receive, send)


def _test_application():
    """Build a fresh ASGI application for each test."""
    return _TestAuthMiddleware(URLRouter(websocket_urlpatterns))


@pytest.fixture(autouse=True)
def reset_channel_layer():
    """Reset the channel layer backend cache so tests pick up override_settings."""
    import channels.layers as cl

    original_backends = cl.channel_layers.backends.copy()
    cl.channel_layers.backends = {}
    yield
    cl.channel_layers.backends = original_backends


@override_settings(CHANNEL_LAYERS={"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}})
@pytest.mark.django_db(databases="__all__", transaction=True)
@pytest.mark.asyncio
async def test_consumer_accepts_member_and_receives_update():
    user = await sync_to_async(User.objects.create_user)(username="ws_user", password="pass")
    org = await sync_to_async(Organization.objects.create)(name="WS Org", slug="ws-org")
    await sync_to_async(org.memberships.create)(user=user, role="member")
    session = await sync_to_async(TSession.objects.create)(
        organization=org, title="WS Session", created_by=user
    )
    thread = (await sync_to_async(Thread.objects.get_or_create)(tsession=session, user=user))[0]

    communicator = WebsocketCommunicator(_test_application(), f"/ws/sessions/{session.id}/")
    communicator.scope["user"] = user
    connected, subprotocol = await communicator.connect(timeout=10)
    assert connected, f"Connection failed. subprotocol={subprotocol}"

    # Simulate a channel-layer broadcast to the thread group
    layer = get_channel_layer()
    assert layer is not None
    await layer.group_send(
        f"thread_{thread.id}",
        {
            "type": "thread_update",
            "update_type": "chat",
            "thread_id": str(thread.id),
            "payload": {"items": []},
        },
    )
    response = await communicator.receive_json_from()
    assert response["type"] == "chat"

    await communicator.disconnect()


@override_settings(CHANNEL_LAYERS={"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}})
@pytest.mark.django_db(databases="__all__", transaction=True)
@pytest.mark.asyncio
async def test_consumer_rejects_anonymous():
    communicator = WebsocketCommunicator(
        _test_application(), "/ws/sessions/00000000-0000-0000-0000-000000000001/"
    )
    connected, _ = await communicator.connect()
    assert not connected
    await communicator.disconnect()


@override_settings(CHANNEL_LAYERS={"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}})
@pytest.mark.django_db(databases="__all__", transaction=True)
@pytest.mark.asyncio
async def test_consumer_rejects_non_member():
    user = await sync_to_async(User.objects.create_user)(username="outsider", password="pass")
    org = await sync_to_async(Organization.objects.create)(name="Other Org", slug="other-org")
    await sync_to_async(org.memberships.create)(user=user, role="member")
    other_org = await sync_to_async(Organization.objects.create)(
        name="Target Org", slug="target-org"
    )
    session = await sync_to_async(TSession.objects.create)(
        organization=other_org, title="Target Session", created_by=user
    )

    communicator = WebsocketCommunicator(_test_application(), f"/ws/sessions/{session.id}/")
    communicator.scope["user"] = user
    connected, _ = await communicator.connect()
    assert not connected
    await communicator.disconnect()


@override_settings(CHANNEL_LAYERS={"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}})
@pytest.mark.django_db(databases="__all__", transaction=True)
@pytest.mark.asyncio
async def test_consumer_rejects_unknown_session():
    user = await sync_to_async(User.objects.create_user)(username="unknown_sess", password="pass")
    org = await sync_to_async(Organization.objects.create)(name="Unknown Org", slug="unknown-org")
    await sync_to_async(org.memberships.create)(user=user, role="member")

    communicator = WebsocketCommunicator(
        _test_application(),
        "/ws/sessions/00000000-0000-0000-0000-000000000001/",
    )
    communicator.scope["user"] = user
    connected, _ = await communicator.connect()
    assert not connected
    await communicator.disconnect()


@override_settings(CHANNEL_LAYERS={"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}})
@pytest.mark.django_db(databases="__all__", transaction=True)
@pytest.mark.asyncio
async def test_consumer_stops_sending_thread_updates_after_access_revoked():
    owner = await sync_to_async(User.objects.create_user)(username="thread_owner", password="pass")
    viewer = await sync_to_async(User.objects.create_user)(
        username="thread_viewer", password="pass"
    )
    org = await sync_to_async(Organization.objects.create)(name="Shared Org", slug="shared-org")
    await sync_to_async(org.memberships.create)(user=owner, role="owner")
    await sync_to_async(org.memberships.create)(user=viewer, role="member")
    session = await sync_to_async(TSession.objects.create)(
        organization=org, title="Shared Session", created_by=owner
    )
    thread = await sync_to_async(Thread.objects.create)(
        tsession=session, user=owner, visibility=Thread.Visibility.SHARED
    )
    membership = await sync_to_async(ThreadMembership.objects.create)(thread=thread, user=viewer)

    communicator = WebsocketCommunicator(_test_application(), f"/ws/sessions/{session.id}/")
    communicator.scope["user"] = viewer
    connected, _ = await communicator.connect(timeout=10)
    assert connected

    await sync_to_async(membership.delete)()
    layer = get_channel_layer()
    assert layer is not None
    await layer.group_send(
        f"thread_{thread.id}",
        {
            "type": "thread_update",
            "update_type": "chat",
            "thread_id": str(thread.id),
            "payload": {"html": "secret"},
        },
    )

    assert await communicator.receive_nothing(timeout=1, interval=0.1)

    await communicator.disconnect()
