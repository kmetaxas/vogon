"""Django signals for Marvin online config and live push."""

import asyncio
import logging
from typing import Any

from django.db.models.signals import m2m_changed, post_save
from django.dispatch import receiver

from apps.marvins.models import Marvin, MarvinConfig, MarvinResourceAttachment

logger = logging.getLogger(__name__)


@receiver(post_save, sender=Marvin)
def create_marvin_config_on_marvin_create(
    sender: type[Marvin],
    instance: Marvin,
    created: bool,
    **kwargs: Any,
) -> None:
    """Auto-create MarvinConfig when a Marvin is created."""
    if created:
        MarvinConfig.objects.get_or_create(marvin=instance)


@receiver(post_save, sender=MarvinConfig)
def push_config_on_marvin_config_change(
    sender: type[MarvinConfig],
    instance: MarvinConfig,
    **kwargs: Any,
) -> None:
    """Push UpdateConfig to the connected Marvin when config changes."""
    _push_config_to_marvin(instance.marvin)


@receiver(post_save, sender=MarvinResourceAttachment)
def push_config_on_attachment_create(
    sender: type[MarvinResourceAttachment],
    instance: MarvinResourceAttachment,
    created: bool,
    **kwargs: Any,
) -> None:
    """Push UpdateConfig when a resource is attached to a Marvin."""
    if created:
        _push_config_to_marvin(instance.marvin)


@receiver(m2m_changed, sender=Marvin.attached_resources.through)
def push_config_on_resource_m2m_change(
    sender: type[Any],
    instance: Marvin,
    action: str,
    **kwargs: Any,
) -> None:
    """Push UpdateConfig when Marvin.attached_resources changes via M2M."""
    if action in ("post_add", "post_remove", "post_clear"):
        _push_config_to_marvin(instance)


def _push_config_to_marvin(marvin: Marvin) -> None:
    """Enqueue an update_config command to the Marvin's gRPC stream if online."""
    from apps.marvins.config_assembly import assemble_marvin_config
    from services.grpc_server.servicer import get_servicer

    servicer = get_servicer()
    if servicer is None:
        logger.debug("No gRPC servicer available; skipping config push for %s", marvin.client_id)
        return

    stream = servicer.marvins.get(marvin.client_id)
    if stream is None or not stream.connected:
        logger.debug(
            "Marvin %s is not connected; config change will be applied on next connect",
            marvin.client_id,
        )
        return

    try:
        effective = assemble_marvin_config(marvin)
        command = {
            "command_id": f"config-push-{marvin.client_id}",
            "update_config": effective,
        }
        # Use call_soon_threadsafe because signals may run in a different thread
        loop = asyncio.get_event_loop()
        loop.call_soon_threadsafe(lambda: asyncio.create_task(stream.command_queue.put(command)))
        logger.info("Pushed config update to Marvin %s", marvin.client_id)
    except Exception:
        logger.exception("Failed to push config to Marvin %s", marvin.client_id)
