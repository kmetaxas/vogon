import asyncio
import time

import grpc
from django.core.management.base import BaseCommand

import marvin_pb2
import marvin_pb2_grpc


class Command(BaseCommand):
    help = "Test Marvin gRPC connection manually"

    def add_arguments(self, parser):
        parser.add_argument(
            "--registration-key",
            required=True,
            help="Organization registration key for the Marvin",
        )
        parser.add_argument(
            "--agent-id",
            default="test-agent-1",
            help="Unique agent ID (default: test-agent-1)",
        )
        parser.add_argument(
            "--grpc-host",
            default="grpc",
            help="gRPC server host (default: grpc - use 'localhost' from host)",
        )
        parser.add_argument(
            "--grpc-port",
            type=int,
            default=50051,
            help="gRPC server port (default: 50051)",
        )
        parser.add_argument(
            "--capability",
            action="append",
            default=["test.capability"],
            help="Capabilities to register (can be used multiple times)",
        )
        parser.add_argument(
            "--label",
            action="append",
            default=["env:production"],
            help="Labels to register with (can be used multiple times)",
        )
        parser.add_argument(
            "--heartbeat-interval",
            type=int,
            default=10,
            help="Seconds between heartbeats (default: 10)",
        )
        parser.add_argument(
            "--duration",
            type=int,
            default=60,
            help="How long to stay connected in seconds (default: 60)",
        )

    async def send_messages(self, args, stop_event: asyncio.Event, pending_heartbeats: dict):
        capabilities = [
            marvin_pb2.CapabilityManifest(  # type: ignore[attr-defined]
                name=cap,
                description=f"Test capability {cap}",
                enabled=True,
                parameters_json_schema="{}",
                config={},
            )
            for cap in args.capability
        ]

        registration = marvin_pb2.Register(  # type: ignore[attr-defined]
            agent_version="test-client/v1.0.0",
            host=marvin_pb2.HostMetadata(  # type: ignore[attr-defined]
                hostname="test-host",
                local_ip="127.0.0.1",
                provider="test",
                region="test-region",
                availability_zone="test-az",
                vm_id="test-vm-1",
                os="Linux",
                os_version="TestOS 1.0",
                arch="x86_64",
            ),
            capabilities=capabilities,
            labels=args.label,
            registration_key=args.registration_key,
        )

        yield marvin_pb2.AgentMessage(  # type: ignore[attr-defined]
            agent_id=args.agent_id,
            register=registration,
        )
        self.stdout.write(f"[TX] Sent registration for agent {args.agent_id}")

        heartbeat_count = 0
        while not stop_event.is_set():
            await asyncio.sleep(args.heartbeat_interval)
            if stop_event.is_set():
                break
            heartbeat_count += 1
            ts = int(time.time() * 1000)
            yield marvin_pb2.AgentMessage(  # type: ignore[attr-defined]
                agent_id=args.agent_id,
                heartbeat=marvin_pb2.Heartbeat(  # type: ignore[attr-defined]
                    timestamp_unix_ms=ts,
                ),
            )
            pending_heartbeats[heartbeat_count] = time.monotonic()
            self.stdout.write(f"[TX] Sent heartbeat #{heartbeat_count} (pending ACK)")

    async def test_connection(self, args):
        target = f"{args.grpc_host}:{args.grpc_port}"
        self.stdout.write(f"Connecting to gRPC server at {target}...")

        stop_event = asyncio.Event()
        pending_heartbeats: dict[int, float] = {}

        channel_options = [
            ("grpc.keepalive_time_ms", 20000),
            ("grpc.keepalive_timeout_ms", 5000),
            ("grpc.keepalive_permit_without_calls", 1),
            ("grpc.http2.max_pings_without_data", 0),
        ]

        response_count = 0
        try:
            async with grpc.aio.insecure_channel(target, options=channel_options) as channel:
                stub = marvin_pb2_grpc.MarvinServiceStub(channel)
                self.stdout.write("Waiting for server to respond...")

                async for response in stub.Connect(
                    self.send_messages(args, stop_event, pending_heartbeats)
                ):
                    response_count += 1
                    payload_field = response.WhichOneof("payload")
                    self.stdout.write(f"[RX] Response #{response_count}: {payload_field}")

                    if response.HasField("registered"):
                        self.stdout.write("     SUCCESS! Server accepted registration")
                        hbi = response.registered.heartbeat_interval_seconds
                        self.stdout.write(f"        heartbeat_interval: {hbi}s")
                        self.stdout.write(
                            "        server_timestamp: "
                            f"{response.registered.server_timestamp_unix_ms}"
                        )

                    elif response.HasField("registration_rejected"):
                        self.stdout.write(f"     REJECTED: {response.registration_rejected.reason}")
                        self.stdout.write(
                            f"        permanent: {response.registration_rejected.permanent}"
                        )
                        stop_event.set()
                        break

                    elif response.HasField("execute_capability"):
                        self.stdout.write("     Execute capability request received!")
                        self.stdout.write(
                            f"        capability: {response.execute_capability.capability_name}"
                        )
                        self.stdout.write(
                            f"        session_id: {response.execute_capability.session_id}"
                        )
                        self.stdout.write(
                            f"        thread_id: {response.execute_capability.thread_id}"
                        )
                        self.stdout.write(
                            f"        params: {response.execute_capability.parameters_json}"
                        )

                    elif response.HasField("disconnect"):
                        self.stdout.write(
                            f"     Server requested disconnect: {response.disconnect.reason}"
                        )
                        stop_event.set()
                        break

                    elif response.HasField("heartbeat_ack"):
                        self.stdout.write("     Heartbeat ACK received")

                if pending_heartbeats:
                    self.stdout.write(
                        f"Stream ended with {len(pending_heartbeats)} unacknowledged heartbeat(s)"
                    )

        except grpc.aio.AioRpcError as e:
            self.stderr.write(f"gRPC connection failed: {e.code()} - {e.details()}")
            if e.code() == grpc.StatusCode.UNAVAILABLE:
                self.stderr.write("   Is the gRPC server running? Check: docker compose ps grpc")
            return 1
        except asyncio.CancelledError:
            self.stdout.write("Connection cancelled")
        finally:
            stop_event.set()

        self.stdout.write(f"Test complete. Received {response_count} response(s).")
        return 0

    async def run_with_timeout(self, args):
        try:
            await asyncio.wait_for(self.test_connection(args), timeout=args.duration + 5)
        except TimeoutError:
            self.stdout.write(f"Test timed out after {args.duration}s")

    def handle(self, *args, **options):
        class Args:
            pass

        args = Args()
        for key in (
            "registration_key",
            "agent_id",
            "grpc_host",
            "grpc_port",
            "capability",
            "label",
            "heartbeat_interval",
            "duration",
        ):
            setattr(args, key, options[key])

        try:
            asyncio.run(self.run_with_timeout(args))
        except KeyboardInterrupt:
            self.stdout.write("Interrupted by user")
