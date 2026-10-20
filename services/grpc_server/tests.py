import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from django.test import override_settings

import marvin_pb2
from apps.core.models import Organization
from apps.marvins.models import CircuitBreakerConfig, Marvin, MarvinHealthMetrics
from services.grpc_server.servicer import (
    CircuitBreaker,
    CircuitBreakerOpenError,
    ConcurrencyLimitExceededError,
    MarvinServicer,
    MarvinStream,
    serve_grpc,
)


@pytest.fixture
def mock_servicer():
    return MagicMock(spec=MarvinServicer)


@patch("services.grpc_server.servicer.aio.server")
@patch("services.grpc_server.servicer.marvin_pb2_grpc.add_MarvinServiceServicer_to_server")
@pytest.mark.asyncio
async def test_serve_grpc_uses_insecure_port_when_tls_disabled(
    mock_add_servicer, mock_server, mock_servicer
):
    mock_instance = MagicMock()
    mock_instance.add_insecure_port = MagicMock()
    mock_instance.add_secure_port = MagicMock()
    mock_instance.start = AsyncMock()
    mock_instance.wait_for_termination = AsyncMock()
    mock_server.return_value = mock_instance

    await asyncio.wait_for(serve_grpc(mock_servicer, port=50051), timeout=1.0)

    mock_instance.add_insecure_port.assert_called_once_with("[::]:50051")
    mock_instance.add_secure_port.assert_not_called()
    mock_instance.start.assert_awaited_once()


@patch("services.grpc_server.servicer.aio.server")
@patch("services.grpc_server.servicer.marvin_pb2_grpc.add_MarvinServiceServicer_to_server")
@patch("services.grpc_server.servicer.grpc.ssl_server_credentials")
@pytest.mark.asyncio
async def test_serve_grpc_uses_secure_port_when_tls_enabled(
    mock_ssl_creds, mock_add_servicer, mock_server, mock_servicer, tmp_path
):
    cert_file = tmp_path / "server.crt"
    key_file = tmp_path / "server.key"
    cert_file.write_bytes(b"cert")
    key_file.write_bytes(b"key")

    mock_instance = MagicMock()
    mock_instance.add_insecure_port = MagicMock()
    mock_instance.add_secure_port = MagicMock()
    mock_instance.start = AsyncMock()
    mock_instance.wait_for_termination = AsyncMock()
    mock_server.return_value = mock_instance

    with override_settings(
        GRPC_TLS_ENABLED=True,
        GRPC_TLS_CERT_FILE=str(cert_file),
        GRPC_TLS_KEY_FILE=str(key_file),
        GRPC_TLS_CA_FILE="",
    ):
        await asyncio.wait_for(serve_grpc(mock_servicer, port=50051), timeout=1.0)

    mock_instance.add_insecure_port.assert_not_called()
    mock_instance.add_secure_port.assert_called_once_with("[::]:50051", mock_ssl_creds.return_value)
    mock_ssl_creds.assert_called_once_with(((b"key", b"cert"),))
    mock_instance.start.assert_awaited_once()


@patch("services.grpc_server.servicer.aio.server")
@patch("services.grpc_server.servicer.marvin_pb2_grpc.add_MarvinServiceServicer_to_server")
@patch("services.grpc_server.servicer.grpc.ssl_server_credentials")
@pytest.mark.asyncio
async def test_serve_grpc_uses_mutual_tls_when_ca_file_set(
    mock_ssl_creds, mock_add_servicer, mock_server, mock_servicer, tmp_path
):
    cert_file = tmp_path / "server.crt"
    key_file = tmp_path / "server.key"
    ca_file = tmp_path / "ca.crt"
    cert_file.write_bytes(b"cert")
    key_file.write_bytes(b"key")
    ca_file.write_bytes(b"ca")

    mock_instance = MagicMock()
    mock_instance.add_insecure_port = MagicMock()
    mock_instance.add_secure_port = MagicMock()
    mock_instance.start = AsyncMock()
    mock_instance.wait_for_termination = AsyncMock()
    mock_server.return_value = mock_instance

    with override_settings(
        GRPC_TLS_ENABLED=True,
        GRPC_TLS_CERT_FILE=str(cert_file),
        GRPC_TLS_KEY_FILE=str(key_file),
        GRPC_TLS_CA_FILE=str(ca_file),
    ):
        await asyncio.wait_for(serve_grpc(mock_servicer, port=50051), timeout=1.0)

    mock_instance.add_insecure_port.assert_not_called()
    mock_instance.add_secure_port.assert_called_once_with("[::]:50051", mock_ssl_creds.return_value)
    mock_ssl_creds.assert_called_once_with(
        private_key_certificate_chain_pairs=((b"key", b"cert"),),
        root_certificates=b"ca",
        require_client_auth=True,
    )
    mock_instance.start.assert_awaited_once()


@pytest.mark.asyncio
@override_settings(GRPC_TLS_ENABLED=True, GRPC_TLS_CERT_FILE="", GRPC_TLS_KEY_FILE="")
async def test_serve_grpc_raises_when_tls_enabled_but_files_missing(mock_servicer):
    with pytest.raises(ValueError, match="GRPC_TLS_CERT_FILE and GRPC_TLS_KEY_FILE must be set"):
        await serve_grpc(mock_servicer, port=50051)


class TestCircuitBreaker:
    def test_opens_after_threshold_failures(self):
        breaker = CircuitBreaker(failure_threshold=3, recovery_timeout_seconds=60.0)

        assert breaker.allow("kinit", "agent-1")
        breaker.record_failure("kinit", "agent-1")
        breaker.record_failure("kinit", "agent-1")
        assert breaker.allow("kinit", "agent-1")  # two failures, still closed

        breaker.record_failure("kinit", "agent-1")  # third failure opens the breaker
        assert breaker.is_open("kinit", "agent-1")
        assert not breaker.allow("kinit", "agent-1")

    def test_closes_after_recovery_timeout(self):
        breaker = CircuitBreaker(failure_threshold=2, recovery_timeout_seconds=60.0)
        breaker.record_failure("kinit", "agent-1")
        breaker.record_failure("kinit", "agent-1")
        assert breaker.is_open("kinit", "agent-1")

        # Simulate the recovery window elapsing.
        breaker._opened_until[("kinit", "agent-1")] = time.monotonic() - 1.0
        assert breaker.allow("kinit", "agent-1")
        assert breaker.allow("kinit", "agent-1")

    def test_success_resets_failure_count(self):
        breaker = CircuitBreaker(failure_threshold=3, recovery_timeout_seconds=60.0)
        breaker.record_failure("kinit", "agent-1")
        breaker.record_failure("kinit", "agent-1")
        breaker.record_success("kinit", "agent-1")
        breaker.record_failure("kinit", "agent-1")
        breaker.record_failure("kinit", "agent-1")
        assert not breaker.is_open("kinit", "agent-1")

    def test_tracks_failures_per_capability_and_marvin(self):
        breaker = CircuitBreaker(failure_threshold=2, recovery_timeout_seconds=60.0)
        breaker.record_failure("kinit", "agent-1")
        breaker.record_failure("kinit", "agent-1")
        assert breaker.is_open("kinit", "agent-1")
        assert breaker.allow("ping", "agent-1")  # different capability unaffected
        assert breaker.allow("kinit", "agent-2")  # different marvin unaffected


class TestConcurrencyCap:
    def test_rejects_beyond_five_in_flight(self):
        servicer = MarvinServicer()
        for _ in range(5):
            servicer.acquire_command_slot("agent-1")

        with pytest.raises(ConcurrencyLimitExceededError):
            servicer.acquire_command_slot("agent-1")

    def test_slot_released_allows_new_command(self):
        servicer = MarvinServicer()
        for _ in range(5):
            servicer.acquire_command_slot("agent-1")

        servicer.release_command_slot("agent-1")
        servicer.acquire_command_slot("agent-1")  # allowed after release

    def test_release_never_goes_below_zero(self):
        servicer = MarvinServicer()
        servicer.release_command_slot("agent-1")
        servicer.release_command_slot("agent-1")
        assert servicer._in_flight.get("agent-1", 0) == 0


class TestCircuitBreakerIntegration:
    def test_send_command_rejected_when_breaker_open(self):
        servicer = MarvinServicer()
        servicer.circuit_breaker = CircuitBreaker(
            failure_threshold=1, recovery_timeout_seconds=60.0
        )
        servicer.circuit_breaker.record_failure("kinit", "agent-1")
        stream = MarvinStream("agent-1", servicer)

        with pytest.raises(CircuitBreakerOpenError):
            asyncio.run(
                stream.send_command(
                    {"command_id": "cmd-1", "execute_capability": {"capability_name": "kinit"}}
                )
            )

    def test_check_circuit_breaker_raises_when_open(self):
        servicer = MarvinServicer()
        servicer.circuit_breaker.record_failure(
            "kinit", "agent-1", threshold=1, recovery_timeout_seconds=60.0
        )

        with pytest.raises(CircuitBreakerOpenError):
            servicer.check_circuit_breaker("agent-1", "kinit")

    def test_send_command_releases_slot_after_completion(self):
        servicer = MarvinServicer()
        stream = MarvinStream("agent-1", servicer)

        async def driver():
            task = asyncio.create_task(
                stream.send_command(
                    {"command_id": "cmd-1", "execute_capability": {"capability_name": "kinit"}}
                )
            )
            await asyncio.sleep(0)
            stream.handle_response("cmd-1", {"result": "ok"})
            return await task

        result = asyncio.run(driver())
        assert result == {"result": "ok"}
        assert servicer._in_flight.get("agent-1", 0) == 0

    @pytest.mark.django_db(transaction=True)
    def test_record_failure_uses_configured_threshold(self):
        Organization.objects.create(name="CB", slug="cb")
        CircuitBreakerConfig.objects.create(
            capability_name="kinit", failure_threshold=2, recovery_timeout_seconds=60
        )
        servicer = MarvinServicer()

        asyncio.run(servicer.record_circuit_breaker_failure("agent-1", "kinit"))
        assert not servicer.circuit_breaker.is_open("kinit", "agent-1")
        asyncio.run(servicer.record_circuit_breaker_failure("agent-1", "kinit"))
        assert servicer.circuit_breaker.is_open("kinit", "agent-1")


class TestHeartbeatMetrics:
    @pytest.mark.django_db(transaction=True)
    def test_heartbeat_persists_resource_metrics(self):
        organization = Organization.objects.create(name="HB", slug="hb")
        marvin = Marvin.objects.create(
            organization=organization,
            name="marvin-1",
            client_id="agent-1",
            status="online",
        )
        servicer = MarvinServicer()

        resources = marvin_pb2.ResourceMetrics(
            cpu_usage=0.25, memory_usage=0.5, load_average=1.5, disk_usage=0.75
        )
        asyncio.run(servicer._persist_health_metrics("agent-1", resources))

        metrics = MarvinHealthMetrics.objects.get(marvin=marvin)
        assert metrics.cpu_usage == 25.0  # fractions (0-1) stored as percentages (0-100)
        assert metrics.memory_usage == 50.0
        assert metrics.load_average == 1.5
        assert metrics.disk_usage == 0.75

    @pytest.mark.django_db(transaction=True)
    def test_heartbeat_updates_existing_snapshot(self):
        organization = Organization.objects.create(name="HB2", slug="hb2")
        marvin = Marvin.objects.create(
            organization=organization,
            name="marvin-2",
            client_id="agent-2",
            status="online",
        )
        servicer = MarvinServicer()

        first = marvin_pb2.ResourceMetrics(
            cpu_usage=0.1, memory_usage=0.2, load_average=0.5, disk_usage=0.3
        )
        asyncio.run(servicer._persist_health_metrics("agent-2", first))

        second = marvin_pb2.ResourceMetrics(
            cpu_usage=0.9, memory_usage=0.8, load_average=4.0, disk_usage=0.9
        )
        asyncio.run(servicer._persist_health_metrics("agent-2", second))

        assert MarvinHealthMetrics.objects.filter(marvin=marvin).count() == 1
        metrics = MarvinHealthMetrics.objects.get(marvin=marvin)
        assert metrics.cpu_usage == 90.0
        assert metrics.load_average == 4.0
