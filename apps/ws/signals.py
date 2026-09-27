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


@receiver(post_save, sender=TSession)
def broadcast_usage(sender, instance, created, **kwargs):
    if created:
        return
    # Only broadcast if usage fields changed
    update_fields = kwargs.get("update_fields")
    usage_fields = {
        "total_tokens",
        "total_cost",
        "cumulative_context_tokens",
        "total_input_tokens",
        "total_output_tokens",
    }
    if update_fields is not None and not any(f in update_fields for f in usage_fields):
        return
    layer = get_channel_layer()
    if layer is None:
        return
    for thread in instance.threads.all():
        group = f"thread_{thread.id}"
        from apps.sessions.views import _resolved_provider

        model_names = set(
            instance.threads.filter(messages__role="assistant")
            .exclude(messages__model_name="")
            .values_list("messages__model_name", flat=True)
        )
        usage_html = render_to_string(
            "sessions/_usage_bar.html",
            {
                "session": instance,
                "resolved_provider": _resolved_provider(instance),
                "is_mixed_models": len(model_names) > 1,
                "model_names": model_names,
            },
        )
        async_to_sync(layer.group_send)(
            group,
            {
                "type": "thread_update",
                "update_type": "usage",
                "thread_id": str(thread.id),
                "payload": {
                    "total_tokens": instance.total_tokens,
                    "total_cost": str(instance.total_cost),
                    "cumulative_context_tokens": instance.cumulative_context_tokens,
                    "total_input_tokens": instance.total_input_tokens,
                    "total_output_tokens": instance.total_output_tokens,
                    "usage_html": usage_html,
                },
            },
        )
