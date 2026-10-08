"""Inbound event receivers for Checks.

This package contains adapters that normalize external event payloads
(initially Prometheus Alertmanager v4 webhooks) into a generic
:class:`~services.checks.receivers.alertmanager.NormalizedEvent` representation.
"""
