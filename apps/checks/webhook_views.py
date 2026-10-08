"""Webhook receiver view for CheckReceivers.

Provides a single Django View (not DRF) endpoint that accepts inbound
Prometheus Alertmanager v4 webhooks, validates the receiver secret, and
persists normalized events.
"""

# pyright: reportAttributeAccessIssue=false

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from apps.checks.enums import ReceiverDisposition
from apps.checks.models import CheckReceiver
from services.checks.receivers.rate_limit import RateLimiter
from services.checks.receivers.service import ReceiverService

if TYPE_CHECKING:
    from django.http import HttpRequest

logger = logging.getLogger(__name__)

# Response constants to avoid leaking receiver existence.
_UNAUTHORIZED_RESPONSE = JsonResponse({"detail": "Unauthorized"}, status=401)


@method_decorator(csrf_exempt, name="dispatch")
class CheckReceiverWebhookView(View):
    """POST-only webhook endpoint for a CheckReceiver.

    URL pattern:
        /api/check-receivers/<uuid:receiver_id>/<str:secret>/
    """

    def post(self, request: HttpRequest, receiver_id: str, secret: str) -> HttpResponse:
        logger.debug("Webhook POST for receiver %s", receiver_id)

        # 1. Look up receiver by UUID — same response as invalid secret
        #    so we don't leak existence.
        try:
            receiver = CheckReceiver.objects.get(id=receiver_id)
        except CheckReceiver.DoesNotExist:
            return _UNAUTHORIZED_RESPONSE

        # 2. Disabled receiver
        if not receiver.enabled:
            return JsonResponse({"detail": "Receiver is disabled"}, status=403)

        # 3. Rate limit check (before secret validation)
        if not RateLimiter.is_allowed(receiver_id):
            response = JsonResponse({"detail": "Rate limit exceeded"}, status=429)
            response["Retry-After"] = str(60)
            return response

        # 4. Validate secret
        if not receiver.validate_secret(secret):
            return _UNAUTHORIZED_RESPONSE

        # 5. Payload size check (must happen before parsing)
        max_bytes = getattr(settings, "CHECK_RECEIVER_MAX_PAYLOAD_BYTES", 1_048_576)
        if len(request.body) > max_bytes:
            return JsonResponse({"detail": "Payload too large"}, status=413)

        # 6. Parse JSON body
        try:
            payload = json.loads(request.body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JsonResponse({"detail": "Invalid JSON"}, status=400)

        # Ingest payload via ReceiverService (parses + pipelines)
        receiver_events = ReceiverService.process_payload(receiver, payload)
        events_received = len(receiver_events)
        suppressed_dispositions = {
            ReceiverDisposition.SUPPRESSED.value,
            ReceiverDisposition.SUPPRESS_LOW_VALUE.value,
        }
        events_suppressed = sum(
            1 for e in receiver_events if e.disposition in suppressed_dispositions
        )
        events_processed = events_received - events_suppressed

        return JsonResponse(
            {
                "events_received": events_received,
                "events_processed": events_processed,
                "events_suppressed": events_suppressed,
                "status": "admission_evaluating",
                "message": (
                    "Events accepted. Admission gate evaluation is running asynchronously."
                ),
            },
            status=202,
        )
