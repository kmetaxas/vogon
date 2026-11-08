from types import SimpleNamespace
from unittest.mock import patch

import pytest

from apps.core.models import Organization
from apps.marvins.discovery import DiscoveryEngine
from apps.marvins.models import Capability, Marvin
from services.temporal_workers.activities import _find_targets, _find_tools, _route_and_execute


@pytest.fixture
def fleet(db):
    org = Organization.objects.create(name="Targeting", slug="targeting")
    capability = Capability.objects.create(organization=org, name="inspect")
    for index in range(4):
        marvin = Marvin.objects.create(
            organization=org,
            name=f"agent-{index}",
            client_id=f"target-{index}",
            hostname=f"host-{index}",
            status="online",
            labels=["env:production"],
        )
        marvin.capabilities.add(capability)
    return org


def test_schema_exposes_targeting():
    from services.llm.tools import STANDARD_TOOLS

    specs = {spec.name: spec.parameters for spec in STANDARD_TOOLS}
    assert "selector" in specs["find_tools"]["properties"]
    assert specs["execute_tool"]["properties"]["expected_target_count"]["minimum"] == 1
    assert "find_targets" in specs


def test_metadata_and_session_scope(fleet):
    scope = {"hostname": ["host-1"]}
    preview = _find_targets(fleet, "inspect", session_scope=scope)
    assert preview["matched_count"] == 1
    assert preview["targets"][0]["host_metadata"]["hostname"] == "host-1"
    assert preview["targets"][0]["labels"] == {"env": "production"}
    assert _find_tools(fleet.id, labels="hostname:host-1") == []
    assert _find_tools(fleet.id, selector=scope)[0]["online_marvins"] == 1
    assert _find_targets(fleet, "inspect", {"hostnmae": ["host-1"]})["code"] == "invalid_selector"
    assert _find_tools(fleet.id, labels="env:production", selector={})["code"] == "invalid_selector"


def test_count_before_budget(fleet):
    thread = SimpleNamespace(tsession=SimpleNamespace(target_scope=None))
    original = DiscoveryEngine().check_execution_policy
    with patch(
        "apps.marvins.discovery.DiscoveryEngine.check_execution_policy", wraps=original
    ) as reserve:
        tool_call, ids, error = _route_and_execute(
            fleet,
            thread,
            {"capability_name": "inspect", "expected_target_count": 1},
        )
    assert tool_call is None and ids == []
    assert error["code"] == "target_count_mismatch", error
    assert error["matched_count"] == 4
    assert all(call.kwargs.get("session") is None for call in reserve.call_args_list)


@pytest.mark.parametrize("labels", [False, 0, []])
def test_false_labels_rejected(labels):
    from services.temporal_workers.activities import _target_selector

    with pytest.raises(ValueError):
        _target_selector(labels=labels)


def test_successful_structured_route_persists_scope_and_budget(fleet):
    from apps.marvins.models import TargetScope
    from apps.sessions.models import Thread, TSession

    scope = TargetScope.objects.create(
        organization=fleet,
        name="Production",
        slug="production",
        selector={"labels": {"env": "production"}},
    )
    session = TSession.objects.create(organization=fleet, title="Scoped", target_scope=scope)
    thread = Thread.objects.create(tsession=session, title="Main")
    tool_call, ids, error = _route_and_execute(
        fleet,
        thread,
        {
            "capability_name": "inspect",
            "selector": {"hostname": ["host-1"]},
            "expected_target_count": 1,
            "parameters": {},
        },
    )
    assert error is None
    assert ids == [str(tool_call.marvin_id)]
    assert tool_call.marvin.hostname == "host-1"
    assert tool_call.labels == {"hostname": ["host-1"], "labels": {"env": "production"}}
    session.refresh_from_db()
    assert session.execution_budget["executions_used"] == 1
    assert session.execution_budget["targets_used"] == 1
    assert session.execution_budget["concurrent_running"] == 1
