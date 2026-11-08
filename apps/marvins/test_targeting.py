"""Regression tests for fail-closed, actual-label-only targeting."""

from types import SimpleNamespace

import pytest

from apps.core.models import Organization
from apps.marvins.discovery import DiscoveryEngine
from apps.marvins.labels import (
    LabelSelectorError,
    marvin_matches_labels,
    normalize_labels,
    parse_label_selector,
)
from apps.marvins.models import Capability, Marvin
from apps.marvins.selectors import TargetSelector


@pytest.mark.parametrize("labels", [[":bad"], ["x:1", " x :2"], ["x:"], [1], "x"])
def test_invalid_snapshots(labels):
    with pytest.raises(LabelSelectorError):
        normalize_labels(labels)


def test_label_normalization_and_literal_values():
    assert normalize_labels([" team : platform ", " canary "]) == ["team:platform", "canary"]
    selector = TargetSelector(labels={" custom ": " a,b=c "})
    assert marvin_matches_labels(SimpleNamespace(labels=[" custom : a,b=c "]), selector.labels)
    with pytest.raises(LabelSelectorError):
        parse_label_selector("env:a, env:b")


@pytest.mark.parametrize("selector", [",", "env:prod,", ",env:prod", "env:prod,,tag"])
def test_empty_legacy_segments_rejected(selector):
    with pytest.raises(LabelSelectorError):
        parse_label_selector(selector)


def test_empty_legacy_selector_and_custom_hostname_label():
    assert parse_label_selector("  ") == {}
    assert parse_label_selector("hostname:custom") == {"hostname": "custom"}
    assert marvin_matches_labels(
        SimpleNamespace(labels=["hostname:custom"]), parse_label_selector("hostname:custom")
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"lables": {}},
        {"labels": {"": "x"}},
        {"labels": {"x": [1]}},
        {"hostname": "host"},
        {"marvin_ids": ["not-a-uuid"]},
        [],
    ],
)
def test_invalid_structured_selector(payload):
    with pytest.raises(ValueError):
        TargetSelector.from_dict(payload)


def test_scope_intersections_and_empty_filters():
    scope = TargetSelector(hostname=["a"], labels={"env": None, "team": ["a"]})
    request = TargetSelector(hostname=["b"], labels={"env": [], "team": "b"})
    merged = scope.merge_with(request)
    assert merged.hostname == []
    assert merged.labels == {"env": [], "team": []}
    assert not marvin_matches_labels(SimpleNamespace(labels=["env:prod"]), {"env": []})


@pytest.mark.django_db
def test_four_agents_actual_labels_inventory_and_scoping(monkeypatch):
    monkeypatch.setattr("apps.marvins.signals._push_config_to_marvin", lambda *args: None)
    org = Organization.objects.create(name="Target", slug="target")
    other = Organization.objects.create(name="Other", slug="target-other")
    capability = Capability.objects.create(organization=org, name="target-test", enabled=True)
    agents = []
    for index, labels in enumerate([[], [" region : west "], ["custom:a,b=c"], ["tag"]]):
        agent = Marvin.objects.create(
            organization=org,
            client_id=f"target-{index}",
            hostname=f"host-{index}",
            region="west",
            status="online",
            labels=labels,
        )
        agent.capabilities.add(capability)
        agents.append(agent)
    foreign = Marvin.objects.create(
        organization=other, client_id="foreign", status="online", labels=["secret:private"]
    )
    foreign.capabilities.add(capability)
    engine = DiscoveryEngine()
    result = engine.find_capabilities(org, selector={"labels": {"region": "west"}})
    assert result["results"][0]["online_count"] == 1
    assert result["results"][0]["label_inventory"]["keys"] == {"region": ["west"]}
    result = engine.find_capabilities(org)
    assert result["results"][0]["online_count"] == 4
    assert "secret" not in result["results"][0]["label_inventory"]["keys"]
    assert engine.find_capabilities(org, selector={"hostname": []})["results"] == []
    assert engine.find_capabilities(org, filters={"providers": []})["results"] == []
    snapshot = engine.resolve_targets(org, capability, {"labels": {"region": "absent"}})
    assert snapshot.snapshot == []
    assert snapshot.snapshot_metadata["diagnostics"]["stage_counts"] == {
        "online_capability_candidates": 4,
        "selector_matches": 0,
        "logical_targets": 0,
        "selected_executors": 0,
    }
