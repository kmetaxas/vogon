import os

os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "true")

from unittest.mock import patch

import pytest

from services.mcp_server import server


@pytest.mark.asyncio
async def test_execute_conflicts():
    with patch.object(server, "validate_mcp_token", return_value=object()):
        result = await server.execute("token", "org", "inspect", {}, "env:prod", "set")
        assert result["code"] == "invalid_selector"
        result = await server.execute(
            "token",
            "org",
            "inspect",
            {},
            labels="env:prod",
            selector={},
        )
        assert result["code"] == "invalid_selector"


@pytest.mark.asyncio
async def test_execute_tool_legacy_positional():
    with patch.object(server, "execute", return_value={"success": True}) as execute:
        assert await server.execute_tool("token", "org", "inspect", {}, "env:prod") == {
            "success": True,
        }
    assert execute.call_args.kwargs["labels"] == "env:prod"
    assert execute.call_args.kwargs["selector"] is None


@pytest.fixture
def real_fleet(transactional_db):
    from apps.core.models import Organization
    from apps.marvins.models import Capability, Marvin, TargetScope
    from apps.sessions.models import Thread, TSession

    org = Organization.objects.create(name="Scoped", slug="scoped")
    token = org.generate_mcp_token()
    capability = Capability.objects.create(organization=org, name="inspect")
    for index in range(4):
        marvin = Marvin.objects.create(
            organization=org,
            name=f"agent-{index}",
            client_id=f"scoped-{index}",
            hostname=f"host-{index}",
            status="online",
            labels=["env:production"] + (["hostname:host-0"] if index == 2 else []),
        )
        marvin.capabilities.add(capability)
    scope = TargetScope.objects.create(
        organization=org,
        name="One host",
        slug="one",
        selector={"hostname": ["host-0"]},
    )
    session = TSession.objects.create(organization=org, title="Scoped", target_scope=scope)
    thread = Thread.objects.create(tsession=session, title="Main")
    return org, token, capability, session, thread


def test_real_preview_metadata_and_literal_label(real_fleet):
    import asyncio

    org, token, capability, _, _ = real_fleet
    metadata = asyncio.run(
        server.find_targets(
            token,
            org.slug,
            capability.name,
            {"hostname": ["host-0"]},
        )
    )
    literal = asyncio.run(
        server.find_targets(
            token,
            org.slug,
            capability.name,
            {"labels": {"hostname": "host-0"}},
        )
    )
    assert metadata["matched_count"] == literal["matched_count"] == 1
    assert metadata["targets"][0]["host_metadata"]["hostname"] == "host-0"
    assert literal["targets"][0]["host_metadata"]["hostname"] == "host-2"


@pytest.mark.parametrize(
    "case", ["outside", "foreign_session", "foreign_thread", "frozen", "count"]
)
def test_real_execution_rejects_before_dispatch(real_fleet, case):
    import asyncio

    from apps.core.models import Organization
    from apps.marvins.discovery import DiscoveryEngine
    from apps.sessions.models import Thread, ToolCall, TSession

    org, token, capability, session, thread = real_fleet
    kwargs = {"session_id": str(session.id), "thread_id": str(thread.id)}
    if case == "outside":
        kwargs["selector"] = {"hostname": ["host-1"]}
    elif case in {"foreign_session", "foreign_thread"}:
        foreign = Organization.objects.create(name="Foreign", slug="foreign")
        foreign_session = TSession.objects.create(organization=foreign, title="Foreign")
        foreign_thread = Thread.objects.create(tsession=foreign_session, title="Foreign")
        kwargs = (
            {"session_id": str(foreign_session.id)}
            if case == "foreign_session"
            else {"thread_id": str(foreign_thread.id)}
        )
    elif case == "frozen":
        target_set = DiscoveryEngine().resolve_targets(
            org,
            capability,
            {"hostname": ["host-1"]},
        )
        kwargs["target_set_id"] = str(target_set.id)
    else:
        kwargs["expected_target_count"] = 2
    before = ToolCall.objects.count()
    with patch.object(server, "get_temporal_client") as temporal:
        result = asyncio.run(server.execute(token, org.slug, capability.name, {}, **kwargs))
    assert result["success"] is False
    assert ToolCall.objects.count() == before
    temporal.assert_not_called()
    if case == "outside":
        assert result["code"] == "no_matching_target"
    if case == "count":
        assert result["code"] == "target_count_mismatch"
