# pyright: reportMissingImports=false

import asyncio
from unittest.mock import AsyncMock

from django.apps import apps
from django.test import TestCase
from django.test.testcases import TransactionTestCase
from django.urls import reverse

from apps.core.models import Organization, OrganizationMembership, User
from apps.marvins.auth import validate_marvin_registration_key
from apps.marvins.discovery import DiscoveryEngine
from apps.marvins.models import (
    Capability,
    Marvin,
    MarvinRegistrationKey,
    Resource,
    ResourceType,
)
from marvin_pb2 import (
    CapabilityManifest,
    CapabilityResult,
    HostMetadata,
    Register,
)
from services.grpc_server.servicer import MarvinServicer, MarvinStream


class MarvinServicerTests(TransactionTestCase):
    def _create_servicer(self):
        return MarvinServicer()

    def test_registration_creates_marvin_with_host_metadata(self):
        organization = Organization.objects.create(name="Marvin", slug="marvin")
        servicer = self._create_servicer()
        agent_id = "test-agent-1"

        register = Register(
            agent_version="marvin-agent/v1.0.0",
            host=HostMetadata(
                hostname="server1.us-east.example.com",
                local_ip="10.0.0.1",
                provider="aws",
                region="us-east-1",
                availability_zone="us-east-1a",
                vm_id="i-1234567890abcdef0",
                os="Linux",
                os_version="Ubuntu 22.04",
                arch="x86_64",
            ),
            capabilities=[
                CapabilityManifest(
                    name="kinit",
                    description="Kerberos init",
                    enabled=True,
                    parameters_json_schema="{}",
                    config={"user": "svc_account", "realm": "EXAMPLE.COM"},
                    config_summary="kinit as svc_account@EXAMPLE.COM",
                ),
            ],
            labels=["env:production", "team:platform"],
        )

        asyncio.run(servicer._handle_register(agent_id, register, organization))

        Marvin = apps.get_model("marvins", "Marvin")
        marvin = Marvin.objects.get(client_id=agent_id)

        self.assertEqual(marvin.agent_version, "marvin-agent/v1.0.0")
        self.assertEqual(marvin.hostname, "server1.us-east.example.com")
        self.assertEqual(marvin.local_ip, "10.0.0.1")
        self.assertEqual(marvin.provider, "aws")
        self.assertEqual(marvin.region, "us-east-1")
        self.assertEqual(marvin.availability_zone, "us-east-1a")
        self.assertEqual(marvin.vm_id, "i-1234567890abcdef0")
        self.assertEqual(marvin.os, "Linux")
        self.assertEqual(marvin.os_version, "Ubuntu 22.04")
        self.assertEqual(marvin.arch, "x86_64")
        self.assertEqual(marvin.labels, ["env:production", "team:platform"])
        self.assertEqual(
            marvin.capability_configs,
            {"kinit": {"user": "svc_account", "realm": "EXAMPLE.COM"}},
        )

    def test_registration_links_enabled_capability(self):
        organization = Organization.objects.create(name="Marvin", slug="marvin")
        servicer = self._create_servicer()
        agent_id = "test-agent-2"

        register = Register(
            agent_version="1.0.0",
            host=HostMetadata(hostname="host-a"),
            capabilities=[
                CapabilityManifest(
                    name="kinit",
                    enabled=True,
                    parameters_json_schema="{}",
                ),
                CapabilityManifest(
                    name="disabled-cap",
                    enabled=False,
                    parameters_json_schema="{}",
                ),
            ],
        )

        asyncio.run(servicer._handle_register(agent_id, register, organization))

        Marvin = apps.get_model("marvins", "Marvin")
        Capability = apps.get_model("marvins", "Capability")
        marvin = Marvin.objects.get(client_id=agent_id)

        cap_names = set(marvin.capabilities.values_list("name", flat=True))
        self.assertEqual(cap_names, {"kinit"})
        self.assertFalse(Capability.objects.filter(name="disabled-cap").exists())

    def test_registration_reenables_disabled_capability(self):
        organization = Organization.objects.create(name="ReEnable", slug="reenable")
        servicer = self._create_servicer()
        agent_id = "test-agent-reenable"

        # Create the capability in a disabled state first
        Capability = apps.get_model("marvins", "Capability")
        Capability.objects.create(
            organization=organization,
            name="network.icmp.echo_request",
            description="Send ICMP echo request",
            enabled=False,
        )

        register = Register(
            agent_version="1.0.0",
            host=HostMetadata(hostname="server1.us-east.example.com"),
            capabilities=[
                CapabilityManifest(
                    name="network.icmp.echo_request",
                    description="Send ICMP echo request",
                    enabled=True,
                    parameters_json_schema="{}",
                ),
            ],
        )

        asyncio.run(servicer._handle_register(agent_id, register, organization))

        marvin = apps.get_model("marvins", "Marvin").objects.get(client_id=agent_id)
        cap = marvin.capabilities.get(name="network.icmp.echo_request")
        self.assertTrue(cap.enabled)

    def test_find_tools_returns_capability_without_query_or_labels(self):
        from services.temporal_workers.activities import _find_tools

        organization = Organization.objects.create(name="FindTools", slug="findtools")
        Capability = apps.get_model("marvins", "Capability")
        cap = Capability.objects.create(
            organization=organization,
            name="network.icmp.echo_request",
            description="Ping a host",
            enabled=True,
        )
        Marvin = apps.get_model("marvins", "Marvin")
        marvin = Marvin.objects.create(
            organization=organization,
            name="server1.us-east.example.com",
            client_id="agent-1",
            status="online",
        )
        marvin.capabilities.add(cap)

        result = _find_tools(str(organization.id))

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "network.icmp.echo_request")

    def test_find_tools_excludes_disabled_capabilities(self):
        from services.temporal_workers.activities import _find_tools

        organization = Organization.objects.create(name="FindDisabled", slug="finddisabled")
        Capability = apps.get_model("marvins", "Capability")
        cap = Capability.objects.create(
            organization=organization,
            name="network.icmp.echo_request",
            description="Ping a host",
            enabled=False,
        )
        Marvin = apps.get_model("marvins", "Marvin")
        marvin = Marvin.objects.create(
            organization=organization,
            name="server1.us-east.example.com",
            client_id="agent-2",
            status="online",
        )
        marvin.capabilities.add(cap)

        result = _find_tools(str(organization.id))

        self.assertEqual(result, [])

    def test_find_tools_filters_by_online_marvin_labels(self):
        from services.temporal_workers.activities import _find_tools

        organization = Organization.objects.create(name="FindLabels", slug="findlabels")
        Capability = apps.get_model("marvins", "Capability")
        cap = Capability.objects.create(
            organization=organization,
            name="network.icmp.echo_request",
            description="Ping a host",
            enabled=True,
        )
        Marvin = apps.get_model("marvins", "Marvin")
        marvin = Marvin.objects.create(
            organization=organization,
            name="server1.us-east.example.com",
            client_id="agent-3",
            status="online",
            labels=["env:production"],
        )
        marvin.capabilities.add(cap)

        result_no_labels = _find_tools(str(organization.id))
        self.assertEqual(len(result_no_labels), 1)

        result_matching = _find_tools(str(organization.id), labels="env:production")
        self.assertEqual(len(result_matching), 1)

        result_non_matching = _find_tools(str(organization.id), labels="env:staging")
        self.assertEqual(result_non_matching, [])

    def test_find_tools_excludes_capabilities_with_no_online_marvins(self):
        from services.temporal_workers.activities import _find_tools

        organization = Organization.objects.create(name="NoOnline", slug="noonline")
        Capability = apps.get_model("marvins", "Capability")
        cap = Capability.objects.create(
            organization=organization,
            name="network.icmp.echo_request",
            description="Ping a host",
            enabled=True,
        )
        Marvin = apps.get_model("marvins", "Marvin")
        marvin = Marvin.objects.create(
            organization=organization,
            name="server1.us-east.example.com",
            client_id="agent-offline",
            status="offline",
        )
        marvin.capabilities.add(cap)

        result = _find_tools(str(organization.id))
        self.assertEqual(result, [])

    def test_find_tools_returns_online_marvin_count_and_sample_labels(self):
        from services.temporal_workers.activities import _find_tools

        organization = Organization.objects.create(name="Counts", slug="counts")
        Capability = apps.get_model("marvins", "Capability")
        cap = Capability.objects.create(
            organization=organization,
            name="restart",
            description="Restart service",
            enabled=True,
        )
        Marvin = apps.get_model("marvins", "Marvin")
        m1 = Marvin.objects.create(
            organization=organization,
            name="worker-a",
            client_id="agent-a",
            status="online",
            labels=["env:production", "team:platform"],
        )
        m1.capabilities.add(cap)
        m2 = Marvin.objects.create(
            organization=organization,
            name="worker-b",
            client_id="agent-b",
            status="online",
            labels=["env:production", "team:platform"],
        )
        m2.capabilities.add(cap)

        result = _find_tools(str(organization.id))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "restart")
        self.assertEqual(result[0]["online_marvins"], 2)
        self.assertIn("env:production", result[0]["sample_labels"])
        self.assertIn("team:platform", result[0]["sample_labels"])

    def test_find_tools_exact_capability_name_lookup(self):
        from services.temporal_workers.activities import _find_tools

        organization = Organization.objects.create(name="Exact", slug="exact")
        Capability = apps.get_model("marvins", "Capability")
        cap_a = Capability.objects.create(
            organization=organization,
            name="restart",
            description="Restart service",
            enabled=True,
        )
        cap_b = Capability.objects.create(
            organization=organization,
            name="restart_database",
            description="Restart database",
            enabled=True,
        )
        Marvin = apps.get_model("marvins", "Marvin")
        m = Marvin.objects.create(
            organization=organization,
            name="worker",
            client_id="agent",
            status="online",
        )
        m.capabilities.add(cap_a)
        m.capabilities.add(cap_b)

        result = _find_tools(str(organization.id), capability_name="restart")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "restart")

    def test_capability_result_routes_to_future(self):
        class _CaptureStream(MarvinStream):
            def __init__(self, agent_id: str):
                super().__init__(agent_id)
                self.captured: dict = {}

            def handle_response(self, command_id: str, result: dict):
                self.captured["command_id"] = command_id
                self.captured["result"] = result

        servicer = MarvinServicer()
        agent_id = "test-agent-3"
        stream = _CaptureStream(agent_id)

        from datetime import timedelta

        duration = timedelta(seconds=1, milliseconds=500)
        result = CapabilityResult(
            command_id="cmd-123",
            session_id="session-456",
            thread_id="thread-789",
            capability_name="kinit",
            success=True,
            result_json='{"ticket": "krbtgt/EXAMPLE.COM"}',
            execution_duration=duration,
        )

        asyncio.run(servicer._handle_capability_result(agent_id, result, stream))

        self.assertEqual(stream.captured["command_id"], "cmd-123")
        res = stream.captured["result"]
        self.assertEqual(res["session_id"], "session-456")
        self.assertEqual(res["thread_id"], "thread-789")
        self.assertEqual(res["capability_name"], "kinit")
        self.assertTrue(res["success"])
        self.assertEqual(res["result"], {"ticket": "krbtgt/EXAMPLE.COM"})
        self.assertIsNone(res["error"])
        self.assertEqual(res["execution_duration_ms"], 1500)

    def test_update_marvin_status_updates_model_from_async_handler(self):
        organization = Organization.objects.create(name="Marvin", slug="marvin")
        Marvin = apps.get_model("marvins", "Marvin")
        marvin = Marvin.objects.create(
            organization=organization,
            name="Worker",
            client_id="worker-1",
        )

        asyncio.run(MarvinServicer()._update_marvin_status("worker-1", "online"))

        marvin.refresh_from_db()
        self.assertEqual(marvin.status, "online")
        self.assertIsNotNone(marvin.last_seen)

    def test_startup_cleanup_marks_all_online_offline(self):
        from services.grpc_server.main import _mark_all_online_offline

        organization = Organization.objects.create(name="Cleanup", slug="cleanup")
        Marvin = apps.get_model("marvins", "Marvin")
        m1 = Marvin.objects.create(
            organization=organization,
            name="Worker A",
            client_id="worker-a",
            status="online",
        )
        m2 = Marvin.objects.create(
            organization=organization,
            name="Worker B",
            client_id="worker-b",
            status="offline",
        )

        _mark_all_online_offline()

        m1.refresh_from_db()
        m2.refresh_from_db()
        self.assertEqual(m1.status, "offline")
        self.assertEqual(m2.status, "offline")

    def test_stale_cleanup_leaves_recent_online_alone(self):
        from datetime import timedelta

        from django.utils import timezone

        from services.grpc_server.main import _mark_stale_offline

        organization = Organization.objects.create(name="Stale", slug="stale")
        Marvin = apps.get_model("marvins", "Marvin")
        recent = Marvin.objects.create(
            organization=organization,
            name="Recent",
            client_id="recent",
            status="online",
            last_seen=timezone.now() - timedelta(seconds=10),
        )
        stale = Marvin.objects.create(
            organization=organization,
            name="Stale",
            client_id="stale",
            status="online",
            last_seen=timezone.now() - timedelta(seconds=120),
        )

        _mark_stale_offline()

        recent.refresh_from_db()
        stale.refresh_from_db()
        self.assertEqual(recent.status, "online")
        self.assertEqual(stale.status, "offline")

    def test_stale_cleanup_ignores_offline_marvins(self):
        from datetime import timedelta

        from django.utils import timezone

        from services.grpc_server.main import _mark_stale_offline

        organization = Organization.objects.create(name="Ignore", slug="ignore")
        Marvin = apps.get_model("marvins", "Marvin")
        offline_stale = Marvin.objects.create(
            organization=organization,
            name="OfflineStale",
            client_id="offline-stale",
            status="offline",
            last_seen=timezone.now() - timedelta(seconds=120),
        )

        _mark_stale_offline()

        offline_stale.refresh_from_db()
        self.assertEqual(offline_stale.status, "offline")


class MarvinRegistrationKeyTests(TransactionTestCase):
    def test_key_is_generated_automatically(self):
        organization = Organization.objects.create(name="Org", slug="org")
        key = MarvinRegistrationKey.objects.create(organization=organization)

        self.assertTrue(key.key)
        self.assertEqual(len(key.key), 64)
        self.assertTrue(key.key.isalnum())

    def test_key_is_org_scoped_and_not_cross_org(self):
        org_a = Organization.objects.create(name="A", slug="a")
        org_b = Organization.objects.create(name="B", slug="b")

        key_a = MarvinRegistrationKey.objects.create(organization=org_a)
        MarvinRegistrationKey.objects.create(organization=org_b)

        result = validate_marvin_registration_key(key_a.key)
        self.assertEqual(result, org_a)

    def test_inactive_key_is_rejected(self):
        organization = Organization.objects.create(name="Org", slug="org")
        key = MarvinRegistrationKey.objects.create(organization=organization, active=False)

        result = validate_marvin_registration_key(key.key)
        self.assertIsNone(result)

    def test_invalid_key_is_rejected(self):
        result = validate_marvin_registration_key("totally-made-up-key")
        self.assertIsNone(result)

    def test_keys_are_unique(self):
        organization = Organization.objects.create(name="Org", slug="org")
        key_a = MarvinRegistrationKey.objects.create(organization=organization)

        from django.db import IntegrityError

        with self.assertRaises(IntegrityError):
            MarvinRegistrationKey.objects.create(organization=organization, key=key_a.key)


class MarvinRegistrationKeyAuthTests(TransactionTestCase):
    def test_backend_returns_none_without_key(self):
        from apps.marvins.auth import MarvinKeyBackend

        backend = MarvinKeyBackend()
        self.assertIsNone(backend.authenticate(None, marvin_key=None))

    def test_backend_returns_none_for_invalid_key(self):
        from apps.marvins.auth import MarvinKeyBackend

        backend = MarvinKeyBackend()
        self.assertIsNone(backend.authenticate(None, marvin_key="bad-key"))

    def test_backend_returns_user_for_valid_key(self):
        from apps.core.models import User
        from apps.marvins.auth import MarvinKeyBackend

        organization = Organization.objects.create(name="Org", slug="org")
        user = User.objects.create_user(username="agent", password="pass")
        organization.users.add(user)
        key = MarvinRegistrationKey.objects.create(organization=organization)

        backend = MarvinKeyBackend()
        result = backend.authenticate(None, marvin_key=key.key)
        self.assertEqual(result, user)

    def test_backend_get_user(self):
        from apps.core.models import User
        from apps.marvins.auth import MarvinKeyBackend

        user = User.objects.create_user(username="agent", password="pass")
        backend = MarvinKeyBackend()
        self.assertEqual(backend.get_user(user.id), user)
        self.assertIsNone(backend.get_user(999999))


class LabelSelectorTests(TransactionTestCase):
    def test_parse_label_selector_with_key_value_pairs(self):
        from apps.marvins.labels import parse_label_selector

        result = parse_label_selector("env:production,team:platform")
        self.assertEqual(result, {"env": "production", "team": "platform"})

    def test_parse_label_selector_with_bare_keys(self):
        from apps.marvins.labels import parse_label_selector

        result = parse_label_selector("canary,debug")
        self.assertEqual(result, {"canary": None, "debug": None})

    def test_parse_label_selector_mixed(self):
        from apps.marvins.labels import parse_label_selector

        result = parse_label_selector("env:staging,canary")
        self.assertEqual(result, {"env": "staging", "canary": None})

    def test_parse_label_selector_empty_string(self):
        from apps.marvins.labels import parse_label_selector

        result = parse_label_selector("")
        self.assertEqual(result, {})

    def test_parse_label_selector_whitespace(self):
        from apps.marvins.labels import parse_label_selector

        result = parse_label_selector(" env : production , team : platform ")
        self.assertEqual(result, {"env": "production", "team": "platform"})

    def test_parse_label_selector_rejects_equals_separator(self):
        from apps.marvins.labels import LabelSelectorError, parse_label_selector

        with self.assertRaisesMessage(
            LabelSelectorError,
            "Invalid label selector 'env=production': labels use a colon (:), not equals (=)",
        ):
            parse_label_selector("env=production")

    def test_parse_label_selector_rejects_equals_in_metadata_selector(self):
        from apps.marvins.labels import LabelSelectorError, parse_label_selector

        expected = (
            "Invalid label selector 'client_id=marvin-kill9': "
            "labels use a colon (:), not equals (=)"
        )
        with self.assertRaisesMessage(LabelSelectorError, expected):
            parse_label_selector("client_id=marvin-kill9,hostname=kill9.eu")

    def test_marvin_matches_labels_exact_match(self):
        from apps.marvins.labels import marvin_matches_labels

        class FakeMarvin:
            labels = ["env:production", "team:platform"]

        selector = {"env": "production", "team": "platform"}
        self.assertTrue(marvin_matches_labels(FakeMarvin(), selector))

    def test_marvin_matches_labels_bare_key(self):
        from apps.marvins.labels import marvin_matches_labels

        class FakeMarvin:
            labels = ["canary", "env:production"]

        selector = {"canary": None}
        self.assertTrue(marvin_matches_labels(FakeMarvin(), selector))

    def test_marvin_matches_labels_missing_key(self):
        from apps.marvins.labels import marvin_matches_labels

        class FakeMarvin:
            labels = ["env:production"]

        selector = {"team": "platform"}
        self.assertFalse(marvin_matches_labels(FakeMarvin(), selector))

    def test_marvin_matches_labels_wrong_value(self):
        from apps.marvins.labels import marvin_matches_labels

        class FakeMarvin:
            labels = ["env:staging"]

        selector = {"env": "production"}
        self.assertFalse(marvin_matches_labels(FakeMarvin(), selector))

    def test_marvin_matches_labels_empty_selector(self):
        from apps.marvins.labels import marvin_matches_labels

        class FakeMarvin:
            labels = ["env:production"]

        self.assertTrue(marvin_matches_labels(FakeMarvin(), {}))

    def test_resolve_marvins_filters_by_capability_and_labels(self):
        from apps.core.models import User
        from apps.marvins.labels import resolve_marvins

        org = Organization.objects.create(name="Resolve", slug="resolve")
        User.objects.create_user(username="resolve", password="pass")

        Marvin = apps.get_model("marvins", "Marvin")
        Capability = apps.get_model("marvins", "Capability")

        cap = Capability.objects.create(
            organization=org, name="restart", description="Restart service"
        )

        marvin_a = Marvin.objects.create(
            organization=org,
            name="Worker A",
            client_id="worker-a",
            status="online",
            labels=["env:production", "team:platform"],
        )
        marvin_a.capabilities.add(cap)

        marvin_b = Marvin.objects.create(
            organization=org,
            name="Worker B",
            client_id="worker-b",
            status="online",
            labels=["env:staging"],
        )
        marvin_b.capabilities.add(cap)

        Marvin.objects.create(
            organization=org,
            name="Worker C",
            client_id="worker-c",
            status="offline",
            labels=["env:production"],
        )

        # Should match only worker A
        result = resolve_marvins(org, "restart", {"env": "production", "team": "platform"})
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].client_id, "worker-a")

    def test_resolve_marvins_without_selector(self):
        from apps.core.models import User
        from apps.marvins.labels import resolve_marvins

        org = Organization.objects.create(name="ResolveAll", slug="resolveall")
        User.objects.create_user(username="resolveall", password="pass")

        Marvin = apps.get_model("marvins", "Marvin")
        Capability = apps.get_model("marvins", "Capability")

        cap = Capability.objects.create(
            organization=org, name="restart", description="Restart service"
        )

        marvin = Marvin.objects.create(
            organization=org,
            name="Worker",
            client_id="worker-1",
            status="online",
        )
        marvin.capabilities.add(cap)

        result = resolve_marvins(org, "restart")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].client_id, "worker-1")

    def test_resolve_marvins_no_match(self):
        from apps.core.models import User
        from apps.marvins.labels import resolve_marvins

        org = Organization.objects.create(name="NoMatch", slug="nomatch")
        User.objects.create_user(username="nomatch", password="pass")

        Marvin = apps.get_model("marvins", "Marvin")
        Capability = apps.get_model("marvins", "Capability")

        cap = Capability.objects.create(
            organization=org, name="restart", description="Restart service"
        )

        marvin = Marvin.objects.create(
            organization=org,
            name="Worker",
            client_id="worker-1",
            status="online",
            labels=["env:staging"],
        )
        marvin.capabilities.add(cap)

        result = resolve_marvins(org, "restart", {"env": "production"})
        self.assertEqual(result, [])


class MarvinViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="marvin_viewer", password="pass")
        self.organization = Organization.objects.create(name="Marvin Viewer", slug="marvin-viewer")
        OrganizationMembership.objects.create(
            user=self.user,
            organization=self.organization,
            role=OrganizationMembership.Role.OWNER,
        )
        self.client.force_login(self.user)

    def test_marvin_detail_view_renders(self):
        Marvin = apps.get_model("marvins", "Marvin")
        marvin = Marvin.objects.create(
            organization=self.organization,
            name="Test Worker",
            client_id="test-worker-1",
            status="online",
        )
        response = self.client.get(
            reverse("marvins:marvin-detail", kwargs={"marvin_id": marvin.id})
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Test Worker")
        self.assertContains(response, "test-worker-1")

    def test_marvin_detail_cross_org_isolation(self):
        Marvin = apps.get_model("marvins", "Marvin")
        other_org = Organization.objects.create(name="Other", slug="other")
        marvin = Marvin.objects.create(
            organization=other_org,
            name="Other Worker",
            client_id="other-worker",
            status="online",
        )
        response = self.client.get(
            reverse("marvins:marvin-detail", kwargs={"marvin_id": marvin.id})
        )
        self.assertEqual(response.status_code, 404)

    def test_marvin_detail_anonymous_user_is_forbidden(self):
        Marvin = apps.get_model("marvins", "Marvin")
        marvin = Marvin.objects.create(
            organization=self.organization,
            name="Anon Worker",
            client_id="anon-worker",
            status="online",
        )
        self.client.logout()
        response = self.client.get(
            reverse("marvins:marvin-detail", kwargs={"marvin_id": marvin.id})
        )
        self.assertEqual(response.status_code, 302)

    def test_marvin_detail_shows_capabilities(self):
        Marvin = apps.get_model("marvins", "Marvin")
        Capability = apps.get_model("marvins", "Capability")
        marvin = Marvin.objects.create(
            organization=self.organization,
            name="Cap Worker",
            client_id="cap-worker",
            status="online",
        )
        cap = Capability.objects.create(
            organization=self.organization,
            name="restart",
            description="Restart service",
        )
        marvin.capabilities.add(cap)
        response = self.client.get(
            reverse("marvins:marvin-detail", kwargs={"marvin_id": marvin.id})
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "restart")

    def test_marvin_detail_shows_tool_calls(self):
        from apps.sessions.models import Thread, ToolCall, TSession

        Marvin = apps.get_model("marvins", "Marvin")
        Capability = apps.get_model("marvins", "Capability")
        marvin = Marvin.objects.create(
            organization=self.organization,
            name="Tool Worker",
            client_id="tool-worker",
            status="online",
        )
        cap = Capability.objects.create(
            organization=self.organization,
            name="check_disk",
            description="Check disk usage",
        )
        marvin.capabilities.add(cap)
        session = TSession.objects.create(
            organization=self.organization,
            title="Tool Session",
            created_by=self.user,
        )
        thread = Thread.objects.create(tsession=session, user=self.user)
        ToolCall.objects.create(
            thread=thread,
            capability=cap,
            marvin=marvin,
            parameters={"path": "/"},
            status=ToolCall.Status.COMPLETED,
        )
        response = self.client.get(
            reverse("marvins:marvin-detail", kwargs={"marvin_id": marvin.id})
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "check_disk")

    def test_resource_list_view_renders(self):
        rt = ResourceType.objects.create(name="vm", display_name="Virtual Machine")
        Resource.objects.create(
            organization=self.organization,
            name="vm-1",
            resource_type=rt,
        )
        response = self.client.get(reverse("marvins:resource-list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "vm-1")

    def test_resource_create_view_renders(self):
        ResourceType.objects.create(name="vm", display_name="Virtual Machine")
        response = self.client.get(reverse("marvins:resource-create"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "New Resource")

    def test_resource_create_creates_resource(self):
        rt = ResourceType.objects.create(name="vm", display_name="Virtual Machine")
        response = self.client.post(
            reverse("marvins:resource-create"),
            {
                "name": "prod-vm",
                "resource_type": str(rt.id),
                "config": '{"ip": "10.0.0.1"}',
                "enabled": "on",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            Resource.objects.filter(organization=self.organization, name="prod-vm").exists()
        )

    def test_resource_edit_updates_resource(self):
        rt = ResourceType.objects.create(name="vm", display_name="Virtual Machine")
        resource = Resource.objects.create(
            organization=self.organization,
            name="vm-1",
            resource_type=rt,
        )
        response = self.client.post(
            reverse("marvins:resource-edit", kwargs={"resource_id": resource.id}),
            {
                "name": "vm-renamed",
                "resource_type": str(rt.id),
                "config": "{}",
            },
        )
        self.assertEqual(response.status_code, 302)
        resource.refresh_from_db()
        self.assertEqual(resource.name, "vm-renamed")

    def test_marvin_config_update_via_post(self):
        marvin = Marvin.objects.create(
            organization=self.organization,
            name="Worker",
            client_id="worker-1",
        )
        response = self.client.post(
            reverse("marvins:marvin-config-update", kwargs={"marvin_id": marvin.id}),
            {
                "global_config": '{"log_level": "DEBUG"}',
                "capability_overrides": '{"k8s": {"timeout": 60}}',
            },
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 200)
        marvin.online_config.refresh_from_db()
        self.assertEqual(marvin.online_config.global_config["log_level"], "DEBUG")
        self.assertEqual(marvin.online_config.capability_overrides["k8s"]["timeout"], 60)

    def test_marvin_capability_override_inline_form(self):
        marvin = Marvin.objects.create(
            organization=self.organization,
            name="Worker",
            client_id="worker-1",
        )
        cap = Capability.objects.create(
            organization=self.organization,
            name="k8s",
            description="Kubernetes",
            json_schema={
                "type": "object",
                "properties": {
                    "timeout": {"type": "integer", "title": "Timeout"},
                    "namespace": {"type": "string", "title": "Namespace"},
                },
            },
        )
        marvin.capabilities.add(cap)

        response = self.client.post(
            reverse("marvins:marvin-config-update", kwargs={"marvin_id": marvin.id}),
            {
                "_capability_name": "k8s",
                "cap_field_timeout": "120",
                "cap_field_type_timeout": "integer",
                "cap_field_namespace": "production",
                "cap_field_type_namespace": "string",
            },
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 200)
        marvin.online_config.refresh_from_db()
        self.assertEqual(marvin.online_config.capability_overrides["k8s"]["timeout"], 120)
        self.assertEqual(
            marvin.online_config.capability_overrides["k8s"]["namespace"], "production"
        )

    def test_marvin_capability_override_clear(self):
        marvin = Marvin.objects.create(
            organization=self.organization,
            name="Worker",
            client_id="worker-1",
        )
        cap = Capability.objects.create(
            organization=self.organization,
            name="k8s",
            description="Kubernetes",
            json_schema={
                "type": "object",
                "properties": {
                    "timeout": {"type": "integer", "title": "Timeout"},
                },
            },
        )
        marvin.capabilities.add(cap)
        marvin.online_config.capability_overrides = {"k8s": {"timeout": 60}}
        marvin.online_config.save()

        response = self.client.post(
            reverse("marvins:marvin-config-update", kwargs={"marvin_id": marvin.id}),
            {
                "_capability_name": "k8s",
                "_clear_override": "1",
            },
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 200)
        marvin.online_config.refresh_from_db()
        self.assertNotIn("k8s", marvin.online_config.capability_overrides)

    def test_marvin_capability_override_boolean_field(self):
        marvin = Marvin.objects.create(
            organization=self.organization,
            name="Worker",
            client_id="worker-1",
        )
        cap = Capability.objects.create(
            organization=self.organization,
            name="restart",
            description="Restart service",
            json_schema={
                "type": "object",
                "properties": {
                    "force": {"type": "boolean", "title": "Force"},
                },
            },
        )
        marvin.capabilities.add(cap)

        response = self.client.post(
            reverse("marvins:marvin-config-update", kwargs={"marvin_id": marvin.id}),
            {
                "_capability_name": "restart",
                "cap_field_force": "true",
                "cap_field_type_force": "boolean",
            },
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 200)
        marvin.online_config.refresh_from_db()
        self.assertTrue(marvin.online_config.capability_overrides["restart"]["force"])

    def test_marvin_attach_resource_htmx(self):
        rt = ResourceType.objects.create(name="vm", display_name="Virtual Machine")
        resource = Resource.objects.create(
            organization=self.organization,
            name="vm-1",
            resource_type=rt,
        )
        marvin = Marvin.objects.create(
            organization=self.organization,
            name="Worker",
            client_id="worker-1",
        )
        response = self.client.post(
            reverse("marvins:marvin-attach-resource", kwargs={"marvin_id": marvin.id}),
            {"resource_id": str(resource.id)},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(resource, marvin.attached_resources.all())

    def test_marvin_detach_resource_htmx(self):
        rt = ResourceType.objects.create(name="vm", display_name="Virtual Machine")
        resource = Resource.objects.create(
            organization=self.organization,
            name="vm-1",
            resource_type=rt,
        )
        marvin = Marvin.objects.create(
            organization=self.organization,
            name="Worker",
            client_id="worker-1",
        )
        marvin.attached_resources.add(resource)
        response = self.client.post(
            reverse("marvins:marvin-detach-resource", kwargs={"marvin_id": marvin.id}),
            {"resource_id": str(resource.id)},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(resource, marvin.attached_resources.all())

    def test_resource_create_view_renders_schema_form(self):
        ResourceType.objects.create(
            name="prometheus",
            display_name="Prometheus Server",
            config_json_schema={
                "type": "object",
                "$schema": "https://json-schema.org",
                "title": "PrometheusConfig",
                "required": ["url"],
                "properties": {
                    "url": {
                        "type": "string",
                        "format": "uri",
                        "description": "The base URL of the Prometheus server.",
                    },
                    "auth": {
                        "type": "object",
                        "properties": {
                            "type": {"type": "string"},
                            "token": {"type": "string"},
                            "username": {"type": "string"},
                            "password": {"type": "string"},
                        },
                    },
                    "timeout": {
                        "type": "string",
                        "description": "Duration string like 30s, 2h45m.",
                    },
                    "guardrails": {
                        "type": "object",
                        "properties": {
                            "min_step": {"type": "string"},
                            "max_query_range": {"type": "string"},
                            "max_response_bytes": {"type": "integer", "minimum": 0},
                        },
                    },
                },
            },
        )
        response = self.client.get(reverse("marvins:resource-create"))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("PrometheusConfig", content)
        self.assertIn("Required", content)
        self.assertIn("auth", content.lower())
        self.assertIn("guardrails", content.lower())
        textarea_match = __import__("re").search(
            r'<textarea[^>]*id="config"[^>]*>(.*?)</textarea>', content, __import__("re").DOTALL
        )
        self.assertIsNotNone(textarea_match)
        self.assertNotIn("<script", textarea_match.group(1))

    def test_resource_edit_view_renders_existing_values(self):
        rt = ResourceType.objects.create(
            name="k8s",
            display_name="Kubernetes",
            config_json_schema={
                "type": "object",
                "properties": {
                    "endpoint": {"type": "string", "title": "Endpoint"},
                    "port": {"type": "integer", "title": "Port"},
                },
            },
        )
        resource = Resource.objects.create(
            organization=self.organization,
            name="k8s-prod",
            resource_type=rt,
            config={"endpoint": "https://k8s.prod", "port": 8443},
        )
        response = self.client.get(
            reverse("marvins:resource-edit", kwargs={"resource_id": resource.id})
        )
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("https://k8s.prod", content)
        self.assertIn("8443", content)

    def test_resource_form_textarea_is_hidden(self):
        ResourceType.objects.create(name="vm", display_name="Virtual Machine")
        response = self.client.get(reverse("marvins:resource-create"))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        textarea_match = __import__("re").search(
            r'<textarea[^>]*id="config"[^>]*>(.*?)</textarea>', content, __import__("re").DOTALL
        )
        self.assertIsNotNone(textarea_match)
        self.assertNotIn("<script", textarea_match.group(1))

    def test_resource_create_post_still_works(self):
        rt = ResourceType.objects.create(name="vm", display_name="Virtual Machine")
        response = self.client.post(
            reverse("marvins:resource-create"),
            {
                "name": "prod-vm",
                "resource_type": str(rt.id),
                "config": '{"ip": "10.0.0.1"}',
                "enabled": "on",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            Resource.objects.filter(organization=self.organization, name="prod-vm").exists()
        )
        resource = Resource.objects.get(organization=self.organization, name="prod-vm")
        self.assertEqual(resource.config, {"ip": "10.0.0.1"})


class MarvinAPITests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="api_marvin", password="pass")
        self.organization = Organization.objects.create(name="API Marvin", slug="api-marvin")
        OrganizationMembership.objects.create(
            user=self.user,
            organization=self.organization,
            role=OrganizationMembership.Role.OWNER,
        )
        self.client.force_login(self.user)

    def test_marvin_list_api_returns_org_marvins(self):
        Marvin = apps.get_model("marvins", "Marvin")
        Marvin.objects.create(
            organization=self.organization,
            name="API Worker",
            client_id="api-worker-1",
            status="online",
        )
        response = self.client.get("/api/marvins/")
        self.assertEqual(response.status_code, 200)
        data = response.json()["results"]
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["name"], "API Worker")

    def test_capability_list_api_returns_org_capabilities(self):
        Capability = apps.get_model("marvins", "Capability")
        Capability.objects.create(
            organization=self.organization,
            name="restart",
            description="Restart service",
        )
        response = self.client.get("/api/capabilities/")
        self.assertEqual(response.status_code, 200)
        data = response.json()["results"]
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["name"], "restart")

    def test_marvin_api_cross_org_isolation(self):
        Marvin = apps.get_model("marvins", "Marvin")
        other_org = Organization.objects.create(name="Other", slug="other")
        Marvin.objects.create(
            organization=other_org,
            name="Other Worker",
            client_id="other-worker",
            status="online",
        )
        response = self.client.get("/api/marvins/")
        self.assertEqual(response.status_code, 200)
        data = response.json()["results"]
        self.assertEqual(len(data), 0)


class ResourceTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="resource_user", password="pass")
        self.organization = Organization.objects.create(name="Resource Org", slug="resource-org")
        OrganizationMembership.objects.create(
            user=self.user,
            organization=self.organization,
            role=OrganizationMembership.Role.OWNER,
        )

    def test_resource_type_creation(self):
        rt = ResourceType.objects.create(
            name="kubernetes",
            display_name="Kubernetes Cluster",
            config_json_schema={"type": "object"},
            capability_names=["kubernetes"],
        )
        self.assertEqual(str(rt), "Kubernetes Cluster")

    def test_resource_creation(self):
        rt = ResourceType.objects.create(
            name="kubernetes",
            display_name="Kubernetes Cluster",
        )
        resource = Resource.objects.create(
            organization=self.organization,
            name="prod-k8s",
            resource_type=rt,
            config={"endpoint": "https://k8s.prod"},
        )
        self.assertEqual(str(resource), "prod-k8s (kubernetes)")
        self.assertTrue(resource.enabled)

    def test_marvin_config_auto_created(self):
        marvin = Marvin.objects.create(
            organization=self.organization,
            name="Worker",
            client_id="worker-1",
        )
        self.assertTrue(hasattr(marvin, "online_config"))
        self.assertIsNotNone(marvin.online_config)

    def test_config_assembly(self):
        from apps.marvins.config_assembly import assemble_marvin_config

        rt = ResourceType.objects.create(
            name="kubernetes",
            display_name="Kubernetes Cluster",
            capability_names=["k8s"],
        )
        resource = Resource.objects.create(
            organization=self.organization,
            name="prod-k8s",
            resource_type=rt,
            config={"endpoint": "https://k8s.prod"},
        )
        marvin = Marvin.objects.create(
            organization=self.organization,
            name="Worker",
            client_id="worker-1",
            capability_configs={"k8s": {"namespace": "default"}},
        )
        marvin.online_config.global_config = {"log_level": "INFO"}
        marvin.online_config.capability_overrides = {"k8s": {"timeout": 30}}
        marvin.online_config.save()
        marvin.attached_resources.add(resource)

        config = assemble_marvin_config(marvin)
        self.assertEqual(config["global"]["log_level"], "INFO")
        self.assertEqual(config["capabilities"]["k8s"]["namespace"], "default")
        self.assertEqual(config["capabilities"]["k8s"]["timeout"], 30)
        self.assertEqual(config["capabilities"]["k8s"]["endpoint"], "https://k8s.prod")
        self.assertEqual(len(config["resources"]), 1)
        self.assertEqual(config["resources"][0]["name"], "prod-k8s")

    def test_resource_attach_detach_api(self):
        rt = ResourceType.objects.create(name="vm", display_name="Virtual Machine")
        resource = Resource.objects.create(
            organization=self.organization,
            name="vm-1",
            resource_type=rt,
        )
        marvin = Marvin.objects.create(
            organization=self.organization,
            name="Worker",
            client_id="worker-1",
        )
        self.client.force_login(self.user)

        response = self.client.post(
            f"/api/resources/{resource.id}/attach_to_marvin/",
            {"marvin_id": str(marvin.id)},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "attached")
        self.assertIn(resource, marvin.attached_resources.all())

        response = self.client.post(
            f"/api/resources/{resource.id}/detach_from_marvin/",
            {"marvin_id": str(marvin.id)},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "detached")
        self.assertNotIn(resource, marvin.attached_resources.all())

    def test_marvin_config_update_api(self):
        marvin = Marvin.objects.create(
            organization=self.organization,
            name="Worker",
            client_id="worker-1",
        )
        self.client.force_login(self.user)

        response = self.client.patch(
            f"/api/marvin-configs/{marvin.online_config.id}/",
            {
                "global_config": {"log_level": "DEBUG"},
                "capability_overrides": {"k8s": {"timeout": 60}},
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["global_config"]["log_level"], "DEBUG")
        self.assertEqual(data["capability_overrides"]["k8s"]["timeout"], 60)


class DiscoveryEngineTests(TransactionTestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Discovery", slug="discovery")
        self.engine = DiscoveryEngine()

    def test_find_capabilities_limits_to_20(self):
        for i in range(25):
            cap = Capability.objects.create(
                organization=self.organization,
                name=f"cap.{i}",
                enabled=True,
            )
            m = Marvin.objects.create(
                organization=self.organization,
                name=f"worker-{i}",
                client_id=f"agent-{i}",
                status="online",
            )
            m.capabilities.add(cap)
        result = self.engine.find_capabilities(self.organization, limit=100)
        self.assertLessEqual(len(result["results"]), 20)

    def test_find_capabilities_returns_dict_shape(self):
        cap = Capability.objects.create(
            organization=self.organization,
            name="test.cap",
            enabled=True,
        )
        m = Marvin.objects.create(
            organization=self.organization,
            name="worker",
            client_id="agent",
            status="online",
        )
        m.capabilities.add(cap)
        result = self.engine.find_capabilities(self.organization, query="test")
        self.assertIn("results", result)
        self.assertIn("total_found", result)
        self.assertIn("search_method", result)

    def test_rank_capabilities_overfetch(self):
        for i in range(10):
            cap = Capability.objects.create(
                organization=self.organization,
                name=f"cap.{i}",
                description=f"Capability {i}",
                enabled=True,
            )
            m = Marvin.objects.create(
                organization=self.organization,
                name=f"worker-{i}",
                client_id=f"agent-{i}",
                status="online",
            )
            m.capabilities.add(cap)
        ordered, method = self.engine._rank_capabilities(
            Capability.objects.filter(organization=self.organization),
            "cap",
            3,
        )
        self.assertGreaterEqual(len(ordered), 3)


class CapabilityModelTests(TransactionTestCase):
    def test_save_builds_search_document(self):
        org = Organization.objects.create(name="Org", slug="org")
        cap = Capability.objects.create(
            organization=org,
            name="network.ping",
            description="Ping a host",
            use_cases=["Check host reachability"],
            aliases=["icmp", "echo"],
            tags=["networking", "diagnostics"],
        )
        self.assertIn("network.ping", cap.search_document)
        self.assertIn("Ping a host", cap.search_document)
        self.assertIn("Check host reachability", cap.search_document)
        self.assertIn("icmp", cap.search_document)
        self.assertIn("networking", cap.search_document)
        self.assertEqual(cap.provider, "network")

    def test_save_sets_needs_re_embedding_on_metadata_change(self):
        org = Organization.objects.create(name="Org", slug="org")
        cap = Capability.objects.create(
            organization=org,
            name="test.cap",
            description="Original",
        )
        self.assertTrue(cap.needs_re_embedding)
        cap.embedding_version = cap.search_document_version
        cap.needs_re_embedding = False
        cap.save(update_fields=["embedding_version", "needs_re_embedding"])
        cap.refresh_from_db()
        self.assertFalse(cap.needs_re_embedding)
        cap.description = "Changed"
        cap.save()
        cap.refresh_from_db()
        self.assertTrue(cap.needs_re_embedding)

    def test_save_clears_needs_re_embedding_after_embed(self):
        org = Organization.objects.create(name="Org", slug="org")
        cap = Capability.objects.create(
            organization=org,
            name="test.cap",
            description="Desc",
        )
        cap.save()
        cap.refresh_from_db()
        self.assertTrue(cap.needs_re_embedding)
        cap.embedding_version = cap.search_document_version
        cap.needs_re_embedding = False
        cap.save(update_fields=["embedding_version", "needs_re_embedding"])
        cap.refresh_from_db()
        self.assertFalse(cap.needs_re_embedding)

    def test_provider_derived_from_name(self):
        org = Organization.objects.create(name="Org", slug="org")
        cap = Capability.objects.create(
            organization=org,
            name="kubernetes.pod.list",
            description="List pods",
        )
        self.assertEqual(cap.provider, "kubernetes")


class EmbeddingPollingTests(TransactionTestCase):
    def test_find_re_embedding_ids(self):
        from services.grpc_server.servicer import _find_re_embedding_ids

        org = Organization.objects.create(name="Org", slug="org")
        cap = Capability.objects.create(
            organization=org,
            name="test.cap",
            description="Desc",
        )
        cap.save()
        cap.refresh_from_db()
        self.assertTrue(cap.needs_re_embedding)
        ids = _find_re_embedding_ids()
        self.assertIn(str(cap.id), ids)

    def test_find_re_embedding_ids_excludes_up_to_date(self):
        from services.grpc_server.servicer import _find_re_embedding_ids

        org = Organization.objects.create(name="Org", slug="org")
        cap = Capability.objects.create(
            organization=org,
            name="test.cap",
            description="Desc",
        )
        cap.embedding_version = cap.search_document_version
        cap.needs_re_embedding = False
        cap.save(update_fields=["embedding_version", "needs_re_embedding"])
        ids = _find_re_embedding_ids()
        self.assertNotIn(str(cap.id), ids)


class RRFFusionTests(TransactionTestCase):
    def test_rrf_fusion_combines_ranks(self):
        engine = DiscoveryEngine()
        lexical = [("cap-a", 1), ("cap-b", 2)]
        vector = [("cap-b", 1), ("cap-c", 2)]
        scores = engine._rrf_fusion(lexical, vector)
        self.assertIn("cap-a", scores)
        self.assertIn("cap-b", scores)
        self.assertIn("cap-c", scores)
        self.assertGreater(scores["cap-b"], scores["cap-a"])

    def test_rrf_fusion_single_source(self):
        engine = DiscoveryEngine()
        scores = engine._rrf_fusion([("cap-a", 1)], [])
        self.assertEqual(len(scores), 1)
        self.assertIn("cap-a", scores)


class VectorSearchFilterTests(TransactionTestCase):
    def test_vector_search_excludes_stale_embeddings(self):
        from unittest.mock import MagicMock, patch

        engine = DiscoveryEngine()
        mock_qs = MagicMock()
        mock_filtered = MagicMock()
        mock_qs.filter.return_value = mock_filtered
        mock_filtered.filter.return_value = mock_filtered
        mock_filtered.annotate.return_value = mock_filtered
        mock_filtered.order_by.return_value = []

        with patch("apps.marvins.discovery.settings") as mock_settings:
            mock_settings.EMBEDDING_MODEL = "test-model"
            engine._vector_search(mock_qs, [0.1, 0.2], 10)

            call_kwargs = mock_qs.filter.call_args.kwargs
            self.assertIn("needs_re_embedding", call_kwargs)
            self.assertFalse(call_kwargs["needs_re_embedding"])
            self.assertIn("embedding__isnull", call_kwargs)
            self.assertFalse(call_kwargs["embedding__isnull"])

    def test_vector_search_filters_by_embedding_model(self):
        from unittest.mock import MagicMock, patch

        engine = DiscoveryEngine()
        mock_qs = MagicMock()
        mock_filtered = MagicMock()
        mock_qs.filter.return_value = mock_filtered
        mock_filtered.filter.return_value = mock_filtered
        mock_filtered.annotate.return_value = mock_filtered
        mock_filtered.order_by.return_value = []

        with patch("apps.marvins.discovery.settings") as mock_settings:
            mock_settings.EMBEDDING_MODEL = "text-embedding-3-small"
            engine._vector_search(mock_qs, [0.1, 0.2], 10)

            model_filter_call = mock_filtered.filter.call_args
            self.assertIn("embedding_model", model_filter_call.kwargs)
            self.assertEqual(model_filter_call.kwargs["embedding_model"], "text-embedding-3-small")


class AvailabilityBoostTests(TransactionTestCase):
    def test_availability_boost_affects_ranking(self):
        from unittest.mock import patch

        org = Organization.objects.create(name="Boost", slug="boost")
        low = Capability.objects.create(organization=org, name="low.check", enabled=True)
        high = Capability.objects.create(organization=org, name="high.check", enabled=True)

        for i in range(2):
            m = Marvin.objects.create(
                organization=org, name=f"w-{i}", client_id=f"a-{i}", status="online"
            )
            m.capabilities.add(low)

        for i in range(10):
            m = Marvin.objects.create(
                organization=org, name=f"w2-{i}", client_id=f"a2-{i}", status="online"
            )
            m.capabilities.add(high)

        engine = DiscoveryEngine()
        with (
            patch.object(
                engine, "_lexical_search", return_value=[(str(low.id), 2), (str(high.id), 2)]
            ),
            patch.object(engine, "_vector_search", return_value=[]),
        ):
            ordered, _ = engine._rank_capabilities(
                Capability.objects.filter(organization=org),
                "check",
                10,
            )
        names = [cap.name for cap, _ in ordered]
        self.assertIn("high.check", names)
        self.assertIn("low.check", names)
        high_idx = names.index("high.check")
        low_idx = names.index("low.check")
        self.assertLess(high_idx, low_idx)


class ExactMatchBoostTests(TransactionTestCase):
    def test_exact_name_match_boosts_to_top(self):
        from unittest.mock import patch

        org = Organization.objects.create(name="Boost", slug="boost")
        exact = Capability.objects.create(
            organization=org, name="kubernetes.pod.logs", enabled=True
        )
        other = Capability.objects.create(organization=org, name="other.cap", enabled=True)

        for i in range(2):
            m = Marvin.objects.create(
                organization=org, name=f"w-{i}", client_id=f"a-{i}", status="online"
            )
            m.capabilities.add(exact)
            m.capabilities.add(other)

        engine = DiscoveryEngine()
        with (
            patch.object(
                engine,
                "_lexical_search",
                return_value=[(str(exact.id), 2), (str(other.id), 1)],
            ),
            patch.object(engine, "_vector_search", return_value=[]),
        ):
            ordered, _ = engine._rank_capabilities(
                Capability.objects.filter(organization=org),
                "kubernetes.pod.logs",
                10,
            )
        names = [cap.name for cap, _ in ordered]
        self.assertEqual(names[0], "kubernetes.pod.logs")


class EmbeddingGeneratorTests(TransactionTestCase):
    def test_generate_for_capability_skips_when_no_search_document(self):
        from unittest.mock import AsyncMock

        from services.embeddings.generator import EmbeddingGenerator

        provider = AsyncMock()
        gen = EmbeddingGenerator(provider)

        org = Organization.objects.create(name="Org", slug="org")
        cap = Capability.objects.create(organization=org, name="test.cap", description="Desc")
        Capability.objects.filter(id=cap.id).update(search_document="")
        cap.refresh_from_db()

        result = asyncio.run(gen.generate_for_capability(cap))
        self.assertFalse(result)
        provider.embed.assert_not_called()

    def test_generate_for_capability_calls_provider_and_saves(self):
        from unittest.mock import AsyncMock

        from services.embeddings.base import EmbeddingResult
        from services.embeddings.generator import EmbeddingGenerator

        provider = AsyncMock()
        provider.embed = AsyncMock(
            return_value=EmbeddingResult(
                embedding=[0.1, 0.2, 0.3], model="test-model", dimensions=3
            )
        )
        gen = EmbeddingGenerator(provider)

        org = Organization.objects.create(name="Org", slug="org")
        cap = Capability.objects.create(organization=org, name="test.cap", description="Desc")
        cap.search_document = "Test document"
        cap.save(update_fields=["search_document"])

        result = asyncio.run(gen.generate_for_capability(cap))
        self.assertTrue(result)
        cap.refresh_from_db()
        self.assertEqual(cap.embedding, [0.1, 0.2, 0.3])
        self.assertEqual(cap.embedding_model, "test-model")
        self.assertEqual(cap.embedding_version, cap.search_document_version)

    def test_generate_batch_processes_multiple(self):
        from unittest.mock import AsyncMock

        from services.embeddings.base import EmbeddingResult
        from services.embeddings.generator import EmbeddingGenerator

        provider = AsyncMock()
        provider.embed_batch = AsyncMock(
            return_value=[
                EmbeddingResult(embedding=[0.1, 0.2], model="test-model", dimensions=2),
                EmbeddingResult(embedding=[0.3, 0.4], model="test-model", dimensions=2),
            ]
        )
        gen = EmbeddingGenerator(provider)

        org = Organization.objects.create(name="Org", slug="org")
        cap1 = Capability.objects.create(organization=org, name="cap.one", description="One")
        cap1.search_document = "Doc one"
        cap1.save(update_fields=["search_document"])
        cap2 = Capability.objects.create(organization=org, name="cap.two", description="Two")
        cap2.search_document = "Doc two"
        cap2.save(update_fields=["search_document"])

        succeeded, failed = asyncio.run(gen.generate_batch([cap1, cap2]))
        self.assertEqual(succeeded, 2)
        self.assertEqual(failed, 0)

        cap1.refresh_from_db()
        cap2.refresh_from_db()
        self.assertEqual(cap1.embedding, [0.1, 0.2])
        self.assertEqual(cap2.embedding, [0.3, 0.4])

    def test_generate_batch_skips_empty_search_document(self):
        from unittest.mock import AsyncMock

        from services.embeddings.generator import EmbeddingGenerator

        provider = AsyncMock()
        gen = EmbeddingGenerator(provider)

        org = Organization.objects.create(name="Org", slug="org")
        cap = Capability.objects.create(organization=org, name="cap.empty", description="Empty")
        # Bypass custom save() which rebuilds search_document
        Capability.objects.filter(id=cap.id).update(search_document="")
        cap.refresh_from_db()

        succeeded, failed = asyncio.run(gen.generate_batch([cap]))
        self.assertEqual(succeeded, 0)
        self.assertEqual(failed, 0)
        provider.embed_batch.assert_not_called()


class EmbedQueryTests(TransactionTestCase):
    def test_embed_query_returns_embedding_on_success(self):
        from unittest.mock import MagicMock, patch

        from services.embeddings.base import EmbeddingResult

        engine = DiscoveryEngine()
        mock_provider = MagicMock()
        mock_provider.embed = AsyncMock(
            return_value=EmbeddingResult(embedding=[0.5, 0.6], model="test-model", dimensions=2)
        )

        with patch(
            "services.embeddings.registry.EmbeddingProviderFactory.get_default_provider",
            return_value=mock_provider,
        ):
            result = engine._embed_query("test query")

        self.assertEqual(result, [0.5, 0.6])

    def test_embed_query_returns_none_on_failure(self):
        from unittest.mock import MagicMock, patch

        engine = DiscoveryEngine()
        mock_provider = MagicMock()
        mock_provider.embed = AsyncMock(side_effect=Exception("provider error"))

        with patch(
            "services.embeddings.registry.EmbeddingProviderFactory.get_default_provider",
            return_value=mock_provider,
        ):
            result = engine._embed_query("test query")

        self.assertIsNone(result)


class VectorFieldTests(TransactionTestCase):
    def test_get_prep_value_empty_string_returns_none(self):
        from apps.marvins.fields import VectorField

        field = VectorField()
        self.assertIsNone(field.get_prep_value(""))
        self.assertIsNone(field.get_prep_value("   "))

    def test_get_prep_value_none_returns_none(self):
        from apps.marvins.fields import VectorField

        field = VectorField()
        self.assertIsNone(field.get_prep_value(None))

    def test_get_prep_value_list_returns_bracket_string(self):
        from apps.marvins.fields import VectorField

        field = VectorField()
        self.assertEqual(field.get_prep_value([0.1, 0.2]), "[0.1,0.2]")

    def test_get_prep_value_string_passthrough(self):
        from apps.marvins.fields import VectorField

        field = VectorField()
        self.assertEqual(field.get_prep_value("[0.1,0.2]"), "[0.1,0.2]")

    def test_to_python_empty_string_returns_none(self):
        from apps.marvins.fields import VectorField

        field = VectorField()
        self.assertIsNone(field.to_python(""))

    def test_to_python_bracket_string_returns_list(self):
        from apps.marvins.fields import VectorField

        field = VectorField()
        self.assertEqual(field.to_python("[0.1,0.2]"), [0.1, 0.2])


class MarvinSerializerTests(TransactionTestCase):
    def test_serializer_separates_labels_from_host_metadata(self):
        from apps.marvins.serializers import MarvinSerializer

        class FakeMarvin:
            id = 1
            organization_id = 1
            organization = None
            name = "Test"
            status = "online"
            labels = ["env:development", "team:platform", "canary"]
            client_id = "test-1"
            hostname = "host1"
            provider = "aws"
            region = "us-east-1"
            availability_zone = "us-east-1a"
            local_ip = "10.0.0.1"
            vm_id = "i-123"
            os = "linux"
            os_version = "22.04"
            arch = "x86_64"
            capabilities = []
            online_config = None
            attached_resources = []
            capability_configs = {}
            created_at = None
            updated_at = None
            last_seen = None

        s = MarvinSerializer(FakeMarvin())
        data = s.data
        self.assertIn("labels", data)
        self.assertIn("host_metadata", data)
        self.assertNotIn("client_id", data)
        self.assertNotIn("hostname", data)
        self.assertEqual(data["labels"], {"env": "development", "team": "platform", "canary": None})
        self.assertEqual(data["host_metadata"]["client_id"], "test-1")
        self.assertEqual(data["host_metadata"]["hostname"], "host1")
