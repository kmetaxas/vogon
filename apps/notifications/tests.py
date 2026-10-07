# pyright: reportAttributeAccessIssue=false

import asyncio
import uuid
from io import StringIO
from unittest.mock import AsyncMock, Mock, patch

from django.core.management import call_command
from django.test import TestCase, TransactionTestCase

from apps.core.models import Organization, OrganizationMembership, User
from apps.notifications.models import (
    Notification,
    NotificationChannel,
    NotificationDelivery,
    NotificationPolicy,
    NotificationProvider,
    NotificationRoute,
)
from services.temporal_workers.activities import get_pending_deliveries
from services.temporal_workers.workflows import NotificationDeliveryWorkflow


def _make_org_user(client, username):
    user = User.objects.create_user(username=username, password="pass")
    org = Organization.objects.create(name=f"{username} Org", slug=f"{username}-org")
    OrganizationMembership.objects.create(
        user=user,
        organization=org,
        role=OrganizationMembership.Role.OWNER,
    )
    client.force_login(user)
    return user, org


def _make_policy(organization, name="Default Policy", is_default=True):
    return NotificationPolicy.objects.create(
        organization=organization,
        name=name,
        is_default=is_default,
        dedup_window_seconds=300,
    )


def _make_channel(organization, name="Slack", provider_type=NotificationChannel.ProviderType.SLACK):
    return NotificationChannel.objects.create(
        organization=organization,
        name=name,
        provider_type=provider_type,
        config={"webhook_name": "test"},
    )


def _make_notification(organization, policy, title="Test Notification"):
    return Notification.objects.create(
        organization=organization,
        policy=policy,
        severity=Notification.Severity.INFO,
        attention=Notification.Attention.NORMAL,
        title=title,
        summary="summary",
        source_type="test",
        dedup_key="dedup-" + str(uuid.uuid4()),
    )


class NotificationAPITests(TestCase):
    def setUp(self):
        self.user, self.organization = _make_org_user(self.client, "notif_user")

    def test_notification_api_list_org_scoped(self):
        policy = _make_policy(self.organization)
        _make_notification(self.organization, policy, title="My Notif")
        other_org = Organization.objects.create(name="Other Org", slug="other-org")
        other_policy = _make_policy(other_org, name="Other Policy")
        _make_notification(other_org, other_policy, title="Other Notif")

        response = self.client.get("/api/notifications/")
        self.assertEqual(response.status_code, 200)
        data = response.json()["results"]
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["title"], "My Notif")

    def test_channel_api_create(self):
        response = self.client.post(
            "/api/notification-channels/",
            {
                "name": "New Channel",
                "provider_type": NotificationChannel.ProviderType.EMAIL,
                "config": {
                    "from_email": "ops@example.com",
                    "recipient_list": ["ops@example.com"],
                },
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertEqual(data["name"], "New Channel")
        self.assertEqual(data["provider_type"], "email")
        channel = NotificationChannel.objects.get(id=data["id"])
        self.assertEqual(channel.organization, self.organization)

    def test_channel_credentials_not_in_response(self):
        channel = _make_channel(self.organization)
        channel.credentials = {"webhook_url": "secret-webhook-url"}
        channel.save(update_fields=["_credentials_encrypted"])

        response = self.client.get(f"/api/notification-channels/{channel.id}/")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertNotIn("_credentials_encrypted", data)
        self.assertNotIn("credentials", data)

    def test_channel_api_write_credentials(self):
        response = self.client.post(
            "/api/notification-channels/",
            {
                "name": "Cred Channel",
                "provider_type": NotificationChannel.ProviderType.SLACK,
                "config": {},
                "credentials": {"webhook_url": "https://hooks.slack.com/services/TEST"},
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201)
        channel = NotificationChannel.objects.get(id=response.json()["id"])
        self.assertEqual(
            channel.credentials, {"webhook_url": "https://hooks.slack.com/services/TEST"}
        )

    def test_policy_api_list_org_scoped(self):
        _make_policy(self.organization, name="My Policy")
        other_org = Organization.objects.create(name="Other Org 2", slug="other-org-2")
        _make_policy(other_org, name="Other Policy")

        response = self.client.get("/api/notification-policies/")
        self.assertEqual(response.status_code, 200)
        data = response.json()["results"]
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["name"], "My Policy")

    def test_route_api_list_org_scoped(self):
        policy = _make_policy(self.organization)
        channel = _make_channel(self.organization)
        NotificationRoute.objects.create(
            policy=policy,
            channel=channel,
            severity=NotificationRoute.Severity.INFO,
            attention=NotificationRoute.Attention.NORMAL,
        )
        other_org = Organization.objects.create(name="Other Org 3", slug="other-org-3")
        other_policy = _make_policy(other_org, name="Other Policy 2")
        other_channel = _make_channel(other_org, name="Other Channel")
        NotificationRoute.objects.create(
            policy=other_policy,
            channel=other_channel,
            severity=NotificationRoute.Severity.CRITICAL,
            attention=NotificationRoute.Attention.IMMEDIATE,
        )

        response = self.client.get("/api/notification-routes/")
        self.assertEqual(response.status_code, 200)
        data = response.json()["results"]
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["severity"], "info")

    @patch("apps.notifications.api_views.NotificationService.create_notification")
    def test_notification_api_create(self, mock_create):
        policy = _make_policy(self.organization)
        mock_create.return_value = Notification.objects.create(
            organization=self.organization,
            policy=policy,
            severity=Notification.Severity.INFO,
            attention=Notification.Attention.NORMAL,
            title="API Notif",
            summary="summary",
            source_type="api",
            dedup_key="dedup-api",
        )

        response = self.client.post(
            "/api/notifications/",
            {
                "severity": "info",
                "attention": "normal",
                "title": "API Notif",
                "summary": "summary",
                "source_type": "api",
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201)
        mock_create.assert_called_once()

    def test_notification_api_requires_authentication(self):
        self.client.logout()
        response = self.client.get("/api/notifications/")
        self.assertIn(response.status_code, (401, 403))

    def test_channel_api_requires_authentication(self):
        self.client.logout()
        response = self.client.get("/api/notification-channels/")
        self.assertIn(response.status_code, (401, 403))

    def test_policy_api_requires_authentication(self):
        self.client.logout()
        response = self.client.get("/api/notification-policies/")
        self.assertIn(response.status_code, (401, 403))

    def test_route_api_requires_authentication(self):
        self.client.logout()
        response = self.client.get("/api/notification-routes/")
        self.assertIn(response.status_code, (401, 403))


class NotificationDeliveryWorkflowTests(TestCase):
    def test_delivery_workflow_processes_deliveries(self):
        workflow_instance = NotificationDeliveryWorkflow()
        execute_activity = AsyncMock(
            side_effect=[
                ["delivery-1", "delivery-2"],
                {"status": "delivered"},
                {"status": "delivered"},
            ]
        )

        with (
            patch(
                "services.temporal_workers.workflows.workflow.execute_activity",
                execute_activity,
            ),
            patch("services.temporal_workers.workflows.workflow.logger", Mock()),
        ):
            result = asyncio.run(workflow_instance.run("notif-1"))

        self.assertEqual(result["notification_id"], "notif-1")
        self.assertEqual(result["delivered_count"], 2)
        self.assertEqual(result["failed_count"], 0)
        names = [call.args[0] for call in execute_activity.await_args_list]
        self.assertEqual(names[0], "get_pending_deliveries")
        self.assertEqual(names.count("execute_delivery"), 2)

    def test_delivery_workflow_continues_on_failure(self):
        workflow_instance = NotificationDeliveryWorkflow()

        async def execute_side_effect(name, *args, **kwargs):
            if name == "get_pending_deliveries":
                return ["delivery-1", "delivery-2", "delivery-3"]
            if name == "execute_delivery":
                delivery_id = kwargs.get("args", [""])[0]
                if delivery_id == "delivery-2":
                    raise RuntimeError("Simulated delivery failure")
                return {"status": "delivered"}
            if name == "update_delivery_status":
                return None
            return {}

        execute_activity = AsyncMock(side_effect=execute_side_effect)

        with (
            patch(
                "services.temporal_workers.workflows.workflow.execute_activity",
                execute_activity,
            ),
            patch("services.temporal_workers.workflows.workflow.logger", Mock()),
        ):
            result = asyncio.run(workflow_instance.run("notif-1"))

        self.assertEqual(result["notification_id"], "notif-1")
        self.assertEqual(result["delivered_count"], 2)
        self.assertEqual(result["failed_count"], 1)
        names = [call.args[0] for call in execute_activity.await_args_list]
        self.assertEqual(names.count("execute_delivery"), 3)
        update_calls = [
            call
            for call in execute_activity.await_args_list
            if call.args[0] == "update_delivery_status"
        ]
        self.assertEqual(len(update_calls), 1)
        self.assertEqual(
            update_calls[0].kwargs["args"], ["delivery-2", "failed", "Simulated delivery failure"]
        )


class GetPendingDeliveriesActivityTests(TransactionTestCase):
    def test_get_pending_deliveries(self):
        User.objects.create_user(username="delivery_user", password="pass")
        organization = Organization.objects.create(name="Delivery Org", slug="delivery-org")
        policy = NotificationPolicy.objects.create(
            organization=organization,
            name="Default",
            is_default=True,
            dedup_window_seconds=300,
        )
        notification = Notification.objects.create(
            organization=organization,
            policy=policy,
            severity=Notification.Severity.INFO,
            attention=Notification.Attention.NORMAL,
            title="Test",
            summary="summary",
            source_type="test",
            dedup_key="dedup-1",
        )
        channel = NotificationChannel.objects.create(
            organization=organization,
            name="Email",
            provider_type=NotificationChannel.ProviderType.EMAIL,
        )
        delivery1 = NotificationDelivery.objects.create(
            notification=notification,
            channel=channel,
            status=NotificationDelivery.Status.PENDING,
        )
        delivery2 = NotificationDelivery.objects.create(
            notification=notification,
            channel=channel,
            status=NotificationDelivery.Status.PENDING,
        )
        NotificationDelivery.objects.create(
            notification=notification,
            channel=channel,
            status=NotificationDelivery.Status.DELIVERED,
        )

        result = asyncio.run(get_pending_deliveries(str(notification.id)))

        self.assertEqual(len(result), 2)
        self.assertIn(str(delivery1.id), result)
        self.assertIn(str(delivery2.id), result)


class NotificationSettingsViewTests(TestCase):
    def setUp(self):
        self.user, self.organization = _make_org_user(self.client, "settings_user")

    def test_settings_page_loads(self):
        response = self.client.get("/notifications/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Notification Settings")

    def test_channel_list_org_scoped(self):
        _make_channel(self.organization, name="Org Channel")
        other_org = Organization.objects.create(name="Other Org", slug="other-org")
        _make_channel(other_org, name="Other Channel")

        response = self.client.get("/notifications/channels/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Org Channel")
        self.assertNotContains(response, "Other Channel")

    def test_channel_create(self):
        response = self.client.post(
            "/notifications/channels/create/",
            {
                "name": "New Channel",
                "provider_type": NotificationChannel.ProviderType.EMAIL,
                "field_recipient_list_item[]": ["ops@example.com"],
                "field_from_email": "ops@example.com",
                "enabled": "1",
            },
        )
        self.assertEqual(response.status_code, 302)
        channel = NotificationChannel.objects.get(name="New Channel")
        self.assertEqual(channel.organization, self.organization)
        self.assertEqual(channel.provider_type, "email")
        self.assertEqual(channel.config["recipient_list"], ["ops@example.com"])
        self.assertEqual(channel.config["from_email"], "ops@example.com")

    def test_channel_create_with_secret_field(self):
        response = self.client.post(
            "/notifications/channels/create/",
            {
                "name": "Slack Channel",
                "provider_type": NotificationChannel.ProviderType.SLACK,
                "field_webhook_url": "https://hooks.slack.com/services/TEST",
                "field_channel_label": "Ops",
                "enabled": "1",
            },
        )
        self.assertEqual(response.status_code, 302)
        channel = NotificationChannel.objects.get(name="Slack Channel")
        self.assertEqual(channel.provider_type, "slack")
        self.assertEqual(
            channel.credentials, {"webhook_url": "https://hooks.slack.com/services/TEST"}
        )
        self.assertEqual(channel.config, {"channel_label": "Ops"})

    def test_channel_create_array_field(self):
        response = self.client.post(
            "/notifications/channels/create/",
            {
                "name": "Multi Email",
                "provider_type": NotificationChannel.ProviderType.EMAIL,
                "field_recipient_list_item[]": ["a@example.com", "b@example.com"],
                "enabled": "1",
            },
        )
        self.assertEqual(response.status_code, 302)
        channel = NotificationChannel.objects.get(name="Multi Email")
        self.assertEqual(channel.config["recipient_list"], ["a@example.com", "b@example.com"])

    def test_channel_create_required_array_missing(self):
        response = self.client.post(
            "/notifications/channels/create/",
            {
                "name": "Bad Email",
                "provider_type": NotificationChannel.ProviderType.EMAIL,
                "enabled": "1",
            },
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "is required")
        self.assertFalse(NotificationChannel.objects.filter(name="Bad Email").exists())

    def test_channel_create_provider_type_swap(self):
        response = self.client.get(
            "/notifications/channels/create/",
            {"provider_type": "slack"},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Webhook URL")

    def test_channel_update_preserves_credentials_when_blank(self):
        channel = NotificationChannel.objects.create(
            organization=self.organization,
            name="Update Me",
            provider_type=NotificationChannel.ProviderType.SLACK,
            config={"channel_label": "Ops"},
        )
        channel.credentials = {"webhook_url": "secret-url"}
        channel.save(update_fields=["_credentials_encrypted"])

        response = self.client.post(
            f"/notifications/channels/{channel.id}/update/",
            {
                "name": "Updated Name",
                "provider_type": NotificationChannel.ProviderType.SLACK,
                "field_channel_label": "NewOps",
                "enabled": "1",
            },
        )
        self.assertEqual(response.status_code, 302)
        channel.refresh_from_db()
        self.assertEqual(channel.name, "Updated Name")
        self.assertEqual(channel.credentials["webhook_url"], "secret-url")
        self.assertEqual(channel.config["channel_label"], "NewOps")

    def test_policy_create(self):
        response = self.client.post(
            "/notifications/policies/create/",
            {
                "name": "My Policy",
                "is_default": "1",
                "dedup_window_seconds": "600",
            },
        )
        self.assertEqual(response.status_code, 302)
        policy = NotificationPolicy.objects.get(name="My Policy")
        self.assertEqual(policy.organization, self.organization)
        self.assertTrue(policy.is_default)
        self.assertEqual(policy.dedup_window_seconds, 600)

    def test_route_config(self):
        policy = _make_policy(self.organization)
        channel = _make_channel(self.organization)

        response = self.client.post(
            f"/notifications/policies/{policy.id}/routes/",
            {
                "severity_new[]": "critical",
                "attention_new[]": "immediate",
                "channel_new[]": str(channel.id),
                "enabled_new[]": "1",
            },
        )
        self.assertEqual(response.status_code, 302)
        route = NotificationRoute.objects.filter(policy=policy).first()
        self.assertIsNotNone(route)
        self.assertEqual(route.severity, "critical")
        self.assertEqual(route.attention, "immediate")
        self.assertEqual(route.channel, channel)
        self.assertTrue(route.enabled)

    def test_route_config_htmx_returns_partial(self):
        policy = _make_policy(self.organization, name="HTMX Policy")
        _make_channel(self.organization)

        response = self.client.get(
            f"/notifications/policies/{policy.id}/routes/",
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Routes for HTMX Policy")
        self.assertContains(response, "<table")
        self.assertNotContains(response, "settings-sidebar")

    def test_route_config_without_htmx_returns_full_page(self):
        policy = _make_policy(self.organization, name="Full Page Policy")
        _make_channel(self.organization)

        response = self.client.get(f"/notifications/policies/{policy.id}/routes/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "settings-sidebar")
        self.assertContains(response, "Notification Settings")

    def test_settings_page_has_channels_and_policies_sections(self):
        response = self.client.get("/notifications/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Channels")
        self.assertContains(response, "Policies")

    def test_route_config_form_has_notification_card_class(self):
        policy = _make_policy(self.organization)
        _make_channel(self.organization)

        response = self.client.get(
            f"/notifications/policies/{policy.id}/routes/",
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "notification-card")


class SyncNotificationProvidersTests(TestCase):
    def test_sync_command_creates_providers(self):
        out = StringIO()
        call_command("sync_notification_providers", stdout=out)
        self.assertIn("Synced", out.getvalue())

        # Verify providers were created
        providers = NotificationProvider.objects.all()
        self.assertGreaterEqual(len(providers), 3)

        provider_types = {p.provider_type for p in providers}
        self.assertIn("email", provider_types)
        self.assertIn("slack", provider_types)
        self.assertIn("teams", provider_types)

    def test_sync_command_is_idempotent(self):
        call_command("sync_notification_providers")
        first_count = NotificationProvider.objects.count()

        call_command("sync_notification_providers")
        second_count = NotificationProvider.objects.count()

        self.assertEqual(first_count, second_count)
