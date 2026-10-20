"""gRPC service for Marvin agent communication."""

# pyright: reportMissingImports=false, reportAttributeAccessIssue=false

import asyncio
import json
import logging
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import grpc
from asgiref.sync import sync_to_async
from django.conf import settings
from django.utils import timezone
from grpc import aio

import marvin_pb2 as marvin_pb2
import marvin_pb2_grpc as marvin_pb2_grpc

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s:%(name)s:%(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

_active_servicer: Any = None


def get_servicer() -> Any:
    return _active_servicer


def _set_active_servicer(servicer: Any) -> None:
    global _active_servicer
    _active_servicer = servicer


# Default circuit breaker settings when no CircuitBreakerConfig exists.
DEFAULT_FAILURE_THRESHOLD = 5
DEFAULT_RECOVERY_TIMEOUT_SECONDS = 60.0

# Per-Marvin in-flight command cap.
MAX_IN_FLIGHT_PER_MARVIN = 5

# Bounded command queue size per Marvin stream.
COMMAND_QUEUE_MAXSIZE = 100


class CircuitBreakerOpenError(Exception):
    """Raised when a circuit breaker rejects a command for a (capability, marvin)."""


class ConcurrencyLimitExceededError(Exception):
    """Raised when a Marvin reaches its in-flight command limit."""


class CircuitBreaker:
    """Tracks command failures per (capability, marvin) and opens a breaker.

    Once ``record_failure`` is called ``failure_threshold`` times without an
    intervening success, the breaker opens and ``allow`` returns False for the
    recovery window. After the window elapses, the breaker resets and commands
    are allowed again.
    """

    def __init__(
        self,
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
        recovery_timeout_seconds: float = DEFAULT_RECOVERY_TIMEOUT_SECONDS,
    ):
        self.failure_threshold = failure_threshold
        self.recovery_timeout_seconds = recovery_timeout_seconds
        self._failures: dict[tuple[str, str], int] = {}
        self._opened_until: dict[tuple[str, str], float] = {}

    def allow(self, capability: str, marvin_id: str) -> bool:
        """Return whether a command is allowed for this (capability, marvin)."""
        key = (capability, marvin_id)
        opened_until = self._opened_until.get(key)
        if opened_until is None:
            return True
        if time.monotonic() < opened_until:
            return False
        # Recovery window elapsed: close the breaker.
        self._opened_until.pop(key, None)
        self._failures.pop(key, None)
        return True

    def is_open(self, capability: str, marvin_id: str) -> bool:
        """Return whether the breaker is currently open (no side effects)."""
        key = (capability, marvin_id)
        opened_until = self._opened_until.get(key)
        return opened_until is not None and time.monotonic() < opened_until

    def record_failure(
        self,
        capability: str,
        marvin_id: str,
        threshold: int | None = None,
        recovery_timeout_seconds: float | None = None,
    ) -> None:
        """Record a failure, opening the breaker once the threshold is reached."""
        threshold = self.failure_threshold if threshold is None else threshold
        recovery = (
            self.recovery_timeout_seconds
            if recovery_timeout_seconds is None
            else recovery_timeout_seconds
        )
        key = (capability, marvin_id)
        failures = self._failures.get(key, 0) + 1
        if failures >= threshold:
            self._opened_until[key] = time.monotonic() + recovery
            self._failures.pop(key, None)
        else:
            self._failures[key] = failures

    def record_success(self, capability: str, marvin_id: str) -> None:
        """Reset the failure count after a successful command."""
        self._failures.pop((capability, marvin_id), None)


def _command_capability(command: dict[str, Any]) -> str | None:
    """Return the capability name for a command, or None if not applicable."""
    execute = command.get("execute_capability")
    if not execute:
        return None
    return execute.get("capability_name") or None


class MarvinStream:
    """Represents a bi-directional stream with a Marvin agent."""

    def __init__(self, agent_id: str, servicer: "MarvinServicer | None" = None):
        self.agent_id = agent_id
        self.servicer = servicer
        self.command_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(
            maxsize=COMMAND_QUEUE_MAXSIZE
        )
        self.response_futures: dict[str, asyncio.Future] = {}
        self.connected = False
        self.last_seen = datetime.now(UTC)

    async def send_command(self, command: dict) -> dict:
        """Send a command to the Marvin and wait for response.

        Applies the circuit breaker and per-Marvin concurrency cap (via the
        owning servicer) before dispatching the command.
        """
        command_id = command["command_id"]
        capability_name = _command_capability(command)
        servicer = self.servicer

        if servicer is not None:
            if capability_name is not None:
                servicer.check_circuit_breaker(self.agent_id, capability_name)
            servicer.acquire_command_slot(self.agent_id)

        try:
            future = asyncio.get_event_loop().create_future()
            self.response_futures[command_id] = future
            await self.command_queue.put(command)
            try:
                result = await asyncio.wait_for(future, timeout=300)
            except TimeoutError:
                if servicer is not None and capability_name is not None:
                    await servicer.record_circuit_breaker_failure(self.agent_id, capability_name)
                raise TimeoutError(f"Command {command_id} timed out")
            if servicer is not None and capability_name is not None:
                servicer.record_circuit_breaker_success(self.agent_id, capability_name)
            return result
        finally:
            self.response_futures.pop(command_id, None)
            if servicer is not None:
                servicer.release_command_slot(self.agent_id)

    def handle_response(self, command_id: str, result: dict):
        """Handle a response from the Marvin."""
        future = self.response_futures.get(command_id)
        if future and not future.done():
            future.set_result(result)


class MarvinServicer(marvin_pb2_grpc.MarvinServiceServicer):
    """gRPC servicer for Marvin agent connections."""

    def __init__(self):
        self.marvins: dict[str, MarvinStream] = {}
        self._lock = asyncio.Lock()
        self._embedding_tasks: list[asyncio.Task] = []
        self._embedding_poll_task: asyncio.Task | None = None
        self.circuit_breaker = CircuitBreaker()
        self._in_flight: dict[str, int] = {}
        _set_active_servicer(self)

    async def Connect(self, request_iterator, context):  # noqa: N802
        """Handle bi-directional stream connection from Marvin."""
        agent_id: str | None = None
        stream: MarvinStream | None = None
        message_queue: asyncio.Queue[Any] = asyncio.Queue()

        async def _read_messages() -> None:
            async for message in request_iterator:
                await message_queue.put(("message", message))
            await message_queue.put(("done", None))

        reader_task = asyncio.create_task(_read_messages())

        try:
            while True:
                try:
                    kind, item = await asyncio.wait_for(message_queue.get(), timeout=1.0)
                except TimeoutError:
                    if stream is not None:
                        try:
                            command = await asyncio.wait_for(
                                stream.command_queue.get(), timeout=0.1
                            )
                            yield self._create_control_message(command)
                        except TimeoutError:
                            pass
                    continue

                if kind == "done":
                    break

                message = item
                payload_field = message.WhichOneof("payload")
                if payload_field == "register":
                    agent_id = message.agent_id
                    assert agent_id is not None

                    # Validate registration key
                    registration_key = message.register.registration_key
                    if not registration_key:
                        logger.warning(f"Marvin {agent_id} rejected: missing registration_key")
                        rejected_msg = marvin_pb2.ControlMessage(
                            command_id=str(uuid.uuid4()),
                            registration_rejected=marvin_pb2.RegistrationRejected(
                                reason="Missing registration key",
                                permanent=True,
                            ),
                        )
                        yield rejected_msg
                        break

                    from asgiref.sync import sync_to_async

                    from apps.marvins.auth import validate_marvin_registration_key

                    org = await sync_to_async(validate_marvin_registration_key)(registration_key)
                    if org is None:
                        logger.warning(f"Marvin {agent_id} rejected: invalid registration_key")
                        rejected_msg = marvin_pb2.ControlMessage(
                            command_id=str(uuid.uuid4()),
                            registration_rejected=marvin_pb2.RegistrationRejected(
                                reason="Invalid or inactive registration key",
                                permanent=True,
                            ),
                        )
                        yield rejected_msg
                        break

                    stream = MarvinStream(agent_id, self)
                    async with self._lock:
                        self.marvins[agent_id] = stream
                    stream.connected = True
                    stream.last_seen = datetime.now(UTC)
                    logger.info(f"Marvin {agent_id} connected")

                    await self._handle_register(agent_id, message.register, org)

                    # Assemble effective config for the agent
                    from apps.marvins.config_assembly import assemble_marvin_config
                    from apps.marvins.models import Marvin

                    marvin_instance = await sync_to_async(Marvin.objects.get)(client_id=agent_id)
                    effective_config = await sync_to_async(assemble_marvin_config)(marvin_instance)

                    # Build UpdateConfig proto
                    from google.protobuf import struct_pb2

                    cap_manifests = []
                    for cap_name, cap_cfg in effective_config.get("capabilities", {}).items():
                        struct = struct_pb2.Struct()
                        struct.update(cap_cfg)
                        cap_manifests.append(
                            marvin_pb2.CapabilityManifest(
                                name=cap_name,
                                config=struct,
                            )
                        )

                    config_version = int(datetime.now(UTC).timestamp())

                    registered_msg = marvin_pb2.ControlMessage(
                        command_id=str(uuid.uuid4()),
                        registered=marvin_pb2.Registered(
                            heartbeat_interval_seconds=10,
                            server_timestamp_unix_ms=int(datetime.now(UTC).timestamp() * 1000),
                            effective_config=marvin_pb2.UpdateConfig(
                                capabilities=cap_manifests,
                                config_version=config_version,
                            ),
                        ),
                    )
                    yield registered_msg

                elif payload_field == "heartbeat":
                    if stream is not None:
                        stream.last_seen = datetime.now(UTC)
                    if agent_id is not None:
                        await self._update_marvin_status(agent_id, "online")
                        if message.heartbeat.HasField("resources"):
                            await self._persist_health_metrics(
                                agent_id, message.heartbeat.resources
                            )

                    hb_ack = marvin_pb2.ControlMessage(
                        command_id=str(uuid.uuid4()),
                        heartbeat_ack=marvin_pb2.HeartbeatAck(
                            received_timestamp_unix_ms=int(datetime.now(UTC).timestamp() * 1000),
                        ),
                    )
                    yield hb_ack

                elif payload_field == "capability_result":
                    if stream is not None and agent_id is not None:
                        await self._handle_capability_result(
                            agent_id, message.capability_result, stream
                        )

                elif payload_field == "log_event":
                    if agent_id is not None:
                        await self._handle_log_event(agent_id, message.log_event)

                elif payload_field == "status_update":
                    if agent_id is not None:
                        await self._handle_status_update(agent_id, message.status_update)

                # Send any pending commands
                if stream is not None:
                    try:
                        command = await asyncio.wait_for(stream.command_queue.get(), timeout=1.0)
                        yield self._create_control_message(command)
                    except TimeoutError:
                        pass

        except Exception as e:
            logger.error(f"Error in Marvin connection: {e}")
        finally:
            reader_task.cancel()
            try:
                await reader_task
            except asyncio.CancelledError:
                pass
            if agent_id and stream:
                stream.connected = False
                async with self._lock:
                    # Only remove if this handler's stream is still the current one.
                    # Prevents a race where a reconnect's new stream gets deleted
                    # by an old handler's finally block.
                    if self.marvins.get(agent_id) is stream:
                        self.marvins.pop(agent_id, None)
                await self._update_marvin_status(agent_id, "offline")
                logger.info(f"Marvin {agent_id} disconnected")

    async def _handle_register(self, agent_id: str, register: marvin_pb2.Register, organization):
        """Handle registration from Marvin, updating DB with metadata and capabilities."""
        capability_ids = await sync_to_async(self._update_marvin_registration)(
            agent_id, register, organization
        )
        await self._trigger_embedding_generation(capability_ids)

    async def _trigger_embedding_generation(self, capability_ids: list[str]) -> None:
        """Schedule background embedding generation for created/updated capabilities."""
        if not capability_ids:
            return
        self._cleanup_embedding_tasks()
        for capability_id in capability_ids:
            self._embedding_tasks.append(
                asyncio.create_task(self._run_embedding_activity(capability_id))
            )

    def start_embedding_poll(self):
        """Start the periodic background task that polls for stale embeddings."""
        if self._embedding_poll_task is None or self._embedding_poll_task.done():
            self._embedding_poll_task = asyncio.create_task(self._poll_re_embedding_needs())

    async def _poll_re_embedding_needs(self):
        """Periodically check for capabilities that need re-embedding."""
        while True:
            try:
                from asgiref.sync import sync_to_async

                ids = await sync_to_async(_find_re_embedding_ids)()
                if ids:
                    logger.info("Re-embedding %s stale capability(s)", len(ids))
                    await self._trigger_embedding_generation(ids)
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Embedding poll task error")
                await asyncio.sleep(60)

    def _cleanup_embedding_tasks(self) -> None:
        """Remove completed tasks to prevent unbounded memory growth."""
        self._embedding_tasks = [t for t in self._embedding_tasks if not t.done()]

    async def _run_embedding_activity(self, capability_id: str):
        try:
            from services.temporal_workers.activities import generate_capability_embedding

            await generate_capability_embedding(capability_id)
        except Exception as exc:
            logger.warning("Embedding generation failed for capability %s: %s", capability_id, exc)

    def _update_marvin_registration(
        self, agent_id: str, register: marvin_pb2.Register, organization
    ) -> list[str]:
        import os

        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "vogon.settings")

        import django

        django.setup()

        from apps.marvins.models import Capability, Marvin

        host = register.host

        try:
            marvin = Marvin.objects.get(client_id=agent_id)
            if marvin.organization != organization:
                logger.warning(
                    f"Marvin {agent_id} belongs to {marvin.organization_id}; "
                    f"ignoring registration for {organization.id}"
                )
                return []
        except Marvin.DoesNotExist:
            marvin = Marvin.objects.create(
                organization=organization,
                name=host.hostname or agent_id,
                client_id=agent_id,
            )

        marvin.status = Marvin.Status.ONLINE
        marvin.last_seen = timezone.now()
        marvin.agent_version = register.agent_version
        marvin.labels = list(register.labels)

        # Host metadata
        marvin.hostname = host.hostname
        marvin.local_ip = host.local_ip
        marvin.provider = host.provider
        marvin.region = host.region
        marvin.availability_zone = host.availability_zone
        marvin.vm_id = host.vm_id
        marvin.os = host.os
        marvin.os_version = host.os_version
        marvin.arch = host.arch

        # Capability manifest: update configs and sync many-to-many relation
        capability_configs: dict[str, dict] = {}
        enabled_capabilities: list[str] = []
        capability_ids: list[str] = []
        for cap_manifest in register.capabilities:
            capability_configs[cap_manifest.name] = dict(cap_manifest.config)
            if cap_manifest.enabled:
                enabled_capabilities.append(cap_manifest.name)
                # Ensure Capability record exists for this organization
                capability, created = Capability.objects.get_or_create(
                    organization=marvin.organization,
                    name=cap_manifest.name,
                    defaults={
                        "description": cap_manifest.description,
                        "json_schema": (
                            json.loads(cap_manifest.parameters_json_schema)
                            if cap_manifest.parameters_json_schema
                            else {}
                        ),
                        "use_cases": list(cap_manifest.use_cases),
                        "aliases": list(cap_manifest.aliases),
                        "tags": list(cap_manifest.tags),
                    },
                )
                capability_ids.append(str(capability.id))
                updated_fields = []
                if not created:
                    # Update description and schema if changed
                    if capability.description != cap_manifest.description:
                        capability.description = cap_manifest.description
                        updated_fields.append("description")
                    new_schema = (
                        json.loads(cap_manifest.parameters_json_schema)
                        if cap_manifest.parameters_json_schema
                        else {}
                    )
                    if capability.json_schema != new_schema:
                        capability.json_schema = new_schema
                        updated_fields.append("json_schema")
                    new_use_cases = list(cap_manifest.use_cases)
                    if capability.use_cases != new_use_cases:
                        capability.use_cases = new_use_cases
                        updated_fields.append("use_cases")
                    new_aliases = list(cap_manifest.aliases)
                    if capability.aliases != new_aliases:
                        capability.aliases = new_aliases
                        updated_fields.append("aliases")
                    new_tags = list(cap_manifest.tags)
                    if capability.tags != new_tags:
                        capability.tags = new_tags
                        updated_fields.append("tags")
                    if updated_fields:
                        capability.save(update_fields=updated_fields)
                if not created and not capability.enabled:
                    capability.enabled = True
                    capability.save(update_fields=["enabled"])

        marvin.capability_configs = capability_configs
        marvin.save()

        # Sync many-to-many: keep only enabled capabilities
        marvin.capabilities.set(
            Capability.objects.filter(
                organization=marvin.organization,
                name__in=enabled_capabilities,
            )
        )

        return capability_ids

    async def _handle_capability_result(
        self, agent_id: str, result: marvin_pb2.CapabilityResult, stream: MarvinStream
    ):
        """Handle capability execution result from Marvin."""
        stream.handle_response(
            result.command_id,
            {
                "command_id": result.command_id,
                "session_id": result.session_id,
                "thread_id": result.thread_id,
                "capability_name": result.capability_name,
                "success": result.success,
                "result": json.loads(result.result_json) if result.result_json else None,
                "error": result.error_message if not result.success else None,
                "execution_duration_ms": (
                    int(
                        result.execution_duration.seconds * 1000
                        + result.execution_duration.nanos // 1_000_000
                    )
                    if result.HasField("execution_duration")
                    else None
                ),
                "result_index": result.result_index,
            },
        )

    async def _handle_log_event(self, agent_id: str, log_event: marvin_pb2.LogEvent):
        """Handle log event forwarded from Marvin."""
        level_name = marvin_pb2.LogEvent.Level.Name(log_event.level)
        logger.info(f"[Marvin {agent_id}] {level_name}: {log_event.message}")

    async def _handle_status_update(self, agent_id: str, status_update: marvin_pb2.StatusUpdate):
        """Handle status update from Marvin."""
        severity_name = marvin_pb2.StatusUpdate.Severity.Name(status_update.severity)
        msg = f"[Marvin {agent_id}] StatusUpdate ({severity_name}): {status_update.message}"
        if status_update.capability_name:
            msg += f" (capability: {status_update.capability_name})"
        logger.warning(msg)

    async def _update_marvin_status(self, agent_id: str, status: str):
        """Update Marvin status in the database."""
        await sync_to_async(self._update_marvin_status_in_database)(agent_id, status)

    def _update_marvin_status_in_database(self, agent_id: str, status: str):
        import os

        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "vogon.settings")

        import django

        django.setup()

        from django.utils import timezone

        from apps.marvins.models import Marvin

        try:
            marvin = Marvin.objects.get(client_id=agent_id)
            marvin.status = status
            marvin.last_seen = timezone.now()
            marvin.save()
        except Marvin.DoesNotExist:
            logger.warning(f"Marvin with agent_id {agent_id} not found in database")

    def check_circuit_breaker(self, agent_id: str, capability_name: str) -> None:
        """Raise CircuitBreakerOpenError if the breaker is open for (capability, marvin)."""
        if not self.circuit_breaker.allow(capability_name, agent_id):
            raise CircuitBreakerOpenError(
                f"Circuit breaker open for capability '{capability_name}' on Marvin {agent_id}"
            )

    async def record_circuit_breaker_failure(self, agent_id: str, capability_name: str) -> None:
        """Record a command failure and open the breaker once the threshold is reached."""
        threshold, recovery = await self._get_breaker_config(capability_name)
        self.circuit_breaker.record_failure(
            capability_name,
            agent_id,
            threshold=threshold,
            recovery_timeout_seconds=recovery,
        )

    def record_circuit_breaker_success(self, agent_id: str, capability_name: str) -> None:
        """Reset the breaker failure count after a successful command."""
        self.circuit_breaker.record_success(capability_name, agent_id)

    def acquire_command_slot(self, agent_id: str) -> None:
        """Reserve an in-flight slot, raising ConcurrencyLimitExceededError at the cap."""
        count = self._in_flight.get(agent_id, 0)
        if count >= MAX_IN_FLIGHT_PER_MARVIN:
            raise ConcurrencyLimitExceededError(
                f"Marvin {agent_id} has {count} in-flight commands (max {MAX_IN_FLIGHT_PER_MARVIN})"
            )
        self._in_flight[agent_id] = count + 1

    def release_command_slot(self, agent_id: str) -> None:
        """Release a reserved in-flight slot."""
        count = self._in_flight.get(agent_id, 0) - 1
        if count <= 0:
            self._in_flight.pop(agent_id, None)
        else:
            self._in_flight[agent_id] = count

    async def _get_breaker_config(self, capability_name: str) -> tuple[int, float]:
        """Return (failure_threshold, recovery_timeout_seconds) for a capability."""
        config = await sync_to_async(self._get_breaker_config_in_database)(capability_name)
        if config is None:
            return DEFAULT_FAILURE_THRESHOLD, DEFAULT_RECOVERY_TIMEOUT_SECONDS
        return config.failure_threshold, float(config.recovery_timeout_seconds)

    def _get_breaker_config_in_database(self, capability_name: str):
        import os

        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "vogon.settings")

        import django

        django.setup()

        from apps.marvins.models import CircuitBreakerConfig

        try:
            return CircuitBreakerConfig.objects.filter(
                capability_name=capability_name, enabled=True
            ).first()
        except Exception:
            logger.warning(
                "Failed to load circuit breaker config for %s", capability_name, exc_info=True
            )
            return None

    async def _persist_health_metrics(self, agent_id: str, resources: Any) -> None:
        """Persist heartbeat ResourceMetrics to MarvinHealthMetrics."""
        await sync_to_async(self._persist_health_metrics_in_database)(
            agent_id,
            resources.cpu_usage,
            resources.memory_usage,
            resources.load_average,
            resources.disk_usage,
        )

    def _persist_health_metrics_in_database(
        self,
        agent_id: str,
        cpu_usage: float,
        memory_usage: float,
        load_average: float,
        disk_usage: float,
    ) -> None:
        import os

        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "vogon.settings")

        import django

        django.setup()

        from apps.marvins.models import Marvin, MarvinHealthMetrics

        try:
            marvin = Marvin.objects.get(client_id=agent_id)
        except Marvin.DoesNotExist:
            logger.warning("Marvin with agent_id %s not found for health metrics", agent_id)
            return

        MarvinHealthMetrics.objects.update_or_create(
            marvin=marvin,
            defaults={
                "cpu_usage": cpu_usage * 100.0,
                "memory_usage": memory_usage * 100.0,
                "load_average": load_average,
                "disk_usage": disk_usage,
            },
        )

    def get_marvin_stream(self, marvin_id: str) -> MarvinStream | None:
        """Get the stream for a specific Marvin."""
        return self.marvins.get(marvin_id)

    def _create_control_message(self, command: dict) -> marvin_pb2.ControlMessage:
        if "execute_capability" in command:
            payload = command["execute_capability"]
            return marvin_pb2.ControlMessage(
                command_id=command["command_id"],
                execute_capability=marvin_pb2.ExecuteCapability(
                    session_id=payload.get("session_id", ""),
                    thread_id=payload.get("thread_id", ""),
                    capability_name=payload.get("capability_name", ""),
                    parameters_json=payload.get("parameters_json", ""),
                    deadline_unix_ms=payload.get("deadline_unix_ms", 0),
                    target_set_id=payload.get("target_set_id", ""),
                    execution_mode=payload.get("execution_mode", "single"),
                    result_index=payload.get("result_index", 0),
                ),
            )
        if "update_config" in command:
            payload = command["update_config"]
            return marvin_pb2.ControlMessage(
                command_id=command["command_id"],
                update_config=marvin_pb2.UpdateConfig(
                    capabilities=payload.get("capabilities", []),
                    config_version=payload.get("config_version", 0),
                ),
            )
        if "disconnect" in command:
            payload = command["disconnect"]
            return marvin_pb2.ControlMessage(
                command_id=command["command_id"],
                disconnect=marvin_pb2.Disconnect(
                    reason=payload.get("reason", ""),
                    allow_graceful=payload.get("allow_graceful", True),
                ),
            )
        raise ValueError(f"Unknown command type: {command}")


def _find_re_embedding_ids() -> list[str]:
    """Return capability IDs that need re-embedding."""
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "vogon.settings")
    import django

    django.setup()
    from apps.marvins.models import Capability

    return [str(cap.id) for cap in Capability.objects.filter(needs_re_embedding=True)[:50]]


async def serve_grpc(servicer: MarvinServicer, port: int = 50051):
    """Start the gRPC server."""
    server_options = [
        ("grpc.keepalive_time_ms", 20000),
        ("grpc.keepalive_timeout_ms", 5000),
        ("grpc.keepalive_permit_without_calls", 1),
        ("grpc.http2.max_pings_without_data", 0),
        ("grpc.http2.min_ping_interval_without_data_ms", 10000),
        ("grpc.max_connection_age_ms", 30 * 60 * 1000),
        ("grpc.max_connection_age_grace_ms", 30000),
    ]
    server = aio.server(options=server_options)
    marvin_pb2_grpc.add_MarvinServiceServicer_to_server(servicer, server)

    if getattr(settings, "GRPC_TLS_ENABLED", False):
        cert_file = getattr(settings, "GRPC_TLS_CERT_FILE", "")
        key_file = getattr(settings, "GRPC_TLS_KEY_FILE", "")
        if not cert_file or not key_file:
            raise ValueError(
                "GRPC_TLS_CERT_FILE and GRPC_TLS_KEY_FILE must be set when TLS is enabled"
            )
        with open(cert_file, "rb") as f:
            server_cert = f.read()
        with open(key_file, "rb") as f:
            server_key = f.read()

        ca_file = getattr(settings, "GRPC_TLS_CA_FILE", "")
        if ca_file:
            with open(ca_file, "rb") as f:
                ca_cert = f.read()
            credentials = grpc.ssl_server_credentials(
                private_key_certificate_chain_pairs=((server_key, server_cert),),
                root_certificates=ca_cert,
                require_client_auth=True,
            )
            logger.info("gRPC server using mTLS on port %s", port)
        else:
            credentials = grpc.ssl_server_credentials(((server_key, server_cert),))
            logger.info("gRPC server using TLS on port %s", port)
        server.add_secure_port(f"[::]:{port}", credentials)
    else:
        server.add_insecure_port(f"[::]:{port}")
        logger.info("gRPC server started on port %s", port)

    await server.start()
    await server.wait_for_termination()
