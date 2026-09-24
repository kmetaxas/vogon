import logging

from django.http.response import HttpResponse, HttpResponseForbidden, HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views import View

from apps.core.mixins import OrganizationRequiredMixin
from apps.sessions.models import (
    AgentEvent,
    ArchitectureRequest,
    Message,
    Thread,
    ThreadMembership,
    ToolCall,
    TSession,
)
from apps.sessions.temporal_utils import (
    complete_session_workflow_sync,
    send_message_to_workflow_sync,
    start_troubleshoot_workflow_sync,
)

logger = logging.getLogger(__name__)


def _thread_ui_context(session, user):
    threads = list(session.threads_visible_to(user).order_by("-created_at"))
    for thread in threads:
        thread.is_followed_by_user = thread.is_followed_by(user)
    primary_thread = next((thread for thread in threads if thread.user_id == user.id), None)
    followed_threads = [
        thread for thread in threads if thread.user_id != user.id and thread.is_followed_by_user
    ]
    return {
        "session": session,
        "threads": threads,
        "primary_thread": primary_thread,
        "followed_threads": followed_threads,
        "can_complete_session": session.created_by_id == user.id,
    }


def _events_to_progress(events, active=False):
    """Convert a list of THINKING/TOOL events into a progress dict."""
    tool_calls = []
    tool_calls_by_key = {}
    reasoning = ""
    latest_label = "Assistant is working..."

    def _tool_key(detail):
        return detail.get("id") or detail.get("tool_call_id") or detail.get("name", "")

    for ev in events:
        kind = ev["kind"]
        detail = ev.get("detail", {})
        if kind == AgentEvent.Kind.THINKING:
            latest_label = ev.get("label", latest_label)
            if detail.get("reasoning"):
                reasoning = detail["reasoning"]
            elif detail.get("detail", {}).get("reasoning"):
                reasoning = detail["detail"]["reasoning"]
        elif kind == AgentEvent.Kind.TOOL_CALL_STARTED:
            tool_call = {
                "name": detail.get("name", ""),
                "status": "started",
                "arguments": detail.get("arguments", {}),
            }
            tool_calls.append(tool_call)
            key = _tool_key(detail)
            if key:
                tool_calls_by_key.setdefault(key, []).append(tool_call)
        elif kind == AgentEvent.Kind.TOOL_RESULT:
            key = _tool_key(detail)
            pending = tool_calls_by_key.get(key, []) if key else []
            tool_call = pending.pop(0) if pending else None
            if pending:
                tool_calls_by_key[key] = pending
            elif key and key in tool_calls_by_key:
                del tool_calls_by_key[key]
            if tool_call is None:
                tool_call = next(
                    (item for item in tool_calls if item.get("status") == "started"),
                    None,
                )
            if tool_call is None:
                tool_call = {"name": detail.get("name", ""), "status": "started"}
                tool_calls.append(tool_call)
            tool_call["status"] = "completed"
            tool_call["result"] = detail.get("result", {})

    if not reasoning and not tool_calls and latest_label == "Assistant is working...":
        return None

    return {
        "label": "Analysis complete" if not active else latest_label,
        "active": active,
        "tool_calls": tool_calls,
        "tool_calls_count": len(tool_calls),
        "reasoning": reasoning,
        "created_at": (
            events[-1]["created_at"].isoformat()
            if events and hasattr(events[-1]["created_at"], "isoformat")
            else events[-1]["created_at"] if events else ""
        ),
    }


def _build_chat_items(messages, events, assistant_progress=None):
    items = []
    for m in messages:
        if m.get("role") == "tool":
            continue
        if m.get("role") == "assistant" and m.get("metadata", {}).get("tool_calls"):
            continue
        if not m.get("content", "").strip():
            continue
        item = {
            "type": "message",
            "id": str(m["id"]),
            "role": m.get("role", ""),
            "content": m.get("content", ""),
            "kind": None,
            "label": None,
            "detail": None,
            "created_at": m["created_at"].isoformat(),
        }
        refs = m.get("metadata", {}).get("references", [])
        if refs:
            item["has_references"] = True
            item["references"] = refs
        items.append(item)

    for e in events:
        if e.get("kind") in {
            AgentEvent.Kind.THINKING,
            AgentEvent.Kind.TOOL_CALL_STARTED,
            AgentEvent.Kind.TOOL_RESULT,
        }:
            continue
        items.append(
            {
                "type": "event",
                "id": str(e["id"]),
                "role": None,
                "content": None,
                "kind": e.get("kind", ""),
                "label": e.get("label", ""),
                "detail": e.get("detail", {}),
                "created_at": e["created_at"].isoformat(),
            }
        )

    items.sort(key=lambda x: x["created_at"])

    # Build historical progress items from THINKING/TOOL events for completed turns.
    # Each user message "claims" the THINKING/TOOL events after it until the next user message.
    # The last user message's events are handled by assistant_progress (if provided).
    thinking_events = [
        {**e, "created_at": e["created_at"].isoformat()}
        for e in events
        if e.get("kind")
        in {
            AgentEvent.Kind.THINKING,
            AgentEvent.Kind.TOOL_CALL_STARTED,
            AgentEvent.Kind.TOOL_RESULT,
        }
    ]
    thinking_events.sort(key=lambda x: x["created_at"])

    user_items = [(idx, item) for idx, item in enumerate(items) if item.get("role") == "user"]
    insertions = []
    for i, (idx, user_item) in enumerate(user_items):
        user_ts = user_item["created_at"]
        next_user_ts = user_items[i + 1][1]["created_at"] if i + 1 < len(user_items) else None
        # If this is the last user message and assistant_progress exists, skip
        # historical grouping for this turn; assistant_progress handles it.
        if next_user_ts is None and assistant_progress:
            continue

        group = [
            e
            for e in thinking_events
            if e["created_at"] >= user_ts
            and (next_user_ts is None or e["created_at"] < next_user_ts)
        ]
        if group:
            progress = _events_to_progress(group, active=False)
            if progress:
                insertions.append(
                    {
                        "after_idx": idx,
                        "item": {
                            "type": "progress",
                            "id": f"progress-{user_item['id']}",
                            "role": None,
                            "content": None,
                            "kind": "progress",
                            "label": progress.get("label", ""),
                            "detail": progress,
                            "created_at": progress.get("created_at", ""),
                        },
                    }
                )

    # Insert in reverse index order so earlier indices remain valid.
    for entry in reversed(insertions):
        items.insert(entry["after_idx"] + 1, entry["item"])

    # Insert assistant_progress after the last user message
    if assistant_progress:
        last_user_idx = None
        for idx, item in enumerate(items):
            if item.get("role") == "user":
                last_user_idx = idx

        progress_item = {
            "type": "progress",
            "id": "progress",
            "role": None,
            "content": None,
            "kind": "progress",
            "label": assistant_progress.get("label", ""),
            "detail": assistant_progress,
            "created_at": assistant_progress.get("created_at", ""),
        }

        if last_user_idx is not None:
            items.insert(last_user_idx + 1, progress_item)
        else:
            items.append(progress_item)

    return items


def _build_assistant_progress(thread) -> dict | None:
    from django.utils import timezone

    last_user_message = (
        thread.messages.filter(role=Message.Role.USER).order_by("-created_at").first()
    )
    if last_user_message:
        anchor_at = last_user_message.created_at
        recent_events = thread.agent_events.filter(created_at__gte=anchor_at).order_by("created_at")
    else:
        from datetime import timedelta

        anchor_at = timezone.now() - timedelta(seconds=30)
        recent_events = thread.agent_events.filter(created_at__gte=anchor_at).order_by("created_at")

    if not recent_events.exists():
        return None

    has_assistant_answer = (
        thread.messages.filter(role=Message.Role.ASSISTANT, created_at__gte=anchor_at)
        .exclude(content="")
        .exists()
    )

    events = list(recent_events.values("kind", "label", "detail", "created_at"))
    progress = _events_to_progress(events, active=not has_assistant_answer)
    if progress is None:
        return None

    return {
        "label": "Analysis complete" if has_assistant_answer else progress["label"],
        "active": not has_assistant_answer,
        "tool_calls": progress["tool_calls"],
        "tool_calls_count": progress["tool_calls_count"],
        "reasoning": progress["reasoning"],
        "created_at": recent_events.last().created_at.isoformat(),
    }


def _thread_messages_context(session, thread, signal_error=None):
    messages = list(
        thread.latest_messages().values("id", "role", "content", "metadata", "created_at")
    )
    events = list(
        thread.agent_events.order_by("created_at").values(
            "id", "kind", "label", "detail", "created_at"
        )
    )
    progress = _build_assistant_progress(thread)
    return {
        "session": session,
        "thread": thread,
        "items": _build_chat_items(messages, events, assistant_progress=progress),
        "assistant_progress": progress,
        "signal_error": signal_error,
    }


class SessionListView(OrganizationRequiredMixin, View):
    def get(self, request):
        sessions = TSession.objects.filter(
            organization=self.organization,
        )
        context = {
            "sessions": sessions,
        }
        return render(request, "sessions/session_list.html", context)


class SessionCreateView(OrganizationRequiredMixin, View):
    def get(self, request):
        return render(request, "sessions/session_create.html")

    def post(self, request):
        title = request.POST.get("title", "New Troubleshooting Session")
        session = TSession.objects.create(
            organization=self.organization,
            title=title,
            status=TSession.Status.ACTIVE,
            created_by=request.user,
        )
        thread, _ = Thread.objects.get_or_create(tsession=session, user=request.user)
        session.temporal_workflow_id = f"tsession-{session.id}"
        session.save(update_fields=["temporal_workflow_id", "updated_at"])

        try:
            start_troubleshoot_workflow_sync(str(session.id), str(thread.id))
        except Exception:
            logger.exception(
                "Failed to start Temporal workflow for session %s",
                session.id,
            )

        return HttpResponseRedirect(
            reverse("sessions:session-detail", kwargs={"session_id": session.id})
        )


class SessionDetailView(OrganizationRequiredMixin, View):
    def get(self, request, session_id):
        session = get_object_or_404(
            TSession,
            id=session_id,
            organization=self.organization,
        )
        context = _thread_ui_context(session, request.user)
        primary_thread = context["primary_thread"]
        if not primary_thread:
            primary_thread, _ = Thread.objects.get_or_create(tsession=session, user=request.user)
            context = _thread_ui_context(session, request.user)
        context.update(_thread_messages_context(session, primary_thread))
        return render(request, "sessions/session_detail.html", context)


class ActiveSessionListView(OrganizationRequiredMixin, View):
    def get(self, request):
        sessions = TSession.active_for_user(request.user)[:10]
        context = {
            "sessions": sessions,
        }
        return render(request, "sessions/_active_list.html", context)


class SessionHistoryListView(OrganizationRequiredMixin, View):
    def get(self, request):
        sessions = TSession.completed_for_user(request.user)[:10]
        context = {
            "sessions": sessions,
        }
        return render(request, "sessions/_history_list.html", context)


class ThreadDetailPartialView(OrganizationRequiredMixin, View):
    def get(self, request, session_id, thread_id):
        session = get_object_or_404(TSession, id=session_id, organization=self.organization)
        thread = get_object_or_404(Thread, id=thread_id, tsession=session)
        if not thread.can_view(request.user):
            return HttpResponseForbidden(b"You cannot view this thread.")
        context = {
            "session": session,
            "thread": thread,
            **_thread_messages_context(session, thread),
            "is_primary": thread.user == request.user,
            "primary_thread": Thread.objects.filter(tsession=session, user=request.user).first(),
            "can_complete_session": session.created_by_id == request.user.id,
        }
        return render(request, "sessions/_thread_pane.html", context)


class ThreadNavPartialView(OrganizationRequiredMixin, View):
    def get(self, request, session_id):
        session = get_object_or_404(TSession, id=session_id, organization=self.organization)
        return render(
            request, "sessions/_thread_nav.html", _thread_ui_context(session, request.user)
        )


class ThreadMessageListView(OrganizationRequiredMixin, View):
    def get(self, request, session_id, thread_id):
        session = get_object_or_404(TSession, id=session_id, organization=self.organization)
        thread = get_object_or_404(Thread, id=thread_id, tsession=session)
        if not thread.can_view(request.user):
            return HttpResponseForbidden(b"You cannot view this thread.")
        context = _thread_messages_context(session, thread)
        return render(request, "sessions/_chat_messages.html", context)


class ThreadSendMessageView(OrganizationRequiredMixin, View):
    def post(self, request, session_id, thread_id):
        session = get_object_or_404(TSession, id=session_id, organization=self.organization)
        thread = get_object_or_404(Thread, id=thread_id, tsession=session)
        if thread.user != request.user:
            return HttpResponseForbidden(b"You can only send messages in your own thread.")
        content = request.POST.get("content", "").strip()
        signal_error = None
        if content:
            # Extract design references like [design:<uuid>]
            import re

            DESIGN_REF_RE = re.compile(r"\[design:([0-9a-f\-]{36})\]")
            design_ids = DESIGN_REF_RE.findall(content)
            references = []
            if design_ids:
                from apps.infradesigns.models import InfrastructureDesign

                designs = InfrastructureDesign.objects.filter(
                    organization=self.organization,
                    id__in=design_ids,
                )
                for d in designs:
                    references.append(
                        {
                            "type": "infrastructure_design",
                            "id": str(d.id),
                            "title": d.name,
                            "environment": d.environment,
                        }
                    )
                content = DESIGN_REF_RE.sub("", content).strip()

            Message.objects.create(
                thread=thread,
                role=Message.Role.USER,
                content=content or "(referenced a design)",
                metadata={"references": references} if references else {},
                created_by=request.user,
            )
            try:
                send_message_to_workflow_sync(
                    str(session.id),
                    {
                        "content": content,
                        "role": Message.Role.USER,
                        "thread_id": str(thread.id),
                        "is_continue": session.status == TSession.Status.PAUSED,
                    },
                )
            except Exception as exc:
                signal_error = (
                    "Failed to notify the assistant. Your message was saved but may not be "
                    "processed until the assistant reconnects."
                )
                logger.exception(
                    "Failed to signal Temporal workflow for session %s: %s",
                    session.id,
                    exc,
                )

        if request.headers.get("HX-Request"):
            if signal_error:
                return render(
                    request,
                    "sessions/_chat_messages.html",
                    _thread_messages_context(session, thread, signal_error=signal_error),
                )
            return HttpResponse(status=204)

        return HttpResponseRedirect(
            reverse("sessions:session-detail", kwargs={"session_id": session.id})
        )


class ThreadFollowView(OrganizationRequiredMixin, View):
    def post(self, request, session_id, thread_id):
        session = get_object_or_404(TSession, id=session_id, organization=self.organization)
        thread = get_object_or_404(Thread, id=thread_id, tsession=session)
        if not thread.can_view(request.user):
            return HttpResponseForbidden()
        membership, created = ThreadMembership.objects.get_or_create(
            thread=thread, user=request.user, defaults={"is_following": True}
        )
        if not created and not membership.is_following:
            membership.is_following = True
            membership.save(update_fields=["is_following"])
        if request.headers.get("HX-Request"):
            return render(
                request,
                "sessions/_thread_follow_response.html",
                _thread_ui_context(session, request.user),
            )
        return HttpResponseRedirect(
            reverse("sessions:session-detail", kwargs={"session_id": session.id})
        )


class ThreadUnfollowView(OrganizationRequiredMixin, View):
    def post(self, request, session_id, thread_id):
        session = get_object_or_404(TSession, id=session_id, organization=self.organization)
        thread = get_object_or_404(Thread, id=thread_id, tsession=session)
        ThreadMembership.objects.filter(thread=thread, user=request.user).delete()
        if request.headers.get("HX-Request"):
            return render(
                request,
                "sessions/_thread_follow_response.html",
                _thread_ui_context(session, request.user),
            )
        return HttpResponseRedirect(
            reverse("sessions:session-detail", kwargs={"session_id": session.id})
        )


class ThreadFollowingCardPartialView(OrganizationRequiredMixin, View):
    def get(self, request, session_id, thread_id):
        session = get_object_or_404(TSession, id=session_id, organization=self.organization)
        thread = get_object_or_404(Thread, id=thread_id, tsession=session)
        if not thread.is_followed_by(request.user):
            return HttpResponseForbidden(b"You are not following this thread.")
        context = {
            "session": session,
            "thread": thread,
        }
        return render(request, "sessions/_following_card.html", context)


class SessionStatusView(OrganizationRequiredMixin, View):
    def get(self, request, session_id):
        session = get_object_or_404(
            TSession,
            id=session_id,
            organization=self.organization,
        )
        return render(
            request,
            "sessions/_status_badge.html",
            {"session": session},
        )


class SessionCompleteView(OrganizationRequiredMixin, View):
    def post(self, request, session_id):
        session = get_object_or_404(
            TSession,
            id=session_id,
            organization=self.organization,
        )
        if session.created_by_id != request.user.id:
            return HttpResponseForbidden(b"Only the session creator can close this session.")
        session.status = TSession.Status.COMPLETED
        session.save(update_fields=["status", "updated_at"])

        for thread in session.threads.all():
            thread.tool_calls.filter(
                status__in=[ToolCall.Status.PENDING, ToolCall.Status.IN_PROGRESS]
            ).update(status=ToolCall.Status.FAILED)

        try:
            complete_session_workflow_sync(str(session.id))
        except Exception:
            logger.exception(
                "Failed to signal workflow completion for session %s",
                session.id,
            )

        if request.headers.get("HX-Request"):
            primary_thread = Thread.objects.filter(tsession=session, user=request.user).first()
            if primary_thread is None:
                return render(request, "sessions/_status_badge.html", {"session": session})
            messages = list(
                primary_thread.latest_messages().values(
                    "id", "role", "content", "metadata", "created_at"
                )
            )
            events = list(
                primary_thread.agent_events.order_by("created_at").values(
                    "id", "kind", "label", "detail", "created_at"
                )
            )
            return render(
                request,
                "sessions/_thread_pane.html",
                {
                    "session": session,
                    "thread": primary_thread,
                    "items": _build_chat_items(messages, events),
                    "is_primary": True,
                    "primary_thread": primary_thread,
                    "can_complete_session": True,
                },
            )

        return HttpResponseRedirect(
            reverse("sessions:session-detail", kwargs={"session_id": session.id})
        )


class ArchitectureRequestUploadView(OrganizationRequiredMixin, View):
    def get(self, request, session_id):
        session = get_object_or_404(
            TSession,
            id=session_id,
            organization=self.organization,
        )
        thread = get_object_or_404(Thread, tsession=session, user=request.user)
        open_requests = thread.architecture_requests.filter(
            status=ArchitectureRequest.Status.OPEN,
        )
        context = {
            "session": session,
            "thread": thread,
            "open_requests": open_requests,
        }
        return render(request, "sessions/architecture_upload.html", context)

    def post(self, request, session_id):
        session = get_object_or_404(
            TSession,
            id=session_id,
            organization=self.organization,
        )
        thread = get_object_or_404(Thread, tsession=session, user=request.user)
        request_id = request.POST.get("request_id")
        req = get_object_or_404(
            ArchitectureRequest,
            id=request_id,
            thread=thread,
        )
        req.markdown = request.POST.get("markdown", "")
        if "diagram" in request.FILES:
            req.diagram = request.FILES["diagram"]
        req.status = ArchitectureRequest.Status.FULFILLED
        req.save()
        return HttpResponseRedirect(
            reverse("sessions:session-detail", kwargs={"session_id": session.id})
        )
