"""Enums for Check health states and finding severities."""

from enum import StrEnum

from django.db import models


class HealthState(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    CRITICAL = "critical"
    UNKNOWN = "unknown"


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class AdmissionMode(models.TextChoices):
    ALWAYS = "always", "Always"
    AI_GATED = "ai_gated", "AI Gated"


class ReceiverDisposition(models.TextChoices):
    RECEIVED = "received", "Received"
    NORMALIZED = "normalized", "Normalized"
    DEDUPLICATED = "deduplicated", "Deduplicated"
    CORRELATED = "correlated", "Correlated"
    ADMISSION_DECIDED = "admission_decided", "Admission Decided"
    STARTED = "started", "Started"
    ATTACHED = "attached", "Attached"
    SUPPRESSED = "suppressed", "Suppressed"
    SUPPRESS_LOW_VALUE = "suppress_low_value", "Suppress Low Value"
    FAILED = "failed", "Failed"
    RESOLVED = "resolved", "Resolved"


class AdmissionDecision(models.TextChoices):
    START = "start", "Start"
    SUPPRESS_DUPLICATE = "suppress_duplicate", "Suppress Duplicate"
    SUPPRESS_CORRELATED = "suppress_correlated", "Suppress Correlated"
    SUPPRESS_LOW_VALUE = "suppress_low_value", "Suppress Low Value"
    DEFER = "defer", "Defer"
