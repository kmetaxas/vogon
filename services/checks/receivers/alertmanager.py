"""Inbound Prometheus Alertmanager v4 webhook adapter.

This module converts Alertmanager v4 webhook payloads into a generic,
source-agnostic :class:`NormalizedEvent` representation. It is the inbound
counterpart to :class:`services.checks.formatters.alertmanager.AlertmanagerFormatter`
(which produces outbound Alertmanager payloads).

Only Alertmanager v4 payloads are supported. No version negotiation is
performed; callers are expected to route v4 payloads here.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

from services.checks.receivers.sanitization import PromptInjectionSanitizer

logger = logging.getLogger(__name__)

# Alertmanager v4 top-level keys that must be present for a payload to be
# considered structurally valid.
_REQUIRED_TOP_LEVEL_KEYS = (
    "version",
    "status",
    "alerts",
    "groupLabels",
    "commonLabels",
    "commonAnnotations",
    "externalURL",
    "groupKey",
    "truncatedAlerts",
)

# Control characters to strip: 0x00-0x1F except \n (0x0A) and \t (0x09).
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

# Maximum length of a single sanitized label value.
_MAX_LABEL_VALUE_LENGTH = 500


@dataclass
class NormalizedEvent:
    """Source-agnostic representation of an inbound alert event.

    Produced by :class:`AlertmanagerAdapter` from a single Alertmanager v4
    alert. Downstream consumers (dedup, correlation, admission, dispatch)
    operate on this type rather than raw Alertmanager payloads.
    """

    source: str
    external_fingerprint: str
    alert_name: str
    alert_status: str
    labels: dict
    annotations: dict
    starts_at: datetime
    ends_at: datetime | None
    generator_url: str
    group_key: str
    common_labels: dict
    common_annotations: dict
    received_at: datetime = field(default_factory=lambda: datetime.now(UTC))


def compute_fingerprint(labels: dict) -> str:
    """Compute a deterministic SHA-256 fingerprint from alert labels.

    Label keys are sorted alphabetically and joined as ``key=value`` pairs
    separated by commas, then hashed. The result is stable regardless of the
    insertion order of ``labels``.

    Args:
        labels: Mapping of label names to values.

    Returns:
        Lowercase SHA-256 hex digest of the canonical label string.
    """
    if not labels:
        canonical = ""
    else:
        canonical = ",".join(f"{key}={labels[key]}" for key in sorted(labels))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def sanitize_labels(labels: dict) -> dict:
    """Sanitize label values for safe downstream use (including LLM prompts).

    Applies, per value:
      * Non-UTF8 bytes are replaced with the Unicode replacement char (U+FFFD).
      * Control characters (0x00-0x1F) are stripped, except ``\\n`` and ``\\t``.
      * Values are truncated to 500 characters.
      * Backticks are escaped (``\\``` -> ``\\\\```).

    Keys are coerced to ``str`` and left otherwise untouched.

    Args:
        labels: Mapping of label names to values.

    Returns:
        A new dict with sanitized string values.
    """
    sanitized: dict = {}
    for key, value in labels.items():
        sanitized[str(key)] = _sanitize_value(value)
    return sanitized


def _sanitize_value(value: object) -> str:
    """Sanitize a single label/annotation value into a safe string."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
    else:
        text = str(value)
    # Replace any lone surrogates / undecodable artifacts with U+FFFD.
    text = text.encode("utf-8", errors="replace").decode("utf-8", errors="replace")
    text = _CONTROL_CHARS_RE.sub("", text)
    text = text[:_MAX_LABEL_VALUE_LENGTH]
    text = text.replace("`", "\\`")
    return text


def _parse_timestamp(value: object) -> datetime | None:
    """Parse an Alertmanager RFC3339 timestamp into an aware datetime.

    Returns ``None`` for missing/empty values. Naive datetimes are assumed UTC.
    """
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    # Normalise trailing 'Z' to '+00:00' for fromisoformat on Python 3.11.
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        logger.warning("Alertmanager adapter: unparseable timestamp %r", value)
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


class AlertmanagerAdapter:
    """Adapter for inbound Prometheus Alertmanager v4 webhook payloads."""

    source = "alertmanager"

    @classmethod
    def parse(cls, payload: dict) -> list[NormalizedEvent]:
        """Parse an Alertmanager v4 payload into normalized events.

        Args:
            payload: Decoded JSON body of an Alertmanager v4 webhook.

        Returns:
            List of :class:`NormalizedEvent`, one per alert. Empty list when
            ``alerts`` is empty or the payload is structurally invalid.
        """
        if not isinstance(payload, dict):
            logger.warning("Alertmanager adapter: payload is not a dict, skipping")
            return []

        missing = [key for key in _REQUIRED_TOP_LEVEL_KEYS if key not in payload]
        if missing:
            logger.warning(
                "Alertmanager adapter: payload missing required keys %s, skipping",
                missing,
            )
            return []

        alerts = payload.get("alerts")
        if not isinstance(alerts, list):
            logger.warning("Alertmanager adapter: 'alerts' is not a list, skipping")
            return []

        if PromptInjectionSanitizer.is_suspicious(payload):
            logger.warning("Alertmanager adapter: suspicious payload detected")

        payload = PromptInjectionSanitizer.sanitize(payload)
        alerts = payload.get("alerts", [])

        group_context = {
            "group_key": payload.get("groupKey", ""),
            "common_labels": payload.get("commonLabels") or {},
            "common_annotations": payload.get("commonAnnotations") or {},
        }

        events: list[NormalizedEvent] = []
        for index, alert in enumerate(alerts):
            if not isinstance(alert, dict):
                logger.warning(
                    "Alertmanager adapter: alert at index %d is not a dict, skipping",
                    index,
                )
                continue
            try:
                events.append(cls.normalize(alert, group_context))
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning(
                    "Alertmanager adapter: skipping malformed alert at index %d: %s",
                    index,
                    exc,
                )
                continue
        return events

    @classmethod
    def normalize(cls, alert: dict, group_context: dict) -> NormalizedEvent:
        """Convert a single Alertmanager alert into a :class:`NormalizedEvent`.

        Args:
            alert: One element of the payload's ``alerts`` array.
            group_context: Group-level context with ``group_key``,
                ``common_labels`` and ``common_annotations``.

        Returns:
            A normalized event.

        Raises:
            KeyError: If a required per-alert field is missing.
            TypeError: If a field has an unexpected type.
        """
        status = alert["status"]
        if not isinstance(status, str):
            raise TypeError("alert 'status' must be a string")

        raw_labels = alert.get("labels") or {}
        if not isinstance(raw_labels, dict):
            raise TypeError("alert 'labels' must be a dict")

        raw_annotations = alert.get("annotations") or {}
        if not isinstance(raw_annotations, dict):
            raise TypeError("alert 'annotations' must be a dict")

        labels = sanitize_labels(raw_labels)
        annotations = sanitize_labels(raw_annotations)

        # Prefer Alertmanager's own fingerprint when present; otherwise derive
        # a deterministic one from the (sanitized) labels.
        fingerprint = alert.get("fingerprint")
        if not fingerprint or not isinstance(fingerprint, str):
            fingerprint = compute_fingerprint(labels)

        alert_name = labels.get("alertname", "")

        starts_at = _parse_timestamp(alert.get("startsAt"))
        if starts_at is None:
            starts_at = datetime.now(UTC)

        ends_at = _parse_timestamp(alert.get("endsAt"))

        generator_url = alert.get("generatorURL") or ""
        if not isinstance(generator_url, str):
            generator_url = str(generator_url)

        return NormalizedEvent(
            source=cls.source,
            external_fingerprint=fingerprint,
            alert_name=alert_name,
            alert_status=status,
            labels=labels,
            annotations=annotations,
            starts_at=starts_at,
            ends_at=ends_at,
            generator_url=generator_url,
            group_key=str(group_context.get("group_key", "")),
            common_labels=sanitize_labels(group_context.get("common_labels") or {}),
            common_annotations=sanitize_labels(group_context.get("common_annotations") or {}),
            received_at=datetime.now(UTC),
        )
