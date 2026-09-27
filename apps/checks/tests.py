# pyright: reportAttributeAccessIssue=false, reportCallIssue=false, reportArgumentType=false

from django.test import TestCase

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

    def test_check_dry_run_action(self):
        check = _make_check(self.organization, name="Dry Run Check")
        response = self.client.post(f"/api/checks/{check.id}/dry_run/")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "dry_run_triggered")
        self.assertEqual(data["check_id"], str(check.id))

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
