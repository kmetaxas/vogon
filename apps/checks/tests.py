# pyright: reportAttributeAccessIssue=false, reportCallIssue=false, reportArgumentType=false, reportOptionalMemberAccess=false

import importlib
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from django.core import mail
from django.test import TestCase, override_settings

from apps.checks.evaluation import EvaluationEngine, EvaluationError, HealthState, Severity
from apps.checks.models import (
    Check,
    CheckActionLog,
    CheckExecution,
    CheckHealthState,
    CheckVersion,
)
from apps.core.models import Organization, OrganizationMembership, User


def _make_check(organization, name="Check", **kwargs):
    defaults = {
        "organization": organization,
        "name": name,
        "schedule_type": Check.ScheduleType.INTERVAL,
        "schedule_expression": "60",
        "execution_mode": Check.ExecutionMode.DETERMINISTIC,
    }
    defaults.update(kwargs)
    return Check.objects.create(**defaults)


def _action_dispatcher():
    return importlib.import_module("services.checks.actions").ActionDispatcher


def _retry_manager():
    return importlib.import_module("services.checks.actions").RetryManager


class EvaluationEngineTests(TestCase):
    def test_numeric_comparison_gt_fires(self):
        result = {"cpu_usage": 0.85}
        rules = [
            {
                "type": "numeric_comparison",
                "path": "cpu_usage",
                "operator": "gt",
                "threshold": 0.8,
                "severity": "critical",
            }
        ]
        eval_result = EvaluationEngine.evaluate(result, rules)
        self.assertEqual(eval_result.state, HealthState.CRITICAL)
        self.assertEqual(len(eval_result.findings), 1)
        self.assertEqual(eval_result.findings[0].severity, Severity.CRITICAL)

    def test_numeric_comparison_no_fire(self):
        result = {"cpu_usage": 0.7}
        rules = [
            {
                "type": "numeric_comparison",
                "path": "cpu_usage",
                "operator": "gt",
                "threshold": 0.8,
                "severity": "critical",
            }
        ]
        eval_result = EvaluationEngine.evaluate(result, rules)
        self.assertEqual(eval_result.state, HealthState.HEALTHY)
        self.assertEqual(len(eval_result.findings), 0)

    def test_aggregation_avg_fires(self):
        result = {"nodes": [{"mem": 0.7}, {"mem": 0.9}, {"mem": 0.8}]}
        rules = [
            {
                "type": "aggregation",
                "path": "nodes.*.mem",
                "operator": "avg",
                "threshold": 0.79,
                "comparison_operator": "gt",
                "severity": "critical",
            }
        ]
        eval_result = EvaluationEngine.evaluate(result, rules)
        self.assertEqual(eval_result.state, HealthState.CRITICAL)
        self.assertEqual(len(eval_result.findings), 1)
        self.assertAlmostEqual(eval_result.findings[0].actual, 0.8)

    def test_boolean_expression_fires(self):
        result = {"cpu_usage": 0.9, "memory_usage": 0.95}
        rules = [
            {
                "type": "boolean_expression",
                "expression": "critical_cpu and critical_memory",
                "conditions": [
                    {"path": "cpu_usage", "operator": "gt", "threshold": 0.8},
                    {"path": "memory_usage", "operator": "gt", "threshold": 0.9},
                ],
            }
        ]
        eval_result = EvaluationEngine.evaluate(result, rules)
        self.assertEqual(eval_result.state, HealthState.CRITICAL)
        self.assertEqual(len(eval_result.findings), 1)
        self.assertEqual(eval_result.findings[0].severity, Severity.CRITICAL)

    def test_presence_missing(self):
        result = {"errors": None}
        rules = [{"type": "presence", "path": "errors", "expected": False}]
        eval_result = EvaluationEngine.evaluate(result, rules)
        self.assertEqual(eval_result.state, HealthState.HEALTHY)

    def test_invalid_rule_raises(self):
        result = {}
        rules = [
            {
                "type": "numeric_comparison",
                "path": "missing",
                "operator": "gt",
                "threshold": 0.8,
            }
        ]
        with self.assertRaises(EvaluationError):
            EvaluationEngine.evaluate(result, rules)

    def test_does_not_mutate_input(self):
        result = {"cpu_usage": 0.85}
        original = result.copy()
        rules = [
            {
                "type": "numeric_comparison",
                "path": "cpu_usage",
                "operator": "gt",
                "threshold": 0.8,
            }
        ]
        EvaluationEngine.evaluate(result, rules)
        self.assertEqual(result, original)

    def test_nested_path(self):
        result = {"results": [{"value": {"cpu": 0.9}}]}
        rules = [
            {
                "type": "numeric_comparison",
                "path": "results.0.value.cpu",
                "operator": "gt",
                "threshold": 0.8,
                "severity": "critical",
            }
        ]
        eval_result = EvaluationEngine.evaluate(result, rules)
        self.assertEqual(eval_result.state, HealthState.CRITICAL)


class CheckSchedulerTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Scheduler Org", slug="scheduler-org")

    def test_build_spec_interval(self):
        from services.checks.scheduler import CheckScheduler

        check = _make_check(
            self.organization,
            schedule_type=Check.ScheduleType.INTERVAL,
            schedule_expression="300",
        )

        spec = CheckScheduler._build_spec(check)

        self.assertEqual(spec.intervals[0].every.total_seconds(), 300)

    def test_build_spec_cron(self):
        from services.checks.scheduler import CheckScheduler

        check = _make_check(
            self.organization,
            schedule_type=Check.ScheduleType.CRON,
            schedule_expression="0 */6 * * *",
        )

        spec = CheckScheduler._build_spec(check)

        self.assertEqual(spec.cron_expressions[0], "0 */6 * * *")

    def test_workflow_for_mode(self):
        from services.checks.scheduler import CheckScheduler
        from services.temporal_workers.workflows import (
            AutonomousInvestigationWorkflow,
            CheckWorkflow,
        )

        self.assertEqual(CheckScheduler._workflow_for_mode("deterministic"), CheckWorkflow)
        self.assertEqual(CheckScheduler._workflow_for_mode("ai_assisted"), CheckWorkflow)
        self.assertEqual(
            CheckScheduler._workflow_for_mode("autonomous"),
            AutonomousInvestigationWorkflow,
        )


class ActionDispatcherTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="ad_user", password="pass")
        self.organization = Organization.objects.create(name="AD Org", slug="ad-org")
        OrganizationMembership.objects.create(
            user=self.user,
            organization=self.organization,
            role=OrganizationMembership.Role.OWNER,
        )
        self.check = _make_check(self.organization, name="AD Check")

    def test_dispatch_on_completion(self):
        execution = CheckExecution.objects.create(
            check=self.check,
            execution_status=CheckExecution.ExecutionStatus.COMPLETED,
            health_state=CheckExecution.HealthState.HEALTHY,
        )
        actions = [{"type": "email", "target": "ops@example.com", "condition": "on_completion"}]
        self.check.notification_config = {"actions": actions}
        self.check.save()

        ActionDispatcher = _action_dispatcher()
        logs = ActionDispatcher.dispatch(self.check, execution, [])

        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0].action_type, "email")
        self.assertEqual(logs[0].recipient, "ops@example.com")

    def test_dispatch_on_failure(self):
        execution = CheckExecution.objects.create(
            check=self.check,
            execution_status=CheckExecution.ExecutionStatus.FAILED,
            health_state=CheckExecution.HealthState.UNKNOWN,
        )
        actions = [
            {
                "type": "webhook",
                "target": "https://alert.example.com",
                "condition": "on_failure",
            }
        ]
        self.check.notification_config = {"actions": actions}
        self.check.save()

        ActionDispatcher = _action_dispatcher()
        logs = ActionDispatcher.dispatch(self.check, execution, [])

        self.assertEqual(len(logs), 1)

    def test_dispatch_suppressed_by_cooldown(self):
        execution = CheckExecution.objects.create(
            check=self.check,
            execution_status=CheckExecution.ExecutionStatus.COMPLETED,
            health_state=CheckExecution.HealthState.HEALTHY,
        )
        actions = [
            {
                "type": "email",
                "target": "ops@example.com",
                "condition": "on_completion",
                "cooldown_seconds": 3600,
            }
        ]
        self.check.notification_config = {"actions": actions}
        self.check.save()

        ActionDispatcher = _action_dispatcher()

        ActionDispatcher.dispatch(self.check, execution, [])
        logs = ActionDispatcher.dispatch(self.check, execution, [])

        self.assertEqual(len(logs), 0)

    def test_dispatch_on_severity_threshold(self):
        execution = CheckExecution.objects.create(
            check=self.check,
            execution_status=CheckExecution.ExecutionStatus.COMPLETED,
            health_state=CheckExecution.HealthState.CRITICAL,
        )
        findings = [{"severity": "critical", "message": "CPU high"}]
        actions = [
            {
                "type": "webhook",
                "target": "https://alert.example.com",
                "condition": "on_severity_threshold",
                "severity_threshold": "critical",
            }
        ]
        self.check.notification_config = {"actions": actions}
        self.check.save()

        ActionDispatcher = _action_dispatcher()
        logs = ActionDispatcher.dispatch(self.check, execution, findings)

        self.assertEqual(len(logs), 1)

    def test_retry_manager(self):
        RetryManager = _retry_manager()
        execution = CheckExecution.objects.create(check=self.check)
        action_log = CheckActionLog.objects.create(
            check=self.check,
            execution=execution,
            action_type="email",
            delivery_status=CheckActionLog.DeliveryStatus.FAILED,
            retry_count=0,
        )

        self.assertTrue(RetryManager.should_retry(action_log))
        self.assertEqual(RetryManager.backoff_seconds(0), 5)
        self.assertEqual(RetryManager.backoff_seconds(2), 20)


class CheckAPITests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="check_api_user", password="pass")
        self.organization = Organization.objects.create(name="Check Org", slug="check-org")
        OrganizationMembership.objects.create(
            user=self.user,
            organization=self.organization,
            role=OrganizationMembership.Role.OWNER,
        )
        self.client.force_login(self.user)

    def test_check_list_api_returns_org_checks(self):
        _make_check(self.organization, name="API Check")
        response = self.client.get("/api/checks/")
        self.assertEqual(response.status_code, 200)
        data = response.json()["results"]
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["name"], "API Check")
        self.assertEqual(data[0]["organization_name"], "Check Org")

    def test_check_create_api_sets_organization(self):
        response = self.client.post(
            "/api/checks/",
            {
                "organization": str(self.organization.id),
                "name": "Created Check",
                "schedule_type": Check.ScheduleType.INTERVAL,
                "schedule_expression": "120",
                "execution_mode": Check.ExecutionMode.AI_ASSISTED,
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertEqual(data["name"], "Created Check")
        self.assertEqual(data["organization"], str(self.organization.id))
        self.assertTrue(Check.objects.filter(name="Created Check").exists())

    def test_check_cross_org_isolation(self):
        other_org = Organization.objects.create(name="Other", slug="other-check")
        _make_check(other_org, name="Other Check")
        response = self.client.get("/api/checks/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["results"]), 0)

    @patch("apps.checks.api_views.CheckScheduler.trigger_now", new_callable=AsyncMock)
    def test_check_dry_run_action(self, mock_trigger):
        check = _make_check(self.organization, name="Dry Run Check")
        mock_trigger.return_value = {"workflow_id": "dry", "status": "triggered"}
        response = self.client.post(f"/api/checks/{check.id}/dry_run/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "triggered")
        mock_trigger.assert_called_once_with(check, dry_run=True)

    def test_check_version_list_filtered_by_org(self):
        check = _make_check(self.organization, name="Versioned")
        CheckVersion.objects.create(check=check, version_number=1, definition_snapshot={})
        response = self.client.get("/api/check-versions/")
        self.assertEqual(response.status_code, 200)
        data = response.json()["results"]
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["check_name"], "Versioned")

    def test_check_execution_list_filtered_by_org(self):
        check = _make_check(self.organization, name="Executed")
        CheckExecution.objects.create(check=check)
        response = self.client.get("/api/check-executions/")
        self.assertEqual(response.status_code, 200)
        data = response.json()["results"]
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["check_name"], "Executed")

    def test_check_health_state_list_filtered_by_org(self):
        check = _make_check(self.organization, name="Healthy")
        CheckHealthState.objects.create(check=check)
        response = self.client.get("/api/check-health-states/")
        self.assertEqual(response.status_code, 200)
        data = response.json()["results"]
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["check_name"], "Healthy")

    def test_check_action_log_list_filtered_by_org(self):
        check = _make_check(self.organization, name="Logged")
        CheckActionLog.objects.create(
            check=check,
            action_type=CheckActionLog.ActionType.EMAIL,
            recipient="ops@example.com",
        )
        response = self.client.get("/api/check-action-logs/")
        self.assertEqual(response.status_code, 200)
        data = response.json()["results"]
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["check_name"], "Logged")

    def test_related_endpoints_cross_org_isolation(self):
        other_org = Organization.objects.create(name="Other2", slug="other-check-2")
        other_check = _make_check(other_org, name="Other Related")
        CheckVersion.objects.create(check=other_check, version_number=1, definition_snapshot={})
        CheckExecution.objects.create(check=other_check)
        CheckHealthState.objects.create(check=other_check)
        CheckActionLog.objects.create(
            check=other_check,
            action_type=CheckActionLog.ActionType.WEBHOOK,
        )
        for endpoint in (
            "/api/check-versions/",
            "/api/check-executions/",
            "/api/check-health-states/",
            "/api/check-action-logs/",
        ):
            response = self.client.get(endpoint)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(response.json()["results"]), 0, endpoint)

    def test_check_api_requires_authentication(self):
        self.client.logout()
        response = self.client.get("/api/checks/")
        self.assertIn(response.status_code, (401, 403))


class CheckScheduleLifecycleTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="sched_user", password="pass")
        self.organization = Organization.objects.create(name="Sched Org", slug="sched-org")
        OrganizationMembership.objects.create(
            user=self.user,
            organization=self.organization,
            role=OrganizationMembership.Role.OWNER,
        )
        self.client.force_login(self.user)

    @patch("apps.checks.api_views.CheckScheduler.create_schedule", new_callable=AsyncMock)
    def test_check_create_creates_schedule(self, mock_create):
        mock_create.return_value = {"schedule_id": "check-test", "status": "created"}
        response = self.client.post(
            "/api/checks/",
            {
                "organization": str(self.organization.id),
                "name": "Scheduled Check",
                "schedule_type": Check.ScheduleType.INTERVAL,
                "schedule_expression": "120",
                "execution_mode": Check.ExecutionMode.DETERMINISTIC,
                "enabled": True,
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201)
        mock_create.assert_called_once()

    @patch("apps.checks.api_views.CheckScheduler.pause_schedule", new_callable=AsyncMock)
    def test_disable_action(self, mock_pause):
        check = _make_check(self.organization, name="Disable Me", enabled=True)
        mock_pause.return_value = {"schedule_id": f"check-{check.id}", "status": "paused"}
        response = self.client.post(f"/api/checks/{check.id}/disable/")
        self.assertEqual(response.status_code, 200)
        mock_pause.assert_called_once_with(str(check.id))
        check.refresh_from_db()
        self.assertFalse(check.enabled)

    @patch("apps.checks.api_views.CheckScheduler.resume_schedule", new_callable=AsyncMock)
    def test_enable_action(self, mock_resume):
        check = _make_check(self.organization, name="Enable Me", enabled=False)
        mock_resume.return_value = {"schedule_id": f"check-{check.id}", "status": "resumed"}
        response = self.client.post(f"/api/checks/{check.id}/enable/")
        self.assertEqual(response.status_code, 200)
        mock_resume.assert_called_once_with(str(check.id))
        check.refresh_from_db()
        self.assertTrue(check.enabled)

    @patch("apps.checks.api_views.CheckScheduler.trigger_now", new_callable=AsyncMock)
    def test_trigger_action(self, mock_trigger):
        check = _make_check(self.organization, name="Trigger Me")
        mock_trigger.return_value = {"workflow_id": "test", "status": "triggered"}
        response = self.client.post(f"/api/checks/{check.id}/trigger/")
        self.assertEqual(response.status_code, 200)
        mock_trigger.assert_called_once_with(check)

    @patch("apps.checks.api_views.CheckScheduler.delete_schedule", new_callable=AsyncMock)
    def test_delete_deletes_schedule(self, mock_delete):
        check = _make_check(self.organization, name="Delete Me")
        mock_delete.return_value = {"status": "deleted"}
        response = self.client.delete(f"/api/checks/{check.id}/")
        self.assertEqual(response.status_code, 204)
        mock_delete.assert_called_once_with(str(check.id))
        self.assertFalse(Check.objects.filter(id=check.id).exists())

    @patch("apps.checks.api_views.CheckScheduler.trigger_now", new_callable=AsyncMock)
    def test_trigger_requires_authentication(self, mock_trigger):
        check = _make_check(self.organization, name="Auth Trigger")
        self.client.logout()
        response = self.client.post(f"/api/checks/{check.id}/trigger/")
        self.assertIn(response.status_code, (401, 403))
        mock_trigger.assert_not_called()

    @patch("apps.checks.api_views.CheckScheduler.create_schedule", new_callable=AsyncMock)
    def test_create_survives_temporal_offline(self, mock_create):
        mock_create.side_effect = RuntimeError("temporal offline")
        response = self.client.post(
            "/api/checks/",
            {
                "organization": str(self.organization.id),
                "name": "Offline Check",
                "schedule_type": Check.ScheduleType.INTERVAL,
                "schedule_expression": "120",
                "execution_mode": Check.ExecutionMode.DETERMINISTIC,
                "enabled": True,
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertTrue(Check.objects.filter(name="Offline Check").exists())


class CheckExecutionQueryTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="exec_q_user", password="pass")
        self.organization = Organization.objects.create(name="ExecQ Org", slug="execq-org")
        OrganizationMembership.objects.create(
            user=self.user,
            organization=self.organization,
            role=OrganizationMembership.Role.OWNER,
        )
        self.check = _make_check(self.organization, name="ExecQ Check")

    def test_last_for_check(self):
        CheckExecution.objects.create(
            check=self.check, execution_status=CheckExecution.ExecutionStatus.COMPLETED
        )
        exec2 = CheckExecution.objects.create(
            check=self.check, execution_status=CheckExecution.ExecutionStatus.FAILED
        )
        last = CheckExecution.last_for_check(str(self.check.id))
        self.assertEqual(last.id, exec2.id)

    def test_last_successful_for_check(self):
        CheckExecution.objects.create(
            check=self.check, execution_status=CheckExecution.ExecutionStatus.FAILED
        )
        exec_ok = CheckExecution.objects.create(
            check=self.check, execution_status=CheckExecution.ExecutionStatus.COMPLETED
        )
        last_ok = CheckExecution.last_successful_for_check(str(self.check.id))
        self.assertEqual(last_ok.id, exec_ok.id)

    def test_is_missed_no_executions(self):
        self.assertTrue(CheckExecution.is_missed(self.check, expected_interval_seconds=60))

    def test_is_missed_recent_execution(self):
        CheckExecution.objects.create(
            check=self.check, execution_status=CheckExecution.ExecutionStatus.COMPLETED
        )
        self.assertFalse(CheckExecution.is_missed(self.check, expected_interval_seconds=3600))


class WebhookChannelTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="wh_user", password="pass")
        self.organization = Organization.objects.create(name="WH Org", slug="wh-org")
        OrganizationMembership.objects.create(
            user=self.user,
            organization=self.organization,
            role=OrganizationMembership.Role.OWNER,
        )

    @patch("services.checks.channels.webhook.httpx.Client")
    def test_webhook_success(self, mock_client_cls):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_client = MagicMock()
        mock_client.post.return_value = mock_resp
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_client

        check = _make_check(self.organization, name="WH Check")
        execution = CheckExecution.objects.create(
            check=check,
            execution_status=CheckExecution.ExecutionStatus.COMPLETED,
            health_state=CheckExecution.HealthState.CRITICAL,
        )
        action_log = CheckActionLog.objects.create(
            check=check,
            execution=execution,
            action_type=CheckActionLog.ActionType.WEBHOOK,
            recipient="https://hooks.example.com/alerts",
        )

        from services.checks.channels.webhook import WebhookChannel

        result = WebhookChannel.send(check, execution, action_log)
        self.assertEqual(result["status"], "sent")
        self.assertEqual(result["status_code"], 200)

    @patch("services.checks.channels.webhook.httpx.Client")
    def test_webhook_retry_then_fail(self, mock_client_cls):
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_client = MagicMock()
        mock_client.post.return_value = mock_resp
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_client

        check = _make_check(self.organization, name="WH Check Fail")
        execution = CheckExecution.objects.create(check=check)
        action_log = CheckActionLog.objects.create(
            check=check,
            execution=execution,
            action_type=CheckActionLog.ActionType.WEBHOOK,
            recipient="https://hooks.example.com/alerts",
        )

        from services.checks.channels.webhook import WebhookChannel

        result = WebhookChannel.send(check, execution, action_log)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(mock_client.post.call_count, 3)

    def test_webhook_missing_recipient(self):
        check = _make_check(self.organization, name="WH No URL")
        execution = CheckExecution.objects.create(check=check)
        action_log = CheckActionLog.objects.create(
            check=check,
            execution=execution,
            action_type=CheckActionLog.ActionType.WEBHOOK,
            recipient="",
        )

        from services.checks.channels.webhook import WebhookChannel

        result = WebhookChannel.send(check, execution, action_log)
        self.assertEqual(result["status"], "failed")
        self.assertIn("No webhook URL", result["error"])

    @patch("services.checks.channels.webhook.httpx.Client")
    def test_webhook_handles_connection_error(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client.post.side_effect = httpx.ConnectError("boom")
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_client

        check = _make_check(self.organization, name="WH Conn Err")
        execution = CheckExecution.objects.create(check=check)
        action_log = CheckActionLog.objects.create(
            check=check,
            execution=execution,
            action_type=CheckActionLog.ActionType.WEBHOOK,
            recipient="https://hooks.example.com/alerts",
        )

        from services.checks.channels.webhook import WebhookChannel

        result = WebhookChannel.send(check, execution, action_log)
        self.assertEqual(result["status"], "failed")
        self.assertIn("boom", result["error"])


class EmailChannelTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="email_user", password="pass")
        self.organization = Organization.objects.create(name="Email Org", slug="email-org")
        OrganizationMembership.objects.create(
            user=self.user,
            organization=self.organization,
            role=OrganizationMembership.Role.OWNER,
        )

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_email_channel_sends(self):
        check = _make_check(self.organization, name="Email Check")
        execution = CheckExecution.objects.create(
            check=check,
            execution_status=CheckExecution.ExecutionStatus.COMPLETED,
            health_state=CheckExecution.HealthState.CRITICAL,
        )
        execution.evaluation_result = {
            "findings": [{"severity": "critical", "message": "CPU high"}],
            "summary": "CPU usage exceeded threshold",
        }
        execution.save()
        action_log = CheckActionLog.objects.create(
            check=check,
            execution=execution,
            action_type=CheckActionLog.ActionType.EMAIL,
            recipient="ops@example.com",
        )

        from services.checks.channels.email import EmailChannel

        result = EmailChannel.send(check, execution, action_log)
        self.assertEqual(result["status"], "sent")
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("CRITICAL", mail.outbox[0].subject)
        self.assertIn("Email Check", mail.outbox[0].subject)


class AlertmanagerFormatterTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="am_user", password="pass")
        self.organization = Organization.objects.create(name="AM Org", slug="am-org")
        OrganizationMembership.objects.create(
            user=self.user,
            organization=self.organization,
            role=OrganizationMembership.Role.OWNER,
        )

    def test_format_critical(self):
        check = _make_check(self.organization, name="Disk Check")
        execution = CheckExecution.objects.create(
            check=check,
            execution_status=CheckExecution.ExecutionStatus.COMPLETED,
            health_state=CheckExecution.HealthState.CRITICAL,
        )
        execution.evaluation_result = {
            "findings": [{"severity": "critical", "message": "Disk 95% full"}],
            "summary": "Disk almost full",
        }
        execution.save()

        from services.checks.formatters.alertmanager import AlertmanagerFormatter

        result = AlertmanagerFormatter.format(check, execution)

        self.assertEqual(result["status"], "firing")
        self.assertEqual(len(result["alerts"]), 1)
        alert = result["alerts"][0]
        self.assertEqual(alert["labels"]["severity"], "critical")
        self.assertEqual(alert["labels"]["check_name"], "Disk Check")
        self.assertIn("Disk almost full", alert["annotations"]["summary"])
        self.assertIn("Disk 95% full", alert["annotations"]["description"])
        self.assertEqual(alert["status"], "firing")

    def test_format_healthy(self):
        check = _make_check(self.organization, name="CPU Check")
        execution = CheckExecution.objects.create(
            check=check,
            execution_status=CheckExecution.ExecutionStatus.COMPLETED,
            health_state=CheckExecution.HealthState.HEALTHY,
        )

        from services.checks.formatters.alertmanager import AlertmanagerFormatter

        result = AlertmanagerFormatter.format(check, execution, status="resolved")

        alert = result["alerts"][0]
        self.assertEqual(alert["labels"]["severity"], "info")
        self.assertEqual(alert["status"], "resolved")

    def test_format_batch(self):
        check1 = _make_check(self.organization, name="Check 1")
        check2 = _make_check(self.organization, name="Check 2")
        exec1 = CheckExecution.objects.create(
            check=check1, health_state=CheckExecution.HealthState.CRITICAL
        )
        exec2 = CheckExecution.objects.create(
            check=check2, health_state=CheckExecution.HealthState.DEGRADED
        )

        from services.checks.formatters.alertmanager import AlertmanagerFormatter

        result = AlertmanagerFormatter.format_batch([(check1, exec1), (check2, exec2)])

        self.assertEqual(len(result["alerts"]), 2)
        self.assertEqual(result["alerts"][0]["labels"]["severity"], "critical")
        self.assertEqual(result["alerts"][1]["labels"]["severity"], "warning")


class TeamsChannelTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="teams_user", password="pass")
        self.organization = Organization.objects.create(name="Teams Org", slug="teams-org")
        OrganizationMembership.objects.create(
            user=self.user,
            organization=self.organization,
            role=OrganizationMembership.Role.OWNER,
        )

    @patch("services.checks.channels.teams.httpx.Client")
    def test_teams_card_success(self, mock_client_cls):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_client = MagicMock()
        mock_client.post.return_value = mock_resp
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_client

        check = _make_check(self.organization, name="Teams Check")
        execution = CheckExecution.objects.create(
            check=check,
            execution_status=CheckExecution.ExecutionStatus.COMPLETED,
            health_state=CheckExecution.HealthState.CRITICAL,
        )
        execution.evaluation_result = {
            "findings": [{"severity": "critical", "message": "Disk full"}]
        }
        execution.save()
        action_log = CheckActionLog.objects.create(
            check=check,
            execution=execution,
            action_type=CheckActionLog.ActionType.TEAMS,
            recipient="https://teams.webhook.example.com",
        )

        from services.checks.channels.teams import TeamsChannel

        result = TeamsChannel.send(check, execution, action_log)
        self.assertEqual(result["status"], "sent")

        call_args = mock_client.post.call_args
        payload = call_args.kwargs.get("json", call_args[1].get("json"))
        self.assertEqual(payload["@type"], "MessageCard")
        self.assertEqual(payload["themeColor"], "FF0000")
        self.assertIn("Check Alert: Teams Check", payload["summary"])

    def test_teams_no_webhook_url(self):
        check = _make_check(self.organization, name="Teams No URL")
        execution = CheckExecution.objects.create(check=check)
        action_log = CheckActionLog.objects.create(
            check=check,
            execution=execution,
            action_type=CheckActionLog.ActionType.TEAMS,
            recipient="",
        )

        from services.checks.channels.teams import TeamsChannel

        result = TeamsChannel.send(check, execution, action_log)
        self.assertEqual(result["status"], "failed")
        self.assertIn("No Teams webhook URL", result["error"])
