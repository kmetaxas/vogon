#!/usr/bin/env python3
"""Test Marvin gRPC connection manually.

This script simulates a Marvin agent connecting to the gRPC server
so you can verify end-to-end connectivity and registration.

Usage:
    python scripts/test_marvin_connection.py \
        --registration-key YOUR_KEY \
        --agent-id test-agent-1 \
        --grpc-host localhost \
        --grpc-port 50051

If you don't have a registration key, create one in Django admin
or use: python manage.py shell -c \
    "from apps.marvins.models import MarvinRegistrationKey; \
     k=MarvinRegistrationKey.objects.first(); print(k.key)"
"""

import argparse
import asyncio
import sys
import time

import grpc

import marvin_pb2
import marvin_pb2_grpc


def parse_args():
    parser = argparse.ArgumentParser(description="Test Marvin gRPC connection")
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
        default="localhost",
        help="gRPC server host (default: localhost)",
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
    return parser.parse_args()


async def send_messages(args, stop_event: asyncio.Event, pending_heartbeats: dict):
    """Generate messages for the bi-directional stream."""
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
    print(f"[TX] Sent registration for agent {args.agent_id}")

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
        print(f"[TX] Sent heartbeat #{heartbeat_count} (pending ACK)")


async def test_connection(args):
    target = f"{args.grpc_host}:{args.grpc_port}"
    print(f"Connecting to gRPC server at {target}...")

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

            print("Waiting for server to respond...")
            last_rx_time = time.monotonic()

            async for response in stub.Connect(send_messages(args, stop_event, pending_heartbeats)):
                response_count += 1
                payload_field = response.WhichOneof("payload")
                print(f"[RX] Response #{response_count}: {payload_field}")
                last_rx_time = time.monotonic()

                if response.HasField("registered"):
                    print("     ✅ SUCCESS! Server accepted registration")
                    print(
                        f"        heartbeat_interval: {response.registered.heartbeat_interval_seconds}s"
                    )
                    print(
                        f"        server_timestamp: {response.registered.server_timestamp_unix_ms}"
                    )

                elif response.HasField("registration_rejected"):
                    print(f"     ❌ REJECTED: {response.registration_rejected.reason}")
                    print(f"        permanent: {response.registration_rejected.permanent}")
                    stop_event.set()
                    break

                elif response.HasField("execute_capability"):
                    print("     📋 Execute capability request received!")
                    print(f"        capability: {response.execute_capability.capability_name}")
                    print(f"        session_id: {response.execute_capability.session_id}")
                    print(f"        thread_id: {response.execute_capability.thread_id}")
                    print(f"        params: {response.execute_capability.parameters_json}")

                elif response.HasField("disconnect"):
                    print(f"     🔌 Server requested disconnect: {response.disconnect.reason}")
                    stop_event.set()
                    break

                elif response.HasField("heartbeat_ack"):
                    print("     💓 Heartbeat ACK received")

            # Detect missed heartbeats after stream ends
            if pending_heartbeats:
                print(
                    f"\n⚠️  Stream ended with {len(pending_heartbeats)} unacknowledged heartbeat(s)"
                )

    except grpc.aio.AioRpcError as e:
        print(f"❌ gRPC connection failed: {e.code()} - {e.details()}")
        if e.code() == grpc.StatusCode.UNAVAILABLE:
            print("   Is the gRPC server running? (python services/grpc_server/servicer.py)")
        return 1
    except asyncio.CancelledError:
        print("\n⚠️  Connection cancelled")
    except KeyboardInterrupt:
        print("\n⚠️  Interrupted by user")
    finally:
        stop_event.set()

    print(f"\n✅ Test complete. Received {response_count} response(s).")
    return 0


async def run_with_timeout(args):
    try:
        await asyncio.wait_for(test_connection(args), timeout=args.duration + 5)
    except TimeoutError:
        print(f"\n⏱️  Test timed out after {args.duration}s")


def main():
    args = parse_args()
    try:
        asyncio.run(run_with_timeout(args))
    except KeyboardInterrupt:
        print("\n👋 Goodbye!")
        sys.exit(0)


if __name__ == "__main__":
    main()
