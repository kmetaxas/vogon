"""Registration ownership and reconnect snapshot regressions."""

import asyncio
from unittest.mock import patch

import pytest
from asgiref.sync import sync_to_async

import marvin_pb2
from apps.core.models import Organization
from apps.marvins.labels import LabelSelectorError
from apps.marvins.models import Marvin, MarvinRegistrationKey
from services.grpc_server.servicer import MarvinServicer, MarvinStream

pytestmark = pytest.mark.django_db(transaction=True)


def registration(key, labels=(), hostname="worker"):
    return marvin_pb2.AgentMessage(
        agent_id="worker-1",
        register=marvin_pb2.Register(
            registration_key=str(key.key),
            labels=labels,
            host=marvin_pb2.HostMetadata(hostname=hostname),
        ),
    )


async def messages(message):
    yield message


def test_cross_organization_stream_rejected_without_touching_victim():
    owner = Organization.objects.create(name="Owner", slug="owner")
    attacker = Organization.objects.create(name="Other", slug="other")
    key = MarvinRegistrationKey.objects.create(organization=attacker)
    victim = Marvin.objects.create(
        organization=owner,
        name="Victim",
        client_id="worker-1",
        status=Marvin.Status.ONLINE,
        labels=["env:prod"],
    )
    servicer = MarvinServicer()
    valid_stream = MarvinStream("worker-1")
    valid_stream.connected = True
    servicer.marvins["worker-1"] = valid_stream

    async def exercise():
        with patch("apps.marvins.config_assembly.assemble_marvin_config") as assemble:
            replies = [reply async for reply in servicer.Connect(messages(registration(key)), None)]
            assemble.assert_not_called()
        assert [reply.WhichOneof("payload") for reply in replies] == ["registration_rejected"]
        assert replies[0].registration_rejected.permanent
        assert servicer.marvins["worker-1"] is valid_stream
        assert valid_stream.connected

    asyncio.run(exercise())
    victim.refresh_from_db()
    assert victim.status == Marvin.Status.ONLINE
    assert victim.labels == ["env:prod"]


def test_registration_and_reconnect_replace_labels_and_hostname():
    org = Organization.objects.create(name="Owner", slug="owner")
    key = MarvinRegistrationKey.objects.create(organization=org)
    servicer = MarvinServicer()

    async def exercise():
        for labels, hostname in [
            (["env:prod", "region:west"], "first"),
            (["env:dev"], "second"),
            ([], "third"),
        ]:
            connection = servicer.Connect(messages(registration(key, labels, hostname)), None)
            response = await anext(connection)
            assert response.WhichOneof("payload") == "registered"
            agent = await sync_to_async(Marvin.objects.get)(client_id="worker-1")
            assert agent.labels == labels
            assert agent.hostname == hostname
            assert agent.status == Marvin.Status.ONLINE
            await connection.aclose()

    asyncio.run(exercise())


@pytest.mark.parametrize("existing", [False, True])
def test_invalid_labels_do_not_persist_or_replace_stream(existing):
    org = Organization.objects.create(name="Owner", slug="owner")
    key = MarvinRegistrationKey.objects.create(organization=org)
    if existing:
        Marvin.objects.create(
            organization=org,
            client_id="worker-1",
            name="Original",
            hostname="original",
            labels=["env:prod"],
            status=Marvin.Status.ONLINE,
        )
    servicer = MarvinServicer()
    valid_stream = MarvinStream("worker-1")
    servicer.marvins["worker-1"] = valid_stream
    message = registration(key, ["env:one", "env:two"], "changed")
    with pytest.raises(LabelSelectorError):
        servicer._update_marvin_registration("worker-1", message.register, org)

    async def exercise():
        replies = [reply async for reply in servicer.Connect(messages(message), None)]
        assert [reply.WhichOneof("payload") for reply in replies] == ["registration_rejected"]
        assert servicer.marvins["worker-1"] is valid_stream

    asyncio.run(exercise())
    if existing:
        agent = Marvin.objects.get(client_id="worker-1")
        assert agent.hostname == "original"
        assert agent.labels == ["env:prod"]
        assert agent.status == Marvin.Status.ONLINE
    else:
        assert not Marvin.objects.filter(client_id="worker-1").exists()


def test_old_same_organization_stream_cleanup_preserves_online_replacement():
    org = Organization.objects.create(name="Owner", slug="owner")
    key = MarvinRegistrationKey.objects.create(organization=org)
    servicer = MarvinServicer()

    async def exercise():
        old_connection = servicer.Connect(messages(registration(key)), None)
        new_connection = servicer.Connect(
            messages(registration(key, ["env:prod"], "replacement")), None
        )
        try:
            assert (await anext(old_connection)).WhichOneof("payload") == "registered"
            old_stream = servicer.marvins["worker-1"]
            assert (await anext(new_connection)).WhichOneof("payload") == "registered"
            replacement = servicer.marvins["worker-1"]
            assert replacement is not old_stream

            await old_connection.aclose()

            assert not old_stream.connected
            assert servicer.marvins["worker-1"] is replacement
            assert replacement.connected
            agent = await sync_to_async(Marvin.objects.get)(client_id="worker-1")
            assert agent.status == Marvin.Status.ONLINE
            assert agent.hostname == "replacement"
            assert agent.labels == ["env:prod"]
        finally:
            await old_connection.aclose()
            await new_connection.aclose()

    asyncio.run(exercise())


def test_reconnect_waits_for_overlapping_offline_cleanup_before_writing_online():
    org = Organization.objects.create(name="Owner", slug="owner")
    key = MarvinRegistrationKey.objects.create(organization=org)
    servicer = MarvinServicer()

    async def exercise():
        offline_started = asyncio.Event()
        release_offline = asyncio.Event()
        reconnect_waiting = asyncio.Event()

        class ObservedLock(asyncio.Lock):
            async def __aenter__(self):
                if self.locked():
                    reconnect_waiting.set()
                return await super().__aenter__()

        servicer._lock = ObservedLock()
        update_status = servicer._update_marvin_status

        async def paused_offline(agent_id, status):
            if status == "offline":
                offline_started.set()
                await release_offline.wait()
            await update_status(agent_id, status)

        old_connection = servicer.Connect(messages(registration(key)), None)
        new_connection = servicer.Connect(messages(registration(key)), None)
        assert (await anext(old_connection)).WhichOneof("payload") == "registered"
        with patch.object(servicer, "_update_marvin_status", side_effect=paused_offline):
            close_task = asyncio.create_task(old_connection.aclose())
            await asyncio.wait_for(offline_started.wait(), timeout=5)
            register_task = asyncio.create_task(anext(new_connection))
            try:
                await asyncio.wait_for(reconnect_waiting.wait(), timeout=5)
                release_offline.set()
                await close_task
                response = await register_task
                assert response.WhichOneof("payload") == "registered"
                assert servicer.marvins["worker-1"].connected
                agent = await sync_to_async(Marvin.objects.get)(client_id="worker-1")
                assert agent.status == Marvin.Status.ONLINE
            finally:
                release_offline.set()
                await asyncio.gather(close_task, register_task, return_exceptions=True)
                await new_connection.aclose()

    asyncio.run(exercise())
