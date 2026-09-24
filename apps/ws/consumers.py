"""apps/ws/consumers.py - Django Channels WebSocket consumers."""

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer

from apps.core.models import Organization
from apps.sessions.models import Thread, TSession


class SessionConsumer(AsyncJsonWebsocketConsumer):
    """Push chat items and status changes for visible threads in a session.

    Connects to /ws/sessions/<session_id>/
    Subscribes to thread_<id> groups for every thread the user can see.
    """

    async def connect(self):
        self.session_id = self.scope["url_route"]["kwargs"]["session_id"]
        self.user = self.scope["user"]
        if not getattr(self.user, "is_authenticated", False):
            await self.close()
            return

        self.session = await self._get_session(self.session_id)
        if not self.session:
            await self.close()
            return
        if self.channel_layer is None:
            await self.close()
            return

        # Subscribe to every thread the user can see in this session
        self.thread_ids = await self._get_visible_thread_ids(self.session_id)
        await self.channel_layer.group_add(f"session_{self.session_id}", self.channel_name)
        for tid in self.thread_ids:
            await self.channel_layer.group_add(f"thread_{tid}", self.channel_name)
        await self.accept()

    async def disconnect(self, code):
        if hasattr(self, "thread_ids") and self.channel_layer is not None:
            for tid in self.thread_ids:
                await self.channel_layer.group_discard(f"thread_{tid}", self.channel_name)
            await self.channel_layer.group_discard(f"session_{self.session_id}", self.channel_name)

    # -- incoming messages (not used for chat, but kept for future ping/ack) --
    async def receive_json(self, content, **kwargs):
        pass

    # -- outgoing messages broadcasted by signals --
    async def thread_update(self, event):
        if event["update_type"] == "thread_list" and self.channel_layer is not None:
            await self._sync_thread_groups()
        elif event.get("thread_id") and not await self._can_still_view_thread(event["thread_id"]):
            if self.channel_layer is not None:
                await self.channel_layer.group_discard(
                    f"thread_{event['thread_id']}", self.channel_name
                )
            self.thread_ids = [
                tid for tid in self.thread_ids if str(tid) != str(event["thread_id"])
            ]
            return
        await self.send_json(
            {
                "type": event["update_type"],  # "chat" | "status" | "thread_list"
                "thread_id": event.get("thread_id"),
                "payload": event["payload"],
            }
        )

    async def _sync_thread_groups(self):
        if self.channel_layer is None:
            return
        visible_thread_ids = await self._get_visible_thread_ids(self.session_id)
        visible_set = {str(tid) for tid in visible_thread_ids}
        current_set = {str(tid) for tid in self.thread_ids}
        for tid in visible_set - current_set:
            await self.channel_layer.group_add(f"thread_{tid}", self.channel_name)
        for tid in current_set - visible_set:
            await self.channel_layer.group_discard(f"thread_{tid}", self.channel_name)
        self.thread_ids = visible_thread_ids

    @database_sync_to_async
    def _get_session(self, session_id):
        session = TSession.objects.filter(id=session_id).first()
        if session is None:
            return None
        org_ids = Organization.objects.filter(memberships__user=self.user).values_list(
            "id", flat=True
        )
        if session.organization_id not in list(org_ids):
            return None
        return session

    @database_sync_to_async
    def _get_visible_thread_ids(self, session_id):
        session = TSession.objects.filter(id=session_id).first()
        if session is None:
            return []
        threads = Thread.visible_to_user(session, self.user)
        return list(threads.values_list("id", flat=True))

    @database_sync_to_async
    def _can_still_view_thread(self, thread_id):
        thread = Thread.objects.filter(id=thread_id, tsession_id=self.session_id).first()
        return thread is not None and thread.can_view(self.user)
