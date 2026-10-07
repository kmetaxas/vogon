import asyncio
import importlib
from unittest.mock import MagicMock, Mock, patch

import pytest
from django.apps import apps
from django.test.utils import override_settings

from services.temporal_workers.activities import execute_delivery, update_delivery_status

notification_service = importlib.import_module("services.notifications.service")
email_provider = importlib.import_module("services.notifications.providers.email")
slack_provider = importlib.import_module("services.notifications.providers.slack")
teams_provider = importlib.import_module("services.notifications.providers.teams")
notification_registry = importlib.import_module("services.notifications.registry")

EmailProvider = email_provider.EmailProvider
SlackProvider = slack_provider.SlackProvider
TeamsProvider = teams_provider.TeamsProvider
NotificationService = notification_service.NotificationService
PolicyResolver = notification_service.PolicyResolver
Router = notification_service.Router
registry = notification_registry.registry

Organization = apps.get_model("core", "Organization")
Notification = apps.get_model("notifications", "Notification")
NotificationChannel = apps.get_model("notifications", "NotificationChannel")
NotificationDelivery = apps.get_model("notifications", "NotificationDelivery")
NotificationPolicy = apps.get_model("notifications", "NotificationPolicy")
NotificationRoute = apps.get_model("notifications", "NotificationRoute")


def make_notification(organization, policy):
    return Notification.objects.create(
        organization=organization,
        policy=policy,
        severity=Notification.Severity.CRITICAL,
        attention=Notification.Attention.IMMEDIATE,
        title="Database down",
        summary="Primary database is unavailable",
        source_type="monitor",
        source_id="db-1",
        dedup_key="db-down",
    )


def make_channel(organization, name):
    return NotificationChannel.objects.create(
        organization=organization,
        name=name,
        provider_type=NotificationChannel.ProviderType.EMAIL,
    )


@pytest.mark.django_db
def test_resolve_named_policy_first():
    organization = Organization.objects.create(name="Acme", slug="acme")
    default_policy = NotificationPolicy.objects.create(
        organization=organization,
        name="default",
        is_default=True,
    )
    named_policy = NotificationPolicy.objects.create(
        organization=organization,
        name="critical",
    )

    assert PolicyResolver.resolve(organization, "critical") == named_policy
    assert PolicyResolver.resolve(organization, "critical") != default_policy


@pytest.mark.django_db
def test_resolve_falls_back_to_default_policy():
    organization = Organization.objects.create(name="Acme", slug="acme")
    default_policy = NotificationPolicy.objects.create(
        organization=organization,
        name="default",
        is_default=True,
    )

    assert PolicyResolver.resolve(organization, "missing") == default_policy
    assert PolicyResolver.resolve(organization) == default_policy


@pytest.mark.django_db
def test_resolve_returns_none_without_policy():
    organization = Organization.objects.create(name="Acme", slug="acme")

    assert PolicyResolver.resolve(organization, "missing") is None


@pytest.mark.django_db
def test_matching_routes_create_deliveries_with_fanout():
    organization = Organization.objects.create(name="Acme", slug="acme")
    policy = NotificationPolicy.objects.create(organization=organization, name="default")
    notification = make_notification(organization, policy)
    critical_channel = make_channel(organization, "Critical Email")
    wildcard_channel = make_channel(organization, "Wildcard Email")
    warning_channel = make_channel(organization, "Warning Email")
    NotificationRoute.objects.create(
        policy=policy,
        severity=NotificationRoute.Severity.CRITICAL,
        attention=NotificationRoute.Attention.IMMEDIATE,
        channel=critical_channel,
    )
    NotificationRoute.objects.create(
        policy=policy,
        severity=None,
        attention=None,
        channel=wildcard_channel,
    )
    NotificationRoute.objects.create(
        policy=policy,
        severity=NotificationRoute.Severity.WARNING,
        attention=None,
        channel=warning_channel,
    )

    deliveries = Router.route(notification, policy)

    assert len(deliveries) == 2
    assert {delivery.channel for delivery in deliveries} == {critical_channel, wildcard_channel}
    assert all(delivery.status == NotificationDelivery.Status.PENDING for delivery in deliveries)
    assert all(delivery.attempt_count == 0 for delivery in deliveries)


@pytest.mark.django_db
def test_disabled_and_non_matching_routes_do_not_create_deliveries():
    organization = Organization.objects.create(name="Acme", slug="acme")
    policy = NotificationPolicy.objects.create(organization=organization, name="default")
    notification = make_notification(organization, policy)
    disabled_channel = make_channel(organization, "Disabled Email")
    normal_channel = make_channel(organization, "Normal Email")
    NotificationRoute.objects.create(
        policy=policy,
        severity=None,
        attention=None,
        channel=disabled_channel,
        enabled=False,
    )
    NotificationRoute.objects.create(
        policy=policy,
        severity=None,
        attention=NotificationRoute.Attention.NORMAL,
        channel=normal_channel,
    )

    deliveries = Router.route(notification, policy)

    assert deliveries == []
    assert not NotificationDelivery.objects.exists()


@pytest.mark.django_db
def test_create_notification_uses_policy_resolver():
    organization = Organization.objects.create(name="Acme", slug="acme")
    policy = NotificationPolicy.objects.create(
        organization=organization,
        name="default",
        is_default=True,
    )

    notification = NotificationService.create_notification(
        organization=organization,
        severity="critical",
        attention="immediate",
        title="Disk full",
        summary="Disk usage exceeded threshold",
        source_type="check",
        policy_name="missing",
    )

    assert notification is not None
    assert notification.policy == policy
    assert len(Notification.objects.filter()) == 1


@pytest.mark.django_db
def test_create_notification_silently_drops_when_policy_missing():
    organization = Organization.objects.create(name="Acme", slug="acme")

    notification = NotificationService.create_notification(
        organization=organization,
        severity="critical",
        attention="immediate",
        title="Disk full",
        summary="Disk usage exceeded threshold",
        source_type="check",
    )

    assert notification is None
    assert len(Notification.objects.filter()) == 0


@pytest.mark.django_db
def test_create_notification_routes_only_non_suppressed_notifications():
    organization = Organization.objects.create(name="Acme", slug="acme")
    policy = NotificationPolicy.objects.create(
        organization=organization,
        name="default",
        is_default=True,
    )
    channel = make_channel(organization, "Email")
    NotificationRoute.objects.create(policy=policy, channel=channel)

    first = NotificationService.create_notification(
        organization=organization,
        policy=policy,
        severity=Notification.Severity.CRITICAL,
        attention=Notification.Attention.IMMEDIATE,
        title="Database down",
        summary="Primary database is unavailable",
        source_type="monitor",
        dedup_key="db-down",
    )
    second = NotificationService.create_notification(
        organization=organization,
        policy=policy,
        severity=Notification.Severity.CRITICAL,
        attention=Notification.Attention.IMMEDIATE,
        title="Database down",
        summary="Primary database is unavailable",
        source_type="monitor",
        dedup_key="db-down",
    )

    assert first is not None
    assert second is not None
    assert NotificationDelivery.objects.filter(notification=first).count() == 1
    assert second.status == Notification.Status.SUPPRESSED
    assert NotificationDelivery.objects.filter(notification=second).count() == 0


@pytest.mark.django_db
@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
def test_email_provider_sends_mail():
    organization = Organization.objects.create(name="Acme", slug="acme")
    policy = NotificationPolicy.objects.create(organization=organization, name="default")
    notification = make_notification(organization, policy)
    channel = NotificationChannel.objects.create(
        organization=organization,
        name="Email",
        provider_type=NotificationChannel.ProviderType.EMAIL,
        config={"recipient_list": ["ops@example.com"]},
    )
    delivery = NotificationDelivery.objects.create(
        notification=notification,
        channel=channel,
    )
    provider = EmailProvider()
    result = provider.send(notification, delivery)
    assert result["status"] == "sent"
    assert result["recipient"] == "ops@example.com"


@pytest.mark.django_db
@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
def test_email_provider_missing_recipient():
    organization = Organization.objects.create(name="Acme", slug="acme")
    policy = NotificationPolicy.objects.create(organization=organization, name="default")
    notification = make_notification(organization, policy)
    channel = NotificationChannel.objects.create(
        organization=organization,
        name="Email",
        provider_type=NotificationChannel.ProviderType.EMAIL,
        config={},
    )
    delivery = NotificationDelivery.objects.create(
        notification=notification,
        channel=channel,
    )
    provider = EmailProvider()
    result = provider.send(notification, delivery)
    assert result["status"] == "failed"
    assert "No recipients" in result["error"]


def test_email_provider_validate_config():
    provider = EmailProvider()
    assert provider.validate_config({}) == ["(root): 'recipient_list' is a required property"]
    assert provider.validate_config({"from_email": "a@b.com"}) == [
        "(root): 'recipient_list' is a required property"
    ]
    assert provider.validate_config({"from_email": 123}) == [
        "(root): 'recipient_list' is a required property",
        "from_email: 123 is not of type 'string'",
    ]
    assert provider.validate_config({"from_email": "a@b.com", "recipient_list": ["x"]}) == []
    assert provider.validate_config({"recipient_list": "x"}) == [
        "recipient_list: 'x' is not of type 'array'"
    ]
    assert provider.display_name == "Email"
    assert provider.config_schema["required"] == ["recipient_list"]


def test_registry_has_email_provider():
    assert "email" in registry.list_providers()
    assert registry.get("email") is EmailProvider


@pytest.mark.django_db
def test_slack_provider_sends_webhook():
    organization = Organization.objects.create(name="Acme", slug="acme")
    policy = NotificationPolicy.objects.create(organization=organization, name="default")
    notification = make_notification(organization, policy)
    channel = NotificationChannel.objects.create(
        organization=organization,
        name="Slack",
        provider_type=NotificationChannel.ProviderType.SLACK,
        config={},
    )
    channel.credentials = {"webhook_url": "https://hooks.slack.com/services/T00/B00/XXX"}
    channel.save()
    delivery = NotificationDelivery.objects.create(
        notification=notification,
        channel=channel,
    )
    provider = SlackProvider()

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_client = MagicMock()
    mock_client.post.return_value = mock_response

    with patch("services.notifications.providers.slack.httpx.Client") as mock_cls:
        mock_cls.return_value.__enter__.return_value = mock_client
        result = provider.send(notification, delivery)

    assert result["status"] == "sent"
    assert result["status_code"] == 200
    mock_client.post.assert_called_once()
    call_args = mock_client.post.call_args
    assert call_args[0][0] == "https://hooks.slack.com/services/T00/B00/XXX"
    payload = call_args[1]["json"]
    assert payload["text"] == notification.title
    assert payload["blocks"][0]["type"] == "header"
    assert payload["blocks"][1]["type"] == "section"


@pytest.mark.django_db
def test_slack_provider_missing_webhook_url():
    organization = Organization.objects.create(name="Acme", slug="acme")
    policy = NotificationPolicy.objects.create(organization=organization, name="default")
    notification = make_notification(organization, policy)
    channel = NotificationChannel.objects.create(
        organization=organization,
        name="Slack",
        provider_type=NotificationChannel.ProviderType.SLACK,
        config={},
    )
    channel.credentials = {}
    channel.save()
    delivery = NotificationDelivery.objects.create(
        notification=notification,
        channel=channel,
    )
    provider = SlackProvider()
    result = provider.send(notification, delivery)
    assert result["status"] == "failed"
    assert "No webhook URL" in result["error"]


@pytest.mark.django_db
def test_slack_provider_retries_then_fails():
    organization = Organization.objects.create(name="Acme", slug="acme")
    policy = NotificationPolicy.objects.create(organization=organization, name="default")
    notification = make_notification(organization, policy)
    channel = NotificationChannel.objects.create(
        organization=organization,
        name="Slack",
        provider_type=NotificationChannel.ProviderType.SLACK,
        config={},
    )
    channel.credentials = {"webhook_url": "https://hooks.slack.com/services/T00/B00/XXX"}
    channel.save()
    delivery = NotificationDelivery.objects.create(
        notification=notification,
        channel=channel,
    )
    provider = SlackProvider()

    mock_response = MagicMock()
    mock_response.status_code = 500
    mock_client = MagicMock()
    mock_client.post.return_value = mock_response

    with patch("services.notifications.providers.slack.httpx.Client") as mock_cls:
        mock_cls.return_value.__enter__.return_value = mock_client
        result = provider.send(notification, delivery)

    assert result["status"] == "failed"
    assert mock_client.post.call_count == 3


def test_slack_provider_validate_config():
    provider = SlackProvider()
    assert provider.validate_config({}) == ["(root): 'webhook_url' is a required property"]
    assert provider.validate_config({"channel_label": "Ops"}) == [
        "(root): 'webhook_url' is a required property"
    ]
    assert (
        provider.validate_config({"webhook_url": "https://hooks.slack.com/services/T00/B00/XXX"})
        == []
    )
    assert provider.display_name == "Slack"
    assert provider.config_schema["properties"]["webhook_url"]["writeOnly"] is True


def test_registry_has_slack_provider():
    assert "slack" in registry.list_providers()
    assert registry.get("slack") is SlackProvider


@pytest.mark.django_db
def test_teams_provider_sends_webhook():
    organization = Organization.objects.create(name="Acme", slug="acme")
    policy = NotificationPolicy.objects.create(organization=organization, name="default")
    notification = make_notification(organization, policy)
    channel = NotificationChannel.objects.create(
        organization=organization,
        name="Teams",
        provider_type=NotificationChannel.ProviderType.TEAMS,
        config={},
    )
    channel.credentials = {"webhook_url": "https://outlook.office.com/webhook/xxx"}
    channel.save()
    delivery = NotificationDelivery.objects.create(
        notification=notification,
        channel=channel,
    )
    provider = TeamsProvider()

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_client = MagicMock()
    mock_client.post.return_value = mock_response

    with patch("services.notifications.providers.teams.httpx.Client") as mock_cls:
        mock_cls.return_value.__enter__.return_value = mock_client
        result = provider.send(notification, delivery)

    assert result["status"] == "sent"
    assert result["status_code"] == 200
    mock_client.post.assert_called_once()
    call_args = mock_client.post.call_args
    assert call_args[0][0] == "https://outlook.office.com/webhook/xxx"
    payload = call_args[1]["json"]
    assert payload["@type"] == "MessageCard"
    assert payload["themeColor"] == "FF0000"
    assert payload["summary"] == notification.title
    section = payload["sections"][0]
    assert section["activityTitle"] == notification.title
    assert "Severity: CRITICAL" in section["activitySubtitle"]
    assert "Attention: IMMEDIATE" in section["activitySubtitle"]


@pytest.mark.django_db
def test_teams_provider_missing_webhook_url():
    organization = Organization.objects.create(name="Acme", slug="acme")
    policy = NotificationPolicy.objects.create(organization=organization, name="default")
    notification = make_notification(organization, policy)
    channel = NotificationChannel.objects.create(
        organization=organization,
        name="Teams",
        provider_type=NotificationChannel.ProviderType.TEAMS,
        config={},
    )
    channel.credentials = {}
    channel.save()
    delivery = NotificationDelivery.objects.create(
        notification=notification,
        channel=channel,
    )
    provider = TeamsProvider()
    result = provider.send(notification, delivery)
    assert result["status"] == "failed"
    assert "No Teams webhook URL" in result["error"]


@pytest.mark.django_db
def test_teams_provider_retries_then_fails():
    organization = Organization.objects.create(name="Acme", slug="acme")
    policy = NotificationPolicy.objects.create(organization=organization, name="default")
    notification = make_notification(organization, policy)
    channel = NotificationChannel.objects.create(
        organization=organization,
        name="Teams",
        provider_type=NotificationChannel.ProviderType.TEAMS,
        config={},
    )
    channel.credentials = {"webhook_url": "https://outlook.office.com/webhook/xxx"}
    channel.save()
    delivery = NotificationDelivery.objects.create(
        notification=notification,
        channel=channel,
    )
    provider = TeamsProvider()

    mock_response = MagicMock()
    mock_response.status_code = 500
    mock_client = MagicMock()
    mock_client.post.return_value = mock_response

    with patch("services.notifications.providers.teams.httpx.Client") as mock_cls:
        mock_cls.return_value.__enter__.return_value = mock_client
        result = provider.send(notification, delivery)

    assert result["status"] == "failed"
    assert mock_client.post.call_count == 3


def test_teams_provider_validate_config():
    provider = TeamsProvider()
    assert provider.validate_config({}) == ["(root): 'webhook_url' is a required property"]
    assert provider.validate_config({"display_name": "Ops"}) == [
        "(root): 'webhook_url' is a required property"
    ]
    assert provider.validate_config({"display_name": 123}) == [
        "(root): 'webhook_url' is a required property",
        "display_name: 123 is not of type 'string'",
    ]
    assert provider.validate_config({"webhook_url": "https://outlook.office.com/webhook/xxx"}) == []
    assert provider.display_name == "Microsoft Teams"
    assert provider.config_schema["properties"]["webhook_url"]["writeOnly"] is True


def test_registry_has_teams_provider():
    assert "teams" in registry.list_providers()
    assert registry.get("teams") is TeamsProvider


@pytest.mark.django_db
def test_renderer_returns_normalized_dict():
    from services.notifications.renderers import NotificationRenderer

    organization = Organization.objects.create(name="Acme", slug="acme")
    policy = NotificationPolicy.objects.create(organization=organization, name="default")
    notification = make_notification(organization, policy)
    result = NotificationRenderer.render(notification, "email")
    assert result["title"] == notification.title
    assert result["summary"] == notification.summary
    assert result["details"] == notification.details
    assert result["severity"] == notification.severity
    assert result["attention"] == notification.attention
    assert result["source_type"] == notification.source_type
    assert result["source_id"] == notification.source_id
    assert result["url"] == f"/notifications/{notification.id}/"


@pytest.mark.django_db
@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
def test_email_provider_uses_renderer():
    organization = Organization.objects.create(name="Acme", slug="acme")
    policy = NotificationPolicy.objects.create(organization=organization, name="default")
    notification = make_notification(organization, policy)
    channel = NotificationChannel.objects.create(
        organization=organization,
        name="Email",
        provider_type=NotificationChannel.ProviderType.EMAIL,
        config={"recipient_list": ["ops@example.com"]},
    )
    delivery = NotificationDelivery.objects.create(
        notification=notification,
        channel=channel,
    )
    provider = EmailProvider()
    with patch("services.notifications.providers.email.NotificationRenderer.render") as mock_render:
        mock_render.return_value = {
            "title": notification.title,
            "summary": notification.summary,
            "details": notification.details,
            "severity": notification.severity,
            "attention": notification.attention,
            "source_type": notification.source_type,
            "source_id": notification.source_id,
            "url": f"/notifications/{notification.id}/",
        }
        result = provider.send(notification, delivery)
    assert result["status"] == "sent"
    mock_render.assert_called_once_with(notification, "email")


@pytest.mark.django_db
def test_teams_provider_uses_renderer():
    organization = Organization.objects.create(name="Acme", slug="acme")
    policy = NotificationPolicy.objects.create(organization=organization, name="default")
    notification = make_notification(organization, policy)
    channel = NotificationChannel.objects.create(
        organization=organization,
        name="Teams",
        provider_type=NotificationChannel.ProviderType.TEAMS,
        config={},
    )
    channel.credentials = {"webhook_url": "https://outlook.office.com/webhook/xxx"}
    channel.save()
    delivery = NotificationDelivery.objects.create(
        notification=notification,
        channel=channel,
    )
    provider = TeamsProvider()
    with patch("services.notifications.providers.teams.NotificationRenderer.render") as mock_render:
        mock_render.return_value = {
            "title": notification.title,
            "summary": notification.summary,
            "details": notification.details,
            "severity": notification.severity,
            "attention": notification.attention,
            "source_type": notification.source_type,
            "source_id": notification.source_id,
            "url": f"/notifications/{notification.id}/",
        }
        with patch("services.notifications.providers.teams.httpx.Client") as mock_cls:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_client = MagicMock()
            mock_client.post.return_value = mock_response
            mock_cls.return_value.__enter__.return_value = mock_client
            result = provider.send(notification, delivery)
    assert result["status"] == "sent"
    mock_render.assert_called_once_with(notification, "teams")


@pytest.mark.django_db
def test_slack_provider_uses_renderer():
    organization = Organization.objects.create(name="Acme", slug="acme")
    policy = NotificationPolicy.objects.create(organization=organization, name="default")
    notification = make_notification(organization, policy)
    channel = NotificationChannel.objects.create(
        organization=organization,
        name="Slack",
        provider_type=NotificationChannel.ProviderType.SLACK,
        config={},
    )
    channel.credentials = {"webhook_url": "https://hooks.slack.com/services/T00/B00/XXX"}
    channel.save()
    delivery = NotificationDelivery.objects.create(
        notification=notification,
        channel=channel,
    )
    provider = SlackProvider()
    with patch("services.notifications.providers.slack.NotificationRenderer.render") as mock_render:
        mock_render.return_value = {
            "title": notification.title,
            "summary": notification.summary,
            "details": notification.details,
            "severity": notification.severity,
            "attention": notification.attention,
            "source_type": notification.source_type,
            "source_id": notification.source_id,
            "url": f"/notifications/{notification.id}/",
        }
        with patch("services.notifications.providers.slack.httpx.Client") as mock_cls:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_client = MagicMock()
            mock_client.post.return_value = mock_response
            mock_cls.return_value.__enter__.return_value = mock_client
            result = provider.send(notification, delivery)
    assert result["status"] == "sent"
    mock_render.assert_called_once_with(notification, "slack")


@pytest.mark.django_db
def test_service_starts_workflow():
    organization = Organization.objects.create(name="Acme", slug="acme")
    policy = NotificationPolicy.objects.create(organization=organization, name="default")
    channel = make_channel(organization, "Email")
    NotificationRoute.objects.create(policy=policy, channel=channel)

    with patch.object(NotificationService, "_start_delivery_workflow") as mock_start:
        notification = NotificationService.create_notification(
            organization=organization,
            policy=policy,
            severity=Notification.Severity.CRITICAL,
            attention=Notification.Attention.IMMEDIATE,
            title="Database down",
            summary="Primary database is unavailable",
            source_type="monitor",
            dedup_key="db-down",
        )

    assert notification is not None
    mock_start.assert_called_once_with(notification)


@pytest.mark.django_db
def test_service_no_workflow_without_deliveries():
    organization = Organization.objects.create(name="Acme", slug="acme")
    policy = NotificationPolicy.objects.create(organization=organization, name="default")

    with patch.object(NotificationService, "_start_delivery_workflow") as mock_start:
        notification = NotificationService.create_notification(
            organization=organization,
            policy=policy,
            severity=Notification.Severity.CRITICAL,
            attention=Notification.Attention.IMMEDIATE,
            title="Database down",
            summary="Primary database is unavailable",
            source_type="monitor",
            dedup_key="db-down",
        )

    assert notification is not None
    mock_start.assert_not_called()


@pytest.mark.django_db
def test_service_handles_temporal_failure():
    organization = Organization.objects.create(name="Acme", slug="acme")
    policy = NotificationPolicy.objects.create(organization=organization, name="default")
    channel = make_channel(organization, "Email")
    NotificationRoute.objects.create(policy=policy, channel=channel)

    with patch.object(
        NotificationService,
        "_start_delivery_workflow",
        side_effect=RuntimeError("Temporal offline"),
    ):
        notification = NotificationService.create_notification(
            organization=organization,
            policy=policy,
            severity=Notification.Severity.CRITICAL,
            attention=Notification.Attention.IMMEDIATE,
            title="Database down",
            summary="Primary database is unavailable",
            source_type="monitor",
            dedup_key="db-down",
        )

    assert notification is not None
    assert Notification.objects.filter(id=notification.id).exists()


@pytest.mark.django_db(transaction=True)
def test_execute_delivery_success():
    organization = Organization.objects.create(name="Acme", slug="acme")
    policy = NotificationPolicy.objects.create(organization=organization, name="default")
    notification = Notification.objects.create(
        organization=organization,
        policy=policy,
        severity=Notification.Severity.CRITICAL,
        attention=Notification.Attention.IMMEDIATE,
        title="Database down",
        summary="Primary database is unavailable",
        source_type="monitor",
        source_id="db-1",
        dedup_key="db-down",
    )
    channel = NotificationChannel.objects.create(
        organization=organization,
        name="Email",
        provider_type=NotificationChannel.ProviderType.EMAIL,
        config={"recipient_list": ["ops@example.com"]},
    )
    delivery = NotificationDelivery.objects.create(
        notification=notification,
        channel=channel,
        status=NotificationDelivery.Status.PENDING,
    )

    mock_provider = Mock()
    mock_provider.send = Mock(return_value={"status": "sent"})
    mock_provider_cls = Mock(return_value=mock_provider)

    with patch.object(registry, "get", return_value=mock_provider_cls):
        result = asyncio.run(execute_delivery(str(delivery.id)))

    delivery.refresh_from_db()
    assert result == {"status": "delivered"}
    assert delivery.status == NotificationDelivery.Status.DELIVERED
    assert delivery.attempt_count == 1
    assert delivery.delivered_at is not None
    mock_provider.send.assert_called_once_with(notification, delivery)


@pytest.mark.django_db(transaction=True)
def test_execute_delivery_skips_non_pending():
    organization = Organization.objects.create(name="Acme", slug="acme")
    policy = NotificationPolicy.objects.create(organization=organization, name="default")
    notification = Notification.objects.create(
        organization=organization,
        policy=policy,
        severity=Notification.Severity.CRITICAL,
        attention=Notification.Attention.IMMEDIATE,
        title="Database down",
        summary="Primary database is unavailable",
        source_type="monitor",
        source_id="db-1",
        dedup_key="db-down",
    )
    channel = NotificationChannel.objects.create(
        organization=organization,
        name="Email",
        provider_type=NotificationChannel.ProviderType.EMAIL,
        config={"recipient_list": ["ops@example.com"]},
    )
    delivery = NotificationDelivery.objects.create(
        notification=notification,
        channel=channel,
        status=NotificationDelivery.Status.DELIVERED,
    )

    result = asyncio.run(execute_delivery(str(delivery.id)))

    delivery.refresh_from_db()
    assert result == {"status": "skipped", "reason": "not_pending"}
    assert delivery.status == NotificationDelivery.Status.DELIVERED
    assert delivery.attempt_count == 0


@pytest.mark.django_db(transaction=True)
def test_execute_delivery_catches_provider_exception():
    organization = Organization.objects.create(name="Acme", slug="acme")
    policy = NotificationPolicy.objects.create(organization=organization, name="default")
    notification = Notification.objects.create(
        organization=organization,
        policy=policy,
        severity=Notification.Severity.CRITICAL,
        attention=Notification.Attention.IMMEDIATE,
        title="Database down",
        summary="Primary database is unavailable",
        source_type="monitor",
        source_id="db-1",
        dedup_key="db-down",
    )
    channel = NotificationChannel.objects.create(
        organization=organization,
        name="Email",
        provider_type=NotificationChannel.ProviderType.EMAIL,
        config={"recipient_list": ["ops@example.com"]},
    )
    delivery = NotificationDelivery.objects.create(
        notification=notification,
        channel=channel,
        status=NotificationDelivery.Status.PENDING,
    )

    mock_provider = Mock()
    mock_provider.send = Mock(side_effect=RuntimeError("Provider exploded"))
    mock_provider_cls = Mock(return_value=mock_provider)

    with patch.object(registry, "get", return_value=mock_provider_cls):
        result = asyncio.run(execute_delivery(str(delivery.id)))

    delivery.refresh_from_db()
    assert result == {"status": "failed", "error": "Provider exploded"}
    assert delivery.status == NotificationDelivery.Status.FAILED
    assert delivery.error_message == "Provider exploded"
    assert delivery.attempt_count == 1


@pytest.mark.django_db(transaction=True)
def test_update_delivery_status_sets_status_and_error():
    organization = Organization.objects.create(name="Acme", slug="acme")
    policy = NotificationPolicy.objects.create(organization=organization, name="default")
    notification = Notification.objects.create(
        organization=organization,
        policy=policy,
        severity=Notification.Severity.CRITICAL,
        attention=Notification.Attention.IMMEDIATE,
        title="Database down",
        summary="Primary database is unavailable",
        source_type="monitor",
        source_id="db-1",
        dedup_key="db-down",
    )
    channel = NotificationChannel.objects.create(
        organization=organization,
        name="Email",
        provider_type=NotificationChannel.ProviderType.EMAIL,
        config={"recipient_list": ["ops@example.com"]},
    )
    delivery = NotificationDelivery.objects.create(
        notification=notification,
        channel=channel,
        status=NotificationDelivery.Status.PENDING,
    )

    asyncio.run(
        update_delivery_status(str(delivery.id), NotificationDelivery.Status.FAILED, "boom")
    )

    delivery.refresh_from_db()
    assert delivery.status == NotificationDelivery.Status.FAILED
    assert delivery.error_message == "boom"
