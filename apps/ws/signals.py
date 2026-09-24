"""apps/ws/signals.py - Broadcast model changes over WebSocket via channel layer."""

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.template.loader import render_to_string

from apps.sessions.models import AgentEvent, Message, Thread, TSession


def _chat_payload(thread):
    from apps.sessions.views import _thread_messages_context

    session = thread.tsession
    html = render_to_string(
        "sessions/_chat_messages.html",
        _thread_messages_context(session, thread),
    )
    card_html = render_to_string(
        "sessions/_following_card.html",
        {"session": session, "thread": thread},
    )
    return {"html": html, "card_html": card_html}


@receiver(post_save, sender=Thread)
def broadcast_thread_list(sender, instance, created, **kwargs):
    if not created:
        return
    layer = get_channel_layer()
    if layer is None:
        return
    async_to_sync(layer.group_send)(
        f"session_{instance.tsession_id}",
        {
            "type": "thread_update",
            "update_type": "thread_list",
            "thread_id": str(instance.id),
            "payload": {},
        },
    )


@receiver(post_save, sender=Message)
def broadcast_message(sender, instance, created, **kwargs):
    if not created:
        return
    layer = get_channel_layer()
    if layer is None:
        return
    group = f"thread_{instance.thread_id}"

    async_to_sync(layer.group_send)(
        group,
        {
            "type": "thread_update",
            "update_type": "chat",
            "thread_id": str(instance.thread_id),
            "payload": _chat_payload(instance.thread),
        },
    )


@receiver(post_save, sender=AgentEvent)
def broadcast_agent_event(sender, instance, created, **kwargs):
    if not created:
        return
    layer = get_channel_layer()
    if layer is None:
        return
    group = f"thread_{instance.thread_id}"

    async_to_sync(layer.group_send)(
        group,
        {
            "type": "thread_update",
            "update_type": "chat",
            "thread_id": str(instance.thread_id),
            "payload": _chat_payload(instance.thread),
        },
    )


@receiver(post_save, sender=TSession)
def broadcast_session_status(sender, instance, created, **kwargs):
    if created:
        return
    layer = get_channel_layer()
    if layer is None:
        return
    # Broadcast status update to all threads in the session so any subscriber sees it
    for thread in instance.threads.all():
        group = f"thread_{thread.id}"
        badge_html = render_to_string("sessions/_status_badge.html", {"session": instance})
        async_to_sync(layer.group_send)(
            group,
            {
                "type": "thread_update",
                "update_type": "status",
                "thread_id": str(thread.id),
                "payload": {"status": instance.status, "badge_html": badge_html},
            },
        )
