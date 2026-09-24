"""Architecture design request standard tool for the LLM agent."""

from django.urls import reverse

from apps.sessions.models import ArchitectureRequest


def request_architecture_design(thread, description: str) -> dict:
    req = ArchitectureRequest.objects.create(
        thread=thread,
        description=description,
    )
    return {
        "id": str(req.id),
        "status": req.status,
        "description": req.description,
        "url": reverse(
            "sessions:architecture-upload",
            kwargs={"session_id": thread.tsession.id},
        ),
        "message": (
            "Architecture design request created. "
            "A human can upload the diagram or markdown at the URL provided."
        ),
    }


def get_architecture_design(thread) -> dict:
    req = (
        thread.architecture_requests.filter(
            status=ArchitectureRequest.Status.FULFILLED,
        )
        .order_by("-fulfilled_at")
        .first()
    )
    if not req:
        return {"found": False}
    return {
        "found": True,
        "id": str(req.id),
        "description": req.description,
        "markdown": req.markdown,
        "diagram_url": req.diagram.url if req.diagram else None,
    }
