# pyright: reportAttributeAccessIssue=false, reportCallIssue=false, reportArgumentType=false

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, Mock, patch

from django.test import SimpleTestCase, TestCase
from django.test.testcases import TransactionTestCase
from django.urls import reverse
from django.utils import timezone
from openai import APIError, APITimeoutError

from apps.core.models import Organization, OrganizationMembership, User
from apps.llm.models import LLMProvider
from apps.sessions.models import (
    AgentEvent,
    ArchitectureRequest,
    Execution,
    Message,
    Thread,
    ThreadMembership,
    ToolCall,
    TSession,
)
from services.llm.base import LLMResponse
from services.temporal_workers.activities import (
    _create_executions,
    _route_and_execute,
    build_llm_context,
    call_llm,
    create_assistant_message,
    create_tool_call_messages,
    execute_llm_tool,
    initialize_session,
    record_agent_event,
    set_session_status,
)
from services.temporal_workers.workflows import (
    AutonomousInvestigationWorkflow,
    TroubleshootWorkflow,
)


class SessionViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="alice", password="pass")
        self.organization = Organization.objects.create(name="Acme", slug="acme")
        OrganizationMembership.objects.create(
            user=self.user,
            organization=self.organization,
            role=OrganizationMembership.Role.OWNER,
        )
        self.client.force_login(self.user)

    @patch("apps.sessions.views.start_troubleshoot_workflow_sync")
    def test_session_create_starts_temporal_workflow(self, start_workflow):
        response = self.client.post(
            reverse("sessions:session-create"),
            {"title": "Investigate outage"},
        )

        session = TSession.objects.get(title="Investigate outage")
        thread = Thread.objects.get(tsession=session, user=self.user)

        self.assertRedirects(
            response,
            reverse("sessions:session-detail", kwargs={"session_id": session.id}),
        )
        self.assertEqual(session.temporal_workflow_id, f"tsession-{session.id}")
        start_workflow.assert_called_once_with(str(session.id), str(thread.id))

    @patch("apps.sessions.views.logger.exception")
    @patch(
        "apps.sessions.views.start_troubleshoot_workflow_sync", side_effect=RuntimeError("offline")
    )
    def test_session_create_survives_temporal_failure(
        self,
        _start_workflow,
        log_exception,
    ):
        response = self.client.post(reverse("sessions:session-create"), {"title": "Temporal down"})

        session = TSession.objects.get(title="Temporal down")

        self.assertRedirects(
            response,
            reverse("sessions:session-detail", kwargs={"session_id": session.id}),
        )
        self.assertEqual(session.temporal_workflow_id, f"tsession-{session.id}")
        log_exception.assert_called_once()

    @patch("apps.sessions.views.send_message_to_workflow_sync")
    def test_send_message_signals_temporal_without_placeholder_assistant_message(
        self,
        send_message,
    ):
        session = TSession.objects.create(
            organization=self.organization,
            title="Chat session",
            status=TSession.Status.ACTIVE,
            created_by=self.user,
        )
        thread = Thread.objects.create(tsession=session, user=self.user)

        response = self.client.post(
            reverse(
                "sessions:thread-send-message",
                kwargs={"session_id": session.id, "thread_id": thread.id},
            ),
            {"content": "Check DNS"},
        )

        messages = list(Message.objects.filter(thread__tsession=session))

        self.assertRedirects(
            response,
            reverse("sessions:session-detail", kwargs={"session_id": session.id}),
        )
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].role, Message.Role.USER)
        self.assertEqual(messages[0].content, "Check DNS")
        send_message.assert_called_once_with(
            str(session.id),
            {
                "content": "Check DNS",
                "role": Message.Role.USER,
                "thread_id": str(thread.id),
                "is_continue": False,
            },
        )

    @patch("apps.sessions.views.logger.exception")
    @patch("apps.sessions.views.send_message_to_workflow_sync", side_effect=RuntimeError("offline"))
    def test_send_message_survives_temporal_failure(
        self,
        _send_message,
        log_exception,
    ):
        session = TSession.objects.create(
            organization=self.organization,
            title="Failure tolerant",
            status=TSession.Status.ACTIVE,
            created_by=self.user,
        )
        thread = Thread.objects.create(tsession=session, user=self.user)

        response = self.client.post(
            reverse(
                "sessions:thread-send-message",
                kwargs={"session_id": session.id, "thread_id": thread.id},
            ),
            {"content": "Check logs"},
        )

        self.assertRedirects(
            response,
            reverse("sessions:session-detail", kwargs={"session_id": session.id}),
        )
        self.assertEqual(
            Message.objects.filter(thread__tsession=session, role=Message.Role.USER).count(),
            1,
        )
        self.assertEqual(
            Message.objects.filter(thread__tsession=session, role=Message.Role.ASSISTANT).count(),
            0,
        )
        log_exception.assert_called_once()

    def test_session_create_with_provider_from_web_form(self):
        provider = LLMProvider.objects.create(
            organization=self.organization,
            name="Custom",
            provider_type=LLMProvider.ProviderType.OLLAMA,
            model="custom-model",
        )
        response = self.client.post(
            reverse("sessions:session-create"),
            {"title": "Web Form Session", "llm_provider": str(provider.id)},
        )
        session = TSession.objects.get(title="Web Form Session")
        self.assertEqual(session.llm_provider_id, provider.id)
        self.assertRedirects(
            response,
            reverse("sessions:session-detail", kwargs={"session_id": session.id}),
        )

    def test_session_create_with_invalid_provider_ignored(self):
        response = self.client.post(
            reverse("sessions:session-create"),
            {"title": "Invalid Provider Session", "llm_provider": "not-a-uuid"},
        )
        session = TSession.objects.get(title="Invalid Provider Session")
        self.assertIsNone(session.llm_provider_id)
        self.assertRedirects(
            response,
            reverse("sessions:session-detail", kwargs={"session_id": session.id}),
        )

    def test_send_message_updates_session_provider(self):
        from unittest.mock import patch

        default_provider = LLMProvider.objects.create(
            organization=self.organization,
            name="Default",
            provider_type=LLMProvider.ProviderType.OLLAMA,
            model="default-model",
            is_default=True,
        )
        new_provider = LLMProvider.objects.create(
            organization=self.organization,
            name="Better",
            provider_type=LLMProvider.ProviderType.OPENAI_COMPAT,
            model="better-model",
        )
        session = TSession.objects.create(
            organization=self.organization,
            title="Switch Model",
            status=TSession.Status.ACTIVE,
            created_by=self.user,
            llm_provider=default_provider,
        )
        thread = Thread.objects.create(tsession=session, user=self.user)

        with patch("apps.sessions.views.send_message_to_workflow_sync") as send_message:
            response = self.client.post(
                reverse(
                    "sessions:thread-send-message",
                    kwargs={"session_id": session.id, "thread_id": thread.id},
                ),
                {"content": "Hello with better model", "llm_provider": str(new_provider.id)},
            )

        session.refresh_from_db()
        self.assertEqual(session.llm_provider_id, new_provider.id)
        self.assertRedirects(
            response,
            reverse("sessions:session-detail", kwargs={"session_id": session.id}),
        )
        send_message.assert_called_once()

    def test_session_detail_passes_providers_to_template(self):
        session = TSession.objects.create(
            organization=self.organization,
            title="Detail Context",
            status=TSession.Status.ACTIVE,
            created_by=self.user,
        )
        Thread.objects.create(tsession=session, user=self.user)
        LLMProvider.objects.create(
            organization=self.organization,
            name="Visible",
            provider_type=LLMProvider.ProviderType.OLLAMA,
            model="visible-model",
        )

        response = self.client.get(
            reverse("sessions:session-detail", kwargs={"session_id": session.id})
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("llm_providers", response.context)
        self.assertEqual(len(response.context["llm_providers"]), 1)

    def test_session_status_view_renders_badge_partial(self):
        session = TSession.objects.create(
            organization=self.organization,
            title="Status badge",
            status=TSession.Status.ACTIVE,
            created_by=self.user,
        )

        response = self.client.get(
            reverse("sessions:session-status", kwargs={"session_id": session.id}),
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "sessions/_status_badge.html")
        self.assertContains(response, "badge-active")
        self.assertContains(response, "active")

    @patch("apps.sessions.views.send_message_to_workflow_sync")
    def test_send_message_htmx_returns_partial(self, send_message):
        session = TSession.objects.create(
            organization=self.organization,
            title="HTMX chat",
            status=TSession.Status.ACTIVE,
            created_by=self.user,
        )
        thread = Thread.objects.create(tsession=session, user=self.user)

        response = self.client.post(
            reverse(
                "sessions:thread-send-message",
                kwargs={"session_id": session.id, "thread_id": thread.id},
            ),
            {"content": "Hello via HTMX"},
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 204)
        self.assertTrue(
            Message.objects.filter(
                thread=thread,
                role=Message.Role.USER,
                content="Hello via HTMX",
            ).exists()
        )

    @patch("apps.sessions.views.complete_session_workflow_sync")
    def test_session_complete_marks_completed_and_returns_badge_partial(self, complete_workflow):
        session = TSession.objects.create(
            organization=self.organization,
            title="Done session",
            status=TSession.Status.ACTIVE,
            created_by=self.user,
        )

        response = self.client.post(
            reverse("sessions:session-complete", kwargs={"session_id": session.id}),
            HTTP_HX_REQUEST="true",
        )

        session.refresh_from_db()

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "sessions/_status_badge.html")
        self.assertEqual(session.status, TSession.Status.COMPLETED)
        self.assertContains(response, "badge-completed")
        complete_workflow.assert_called_once_with(str(session.id))

    @patch("apps.sessions.views.complete_session_workflow_sync")
    def test_session_complete_htmx_returns_completed_thread_pane_for_creator(
        self, complete_workflow
    ):
        session = TSession.objects.create(
            organization=self.organization,
            title="Close from page",
            status=TSession.Status.ACTIVE,
            created_by=self.user,
        )
        Thread.objects.create(tsession=session, user=self.user)

        response = self.client.post(
            reverse("sessions:session-complete", kwargs={"session_id": session.id}),
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "sessions/_thread_pane.html")
        self.assertContains(response, "Session completed. No further messages can be sent.")
        complete_workflow.assert_called_once_with(str(session.id))

    @patch("apps.sessions.views.complete_session_workflow_sync")
    def test_session_complete_forbidden_for_non_creator(self, complete_workflow):
        other = User.objects.create_user(username="bob", password="pass")
        OrganizationMembership.objects.create(
            user=other,
            organization=self.organization,
            role=OrganizationMembership.Role.MEMBER,
        )
        session = TSession.objects.create(
            organization=self.organization,
            title="Creator only",
            status=TSession.Status.ACTIVE,
            created_by=self.user,
        )
        self.client.force_login(other)

        response = self.client.post(
            reverse("sessions:session-complete", kwargs={"session_id": session.id}),
        )

        session.refresh_from_db()
        self.assertEqual(response.status_code, 403)
        self.assertEqual(session.status, TSession.Status.ACTIVE)
        complete_workflow.assert_not_called()

    @patch("apps.sessions.views.complete_session_workflow_sync")
    def test_session_complete_marks_pending_tool_calls_failed(self, _complete_workflow):
        session = TSession.objects.create(
            organization=self.organization,
            title="Tool cleanup",
            status=TSession.Status.ACTIVE,
            created_by=self.user,
        )
        thread = Thread.objects.create(tsession=session, user=self.user)
        pending_tc = ToolCall.objects.create(
            thread=thread, status=ToolCall.Status.PENDING, parameters={"cmd": "ls"}
        )
        in_progress_tc = ToolCall.objects.create(
            thread=thread, status=ToolCall.Status.IN_PROGRESS, parameters={"cmd": "df"}
        )
        completed_tc = ToolCall.objects.create(
            thread=thread, status=ToolCall.Status.COMPLETED, parameters={"cmd": "ps"}
        )

        self.client.post(
            reverse("sessions:session-complete", kwargs={"session_id": session.id}),
            HTTP_HX_REQUEST="true",
        )

        pending_tc.refresh_from_db()
        in_progress_tc.refresh_from_db()
        completed_tc.refresh_from_db()
        self.assertEqual(pending_tc.status, ToolCall.Status.FAILED)
        self.assertEqual(in_progress_tc.status, ToolCall.Status.FAILED)
        self.assertEqual(completed_tc.status, ToolCall.Status.COMPLETED)

    @patch("apps.sessions.views.logger.exception")
    @patch(
        "apps.sessions.views.complete_session_workflow_sync", side_effect=RuntimeError("offline")
    )
    def test_session_complete_survives_temporal_failure(
        self,
        _complete_workflow,
        log_exception,
    ):
        session = TSession.objects.create(
            organization=self.organization,
            title="Offline completion",
            status=TSession.Status.ACTIVE,
            created_by=self.user,
        )

        response = self.client.post(
            reverse("sessions:session-complete", kwargs={"session_id": session.id}),
        )

        session.refresh_from_db()

        self.assertRedirects(
            response,
            reverse("sessions:session-detail", kwargs={"session_id": session.id}),
        )
        self.assertEqual(session.status, TSession.Status.COMPLETED)
        log_exception.assert_called_once()


class TroubleshootWorkflowTests(TestCase):
    def test_run_creates_assistant_message_after_user_signal(self):
        workflow_instance = TroubleshootWorkflow()

        async def fake_wait_condition(predicate):
            if not predicate():
                await workflow_instance.user_message(
                    {"content": "Check disk", "role": Message.Role.USER}
                )

        execute_activity = AsyncMock(
            side_effect=[
                {
                    "session_id": "session-1",
                    "thread_id": "thread-1",
                    "status": "initialized",
                },
                {"session_id": "session-1", "status": "active"},
                {"messages": [{"role": "user", "content": "Check disk"}]},
                None,
                {"content": "Disk usage is at 95%.", "tool_calls": []},
                {"message_id": "message-1", "thread_id": "thread-1"},
                True,
            ]
        )

        with (
            patch(
                "services.temporal_workers.workflows.workflow.execute_activity",
                execute_activity,
            ),
            patch(
                "services.temporal_workers.workflows.workflow.wait_condition",
                AsyncMock(side_effect=fake_wait_condition),
            ),
            patch("services.temporal_workers.workflows.workflow.logger", Mock()),
        ):
            state = asyncio.run(workflow_instance.run("session-1", "thread-1"))

        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["processed_messages"], 1)
        self.assertEqual(state["last_message"]["content"], "Check disk")
        # Activity order: initialize, set_session_status(active), build_llm_context,
        # record_agent_event(thinking), call_llm, create_assistant_message
        names = [call.args[0] for call in execute_activity.await_args_list]
        self.assertIn("build_llm_context", names)
        self.assertIn("call_llm", names)
        self.assertIn("create_assistant_message", names)
        create_calls = [
            call
            for call in execute_activity.await_args_list
            if call.args[0] == "create_assistant_message"
        ]
        self.assertEqual(
            create_calls[0].kwargs["args"],
            ["thread-1", "Disk usage is at 95%.", 0, 0, "0.00", ""],
        )

    def test_run_routes_signaled_message_to_its_thread(self):
        workflow_instance = TroubleshootWorkflow()

        async def fake_wait_condition(predicate):
            if not predicate():
                await workflow_instance.user_message(
                    {
                        "content": "Check slashdot.org",
                        "role": Message.Role.USER,
                        "thread_id": "thread-2",
                    }
                )

        execute_activity = AsyncMock(
            side_effect=[
                {
                    "session_id": "session-1",
                    "thread_id": "thread-1",
                    "status": "initialized",
                },
                {"session_id": "session-1", "status": "active"},
                {"messages": [{"role": "user", "content": "Check slashdot.org"}]},
                None,
                {"content": "slashdot.org is reachable.", "tool_calls": []},
                {"message_id": "message-2", "thread_id": "thread-2"},
                True,
            ]
        )

        with (
            patch(
                "services.temporal_workers.workflows.workflow.execute_activity",
                execute_activity,
            ),
            patch(
                "services.temporal_workers.workflows.workflow.wait_condition",
                AsyncMock(side_effect=fake_wait_condition),
            ),
            patch("services.temporal_workers.workflows.workflow.logger", Mock()),
        ):
            asyncio.run(workflow_instance.run("session-1", "thread-1"))

        names = [call.args[0] for call in execute_activity.await_args_list]
        build_idx = names.index("build_llm_context")
        create_idx = names.index("create_assistant_message")
        self.assertEqual(
            execute_activity.await_args_list[build_idx].kwargs["args"],
            [
                "thread-2",
                {"content": "Check slashdot.org", "role": "user", "thread_id": "thread-2"},
            ],
        )
        self.assertEqual(
            execute_activity.await_args_list[create_idx].kwargs["args"],
            ["thread-2", "slashdot.org is reachable.", 0, 0, "0.00", ""],
        )


class AutonomousInvestigationWorkflowTests(SimpleTestCase):
    def _check_context(self, **overrides):
        context = {
            "check_name": "API Health",
            "investigation_goal": "Verify API health and gather evidence before deciding state",
            "instructions": "Use metrics first, then return the final JSON evaluation.",
            "execution_budget": {"max_executions_per_session": 4},
            "llm_provider_id": "provider-auto-1",
            "organization_id": "org-auto-1",
            "version_snapshot": {"version": "v1"},
            "target_scope_id": "scope-1",
        }
        context.update(overrides)
        return context

    def _tool_call(self):
        return {
            "id": "tool-call-1",
            "name": "query_prometheus",
            "arguments": {"query": "up"},
        }

    def test_autonomous_run_executes_tools_and_returns_structured_evaluation(self):
        workflow_instance = AutonomousInvestigationWorkflow()
        tool_call = self._tool_call()
        tool_result = {"status": "up", "value": 1}
        execute_activity = AsyncMock(
            side_effect=[
                self._check_context(),
                {"id": "execution-1"},
                {"session_id": "session-1", "thread_id": "thread-1"},
                None,
                {
                    "content": "Need current metric evidence.",
                    "tool_calls": [tool_call],
                    "reasoning": "Check the up metric before evaluating.",
                    "input_tokens": 7,
                    "output_tokens": 5,
                    "cost": "0.02",
                },
                None,
                None,
                None,
                {"db_tool_call_id": "db-tool-1", "result": tool_result},
                None,
                {"messages_created": True},
                None,
                None,
                {
                    "content": '{"state":"healthy","confidence":0.9,"findings":[],"summary":"OK"}',
                    "tool_calls": [],
                    "input_tokens": 11,
                    "output_tokens": 4,
                    "cost": "0.03",
                },
                {"id": "execution-1"},
                {"session_id": "session-1", "status": "completed"},
                {"dispatched": True},
            ]
        )

        with (
            patch(
                "services.temporal_workers.workflows.workflow.execute_activity",
                execute_activity,
            ),
            patch("services.temporal_workers.workflows.workflow.logger", Mock()),
        ):
            result = asyncio.run(workflow_instance.run("check-1", "version-1"))

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["execution_id"], "execution-1")
        self.assertEqual(result["session_id"], "session-1")
        self.assertEqual(result["thread_id"], "thread-1")
        self.assertEqual(result["iteration_count"], 1)
        self.assertEqual(result["cumulative_tokens"], 27)
        self.assertEqual(result["cumulative_cost"], "0.05")
        self.assertEqual(result["evaluation"]["state"], "healthy")
        self.assertNotIn("capability_results", result)
        self.assertEqual(result["investigation_results"][0]["iteration"], 0)
        self.assertEqual(result["investigation_results"][0]["tool_calls"][0]["result"], tool_result)
        names = [call.args[0] for call in execute_activity.await_args_list]
        self.assertEqual(
            names[:3], ["load_check_context", "update_check_execution", "create_autonomous_session"]
        )
        self.assertEqual(names.count("call_llm"), 2)
        self.assertIn("execute_llm_tool", names)
        self.assertNotIn("execute_capability", names)
        self.assertIn("create_tool_call_messages", names)
        self.assertEqual(names[-2:], ["set_session_status", "dispatch_actions"])
        call_llm_calls = [
            call for call in execute_activity.await_args_list if call.args[0] == "call_llm"
        ]
        self.assertEqual(call_llm_calls[0].kwargs["args"][0], "thread-1")
        self.assertEqual(call_llm_calls[0].kwargs["args"][2], "provider-auto-1")
        self.assertIn("Verify API health", call_llm_calls[0].kwargs["args"][1][0]["content"])
        self.assertEqual(
            execute_activity.await_args_list[names.index("execute_llm_tool")].kwargs["args"],
            ["thread-1", tool_call, None],
        )
        self.assertEqual(
            execute_activity.await_args_list[names.index("create_tool_call_messages")].kwargs[
                "args"
            ],
            ["thread-1", tool_call, tool_result, "db-tool-1"],
        )
        event_calls = [
            call
            for call in execute_activity.await_args_list
            if call.args[0] == "record_agent_event"
        ]
        self.assertIn("thinking", [call.kwargs["args"][1] for call in event_calls])
        self.assertIn("tool_call_started", [call.kwargs["args"][1] for call in event_calls])
        self.assertIn("tool_result", [call.kwargs["args"][1] for call in event_calls])
        update_calls = [
            call
            for call in execute_activity.await_args_list
            if call.args[0] == "update_check_execution"
        ]
        self.assertEqual(update_calls[-1].kwargs["args"][2], "completed")
        self.assertNotIn("investigation_results", update_calls[-1].kwargs["args"][4])
        self.assertEqual(
            update_calls[-1].kwargs["args"][5],
            result["investigation_results"],
        )
        self.assertEqual(
            execute_activity.await_args_list[names.index("set_session_status")].kwargs["args"],
            ["session-1", "completed"],
        )

    def test_autonomous_run_marks_max_iterations_and_skips_actions_for_dry_run(self):
        workflow_instance = AutonomousInvestigationWorkflow()
        tool_call = self._tool_call()
        execute_activity = AsyncMock(
            side_effect=[
                self._check_context(
                    check_name="Disk",
                    investigation_goal="Check disk saturation",
                    instructions="Inspect disk metrics before deciding.",
                    execution_budget={"max_executions_per_session": 3},
                ),
                {"id": "execution-2"},
                {"session_id": "session-2", "thread_id": "thread-2"},
                None,
                {
                    "content": "Need disk evidence.",
                    "tool_calls": [tool_call],
                    "input_tokens": 3,
                    "output_tokens": 2,
                    "cost": "0.01",
                },
                None,
                None,
                {"db_tool_call_id": "db-tool-2", "result": {"usage": "99%"}},
                None,
                {"messages_created": True},
                None,
                {"id": "execution-2"},
                {"session_id": "session-2", "status": "completed"},
            ]
        )

        with (
            patch(
                "services.temporal_workers.workflows.workflow.execute_activity",
                execute_activity,
            ),
            patch("services.temporal_workers.workflows.workflow.logger", Mock()),
        ):
            result = asyncio.run(
                workflow_instance.run("check-2", "version-2", dry_run=True, max_iterations=1)
            )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["last_failure_reason"], "limit")
        self.assertEqual(result["last_failure_detail"], "Maximum tool-call iterations reached")
        self.assertEqual(result["iteration_count"], 0)
        self.assertEqual(result["evaluation"]["state"], "unknown")
        self.assertEqual(
            result["investigation_results"][0]["tool_calls"][0]["result"], {"usage": "99%"}
        )
        names = [call.args[0] for call in execute_activity.await_args_list]
        self.assertEqual(names.count("call_llm"), 1)
        self.assertIn("execute_llm_tool", names)
        self.assertNotIn("dispatch_actions", names)
        self.assertEqual(
            execute_activity.await_args_list[names.index("set_session_status")].kwargs["args"],
            ["session-2", "completed"],
        )

    def test_autonomous_run_marks_session_failed_on_exception(self):
        workflow_instance = AutonomousInvestigationWorkflow()
        execute_activity = AsyncMock(
            side_effect=[
                self._check_context(),
                {"id": "execution-3"},
                {"session_id": "session-3", "thread_id": "thread-3"},
                None,
                RuntimeError("LLM offline"),
                {"id": "execution-3"},
                {"session_id": "session-3", "status": "failed"},
            ]
        )

        with (
            patch(
                "services.temporal_workers.workflows.workflow.execute_activity",
                execute_activity,
            ),
            patch("services.temporal_workers.workflows.workflow.logger", Mock()),
        ):
            with self.assertRaises(RuntimeError):
                asyncio.run(workflow_instance.run("check-3", "version-3"))

        names = [call.args[0] for call in execute_activity.await_args_list]
        update_calls = [
            call
            for call in execute_activity.await_args_list
            if call.args[0] == "update_check_execution"
        ]
        self.assertEqual(
            update_calls[-1].kwargs["args"],
            ["check-3", "execution-3", "failed", "unknown", {"error": "LLM offline"}],
        )
        self.assertEqual(
            execute_activity.await_args_list[names.index("set_session_status")].kwargs["args"],
            ["session-3", "failed"],
        )

    def test_autonomous_run_calls_create_autonomous_session(self):
        workflow_instance = AutonomousInvestigationWorkflow()
        execute_activity = AsyncMock(
            side_effect=[
                self._check_context(
                    check_name="Certificate", investigation_goal="Inspect TLS expiry"
                ),
                {"id": "execution-4"},
                {"session_id": "session-4", "thread_id": "thread-4"},
                None,
                {
                    "content": (
                        '{"state":"healthy","confidence":0.8,"findings":[],'
                        '"summary":"TLS is valid"}'
                    ),
                    "tool_calls": [],
                    "input_tokens": 6,
                    "output_tokens": 4,
                    "cost": "0.02",
                },
                {"id": "execution-4"},
                {"session_id": "session-4", "status": "completed"},
                {"dispatched": True},
            ]
        )

        with (
            patch(
                "services.temporal_workers.workflows.workflow.execute_activity",
                execute_activity,
            ),
            patch("services.temporal_workers.workflows.workflow.logger", Mock()),
        ):
            result = asyncio.run(workflow_instance.run("check-4", "version-4"))

        names = [call.args[0] for call in execute_activity.await_args_list]
        self.assertEqual(
            names[:3], ["load_check_context", "update_check_execution", "create_autonomous_session"]
        )
        self.assertEqual(
            execute_activity.await_args_list[names.index("create_autonomous_session")].kwargs[
                "args"
            ],
            ["check-4", "execution-4"],
        )
        call_llm_args = execute_activity.await_args_list[names.index("call_llm")].kwargs["args"]
        self.assertEqual(call_llm_args[0], "thread-4")
        self.assertEqual(result["session_id"], "session-4")
        self.assertEqual(result["thread_id"], "thread-4")

    def test_autonomous_evidence_split(self):
        workflow_instance = AutonomousInvestigationWorkflow()
        tool_call = self._tool_call()
        tool_result = {"status": "up", "value": 1}
        execute_activity = AsyncMock(
            side_effect=[
                self._check_context(),
                {"id": "execution-evidence"},
                {"session_id": "session-evidence", "thread_id": "thread-evidence"},
                None,
                {
                    "content": "Need current metric evidence.",
                    "tool_calls": [tool_call],
                    "reasoning": "Check the up metric.",
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "cost": "0.02",
                },
                None,
                None,
                None,
                {"db_tool_call_id": "db-tool-evidence", "result": tool_result},
                None,
                {"messages_created": True},
                None,
                None,
                {
                    "content": '{"state":"healthy","confidence":0.9,"findings":[],"summary":"OK"}',
                    "tool_calls": [],
                    "input_tokens": 20,
                    "output_tokens": 10,
                    "cost": "0.03",
                },
                {"id": "execution-evidence"},
                {"session_id": "session-evidence", "status": "completed"},
                {"dispatched": True},
            ]
        )

        with (
            patch(
                "services.temporal_workers.workflows.workflow.execute_activity",
                execute_activity,
            ),
            patch("services.temporal_workers.workflows.workflow.logger", Mock()),
        ):
            result = asyncio.run(workflow_instance.run("check-evidence", "version-evidence"))

        self.assertEqual(result["status"], "completed")
        update_calls = [
            call
            for call in execute_activity.await_args_list
            if call.args[0] == "update_check_execution"
        ]
        final_update = update_calls[-1].kwargs["args"]
        eval_result = final_update[4]
        self.assertNotIn("investigation_results", eval_result)
        self.assertIn("findings", eval_result)
        self.assertIn("summary", eval_result)
        self.assertIn("confidence", eval_result)
        evidence = final_update[5]
        self.assertIsInstance(evidence, list)
        self.assertEqual(evidence[0]["iteration"], 0)
        self.assertEqual(evidence[0]["tool_calls"][0]["result"], tool_result)

    def test_autonomous_token_accumulators(self):
        workflow_instance = AutonomousInvestigationWorkflow()
        tool_call = self._tool_call()
        execute_activity = AsyncMock(
            side_effect=[
                self._check_context(),
                {"id": "execution-acc"},
                {"session_id": "session-acc", "thread_id": "thread-acc"},
                None,
                {
                    "content": "First tool call.",
                    "tool_calls": [tool_call],
                    "input_tokens": 50,
                    "output_tokens": 25,
                    "cost": "0.02",
                },
                None,
                None,
                None,
                {"db_tool_call_id": "db-tool-acc", "result": {"data": 1}},
                None,
                {"messages_created": True},
                None,
                {
                    "content": (
                        '{"state":"degraded","confidence":0.7,"findings":[],"summary":"Slow"}'
                    ),
                    "tool_calls": [],
                    "input_tokens": 50,
                    "output_tokens": 25,
                    "cost": "0.03",
                },
                {"id": "execution-acc"},
                {"session_id": "session-acc", "status": "completed"},
                {"dispatched": True},
            ]
        )

        with (
            patch(
                "services.temporal_workers.workflows.workflow.execute_activity",
                execute_activity,
            ),
            patch("services.temporal_workers.workflows.workflow.logger", Mock()),
        ):
            result = asyncio.run(workflow_instance.run("check-acc", "version-acc"))

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["cumulative_input_tokens"], 100)
        self.assertEqual(result["cumulative_output_tokens"], 50)
        self.assertEqual(result["cumulative_tokens"], 150)
        update_calls = [
            call
            for call in execute_activity.await_args_list
            if call.args[0] == "update_check_execution"
        ]
        final_update = update_calls[-1].kwargs["args"]
        eval_result = final_update[4]
        self.assertEqual(eval_result["input_tokens"], 100)
        self.assertEqual(eval_result["output_tokens"], 50)

    def test_autonomous_parse_evaluation_accepts_markdown_json_and_fallback_text(self):
        workflow_instance = AutonomousInvestigationWorkflow()

        parsed = workflow_instance._parse_evaluation(
            '```json\n{"state":"degraded","confidence":0.7,"findings":[{"message":"slow"}],"summary":"Slow"}\n```'
        )
        fallback = workflow_instance._parse_evaluation("Critical failure in api service")

        self.assertEqual(parsed["state"], "degraded")
        self.assertEqual(parsed["findings"][0]["message"], "slow")
        self.assertEqual(fallback["state"], "critical")
        self.assertEqual(fallback["confidence"], 0.5)


class TemporalActivityTests(TransactionTestCase):
    def test_initialize_session_updates_models_from_async_activity(self):
        user = User.objects.create_user(username="temporal", password="pass")
        organization = Organization.objects.create(name="Temporal", slug="temporal")
        session = TSession.objects.create(
            organization=organization,
            title="Async initialization",
            created_by=user,
        )
        thread = Thread.objects.create(tsession=session, user=user)

        result = asyncio.run(initialize_session(str(session.id), str(thread.id)))

        session.refresh_from_db()
        thread.refresh_from_db()

        self.assertEqual(
            result,
            {
                "session_id": str(session.id),
                "thread_id": str(thread.id),
                "status": "initialized",
            },
        )
        self.assertEqual(session.status, TSession.Status.ACTIVE)
        self.assertEqual(thread.status, Thread.Status.ACTIVE)

    def test_build_llm_context_includes_system_prompt_and_messages(self):
        user = User.objects.create_user(username="build_ctx", password="pass")
        organization = Organization.objects.create(name="BuildCtx", slug="buildctx")
        session = TSession.objects.create(
            organization=organization,
            title="Build Context",
            created_by=user,
        )
        thread = Thread.objects.create(tsession=session, user=user)
        Message.objects.create(thread=thread, role=Message.Role.USER, content="Hello")
        Message.objects.create(thread=thread, role=Message.Role.ASSISTANT, content="")
        Message.objects.create(thread=thread, role=Message.Role.ASSISTANT, content="Hi there")

        result = asyncio.run(
            build_llm_context(str(thread.id), {"content": "Test", "role": Message.Role.USER})
        )

        messages = result["messages"]
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[1]["role"], "user")
        self.assertEqual(messages[1]["content"], "Hello")
        self.assertEqual(messages[2]["role"], "assistant")
        self.assertEqual(messages[2]["content"], "Hi there")
        self.assertEqual(len(messages), 3)

    @patch("services.llm.registry.get_llm_client")
    def test_call_llm_returns_content_and_tool_calls(self, mock_get_client):
        from services.llm.base import LLMResponse, ToolCall

        mock_client = Mock()
        mock_client.chat = AsyncMock(
            return_value=LLMResponse(
                content="Disk is 95% full",
                tool_calls=[ToolCall(id="tc1", name="check_disk", arguments={"path": "/"})],
            )
        )
        mock_get_client.return_value = mock_client

        user = User.objects.create_user(username="call_llm", password="pass")
        organization = Organization.objects.create(name="CallLLM", slug="callllm")
        session = TSession.objects.create(
            organization=organization,
            title="Call LLM",
            created_by=user,
        )
        thread = Thread.objects.create(tsession=session, user=user)

        result = asyncio.run(
            call_llm(
                str(thread.id),
                [
                    {"role": "system", "content": "You are a SRE"},
                    {"role": "user", "content": "Check disk"},
                ],
            )
        )

        self.assertEqual(result["content"], "Disk is 95% full")
        self.assertEqual(len(result["tool_calls"]), 1)
        self.assertEqual(result["tool_calls"][0]["name"], "check_disk")

    @patch("services.llm.registry.get_llm_client")
    def test_call_llm_preserves_tool_context_for_followup_turn(self, mock_get_client):
        from services.llm.base import LLMResponse

        mock_client = Mock()
        mock_client.chat = AsyncMock(
            return_value=LLMResponse(content="Tool result analyzed", tool_calls=[])
        )
        mock_get_client.return_value = mock_client

        user = User.objects.create_user(username="call_llm_tools", password="pass")
        organization = Organization.objects.create(name="CallLLMTools", slug="callllmtools")
        session = TSession.objects.create(
            organization=organization,
            title="Call LLM Tools",
            created_by=user,
        )
        thread = Thread.objects.create(tsession=session, user=user)

        result = asyncio.run(
            call_llm(
                str(thread.id),
                [
                    {"role": "system", "content": "You are a SRE"},
                    {"role": "user", "content": "Check disk"},
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {"id": "tc1", "name": "check_disk", "arguments": {"path": "/"}}
                        ],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": "tc1",
                        "name": "check_disk",
                        "content": '{"usage": "95%"}',
                    },
                ],
            )
        )

        self.assertEqual(result["content"], "Tool result analyzed")
        sent_messages = mock_client.chat.await_args.args[0]
        self.assertEqual(sent_messages[2].tool_calls[0].id, "tc1")
        self.assertEqual(sent_messages[2].tool_calls[0].name, "check_disk")
        self.assertEqual(sent_messages[2].tool_calls[0].arguments, {"path": "/"})
        self.assertEqual(sent_messages[3].tool_call_id, "tc1")
        self.assertEqual(sent_messages[3].name, "check_disk")

    def test_record_agent_event_creates_event_row(self):
        user = User.objects.create_user(username="record_ev", password="pass")
        organization = Organization.objects.create(name="RecordEv", slug="recordev")
        session = TSession.objects.create(
            organization=organization,
            title="Record Event",
            created_by=user,
        )
        thread = Thread.objects.create(tsession=session, user=user)

        result = asyncio.run(
            record_agent_event(
                str(thread.id),
                "tool_call_started",
                {"name": "check_logs", "arguments": {"service": "api"}},
            )
        )

        self.assertEqual(result["kind"], "tool_call_started")
        self.assertEqual(result["label"], "Calling tool check_logs")

        events = thread.agent_events.filter(kind="tool_call_started")
        self.assertEqual(events.count(), 1)
        self.assertEqual(events.first().detail["name"], "check_logs")

    def test_create_assistant_message_skips_blank_content(self):
        user = User.objects.create_user(username="blank_assistant", password="pass")
        organization = Organization.objects.create(name="BlankAssistant", slug="blankassistant")
        session = TSession.objects.create(
            organization=organization,
            title="Blank Assistant",
            created_by=user,
        )
        thread = Thread.objects.create(tsession=session, user=user)

        result = asyncio.run(create_assistant_message(str(thread.id), "   \n"))

        self.assertEqual(
            result,
            {"message_id": None, "thread_id": str(thread.id), "skipped": True},
        )
        self.assertFalse(
            Message.objects.filter(thread=thread, role=Message.Role.ASSISTANT).exists()
        )

    def test_build_llm_context_includes_tool_messages(self):
        user = User.objects.create_user(username="tool_ctx", password="pass")
        organization = Organization.objects.create(name="ToolCtx", slug="toolctx")
        session = TSession.objects.create(
            organization=organization,
            title="Tool Context",
            created_by=user,
        )
        thread = Thread.objects.create(tsession=session, user=user)
        Message.objects.create(thread=thread, role=Message.Role.USER, content="Check disk")
        Message.objects.create(
            thread=thread,
            role=Message.Role.ASSISTANT,
            content="",
            metadata={
                "tool_calls": [{"id": "tc1", "name": "check_disk", "arguments": {"path": "/"}}]
            },
        )
        Message.objects.create(
            thread=thread,
            role=Message.Role.TOOL,
            content='{"usage": "95%"}',
            metadata={"tool_call_id": "tc1", "name": "check_disk"},
        )
        Message.objects.create(
            thread=thread, role=Message.Role.ASSISTANT, content="Disk is at 95%."
        )

        result = asyncio.run(
            build_llm_context(str(thread.id), {"content": "Test", "role": Message.Role.USER})
        )

        messages = result["messages"]
        self.assertEqual(len(messages), 5)
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[1]["role"], "user")
        self.assertEqual(messages[2]["role"], "assistant")
        self.assertEqual(messages[2]["tool_calls"][0]["name"], "check_disk")
        self.assertEqual(messages[3]["role"], "tool")
        self.assertEqual(messages[3]["tool_call_id"], "tc1")
        self.assertEqual(messages[3]["name"], "check_disk")
        self.assertEqual(messages[4]["role"], "assistant")
        self.assertEqual(messages[4]["content"], "Disk is at 95%.")

    def test_build_llm_context_skips_blank_messages_without_tool_calls(self):
        user = User.objects.create_user(username="skip_blank", password="pass")
        organization = Organization.objects.create(name="SkipBlank", slug="skipblank")
        session = TSession.objects.create(
            organization=organization,
            title="Skip Blank",
            created_by=user,
        )
        thread = Thread.objects.create(tsession=session, user=user)
        Message.objects.create(thread=thread, role=Message.Role.USER, content="Hello")
        Message.objects.create(thread=thread, role=Message.Role.ASSISTANT, content="")
        Message.objects.create(thread=thread, role=Message.Role.ASSISTANT, content="Hi there")

        result = asyncio.run(
            build_llm_context(str(thread.id), {"content": "Test", "role": Message.Role.USER})
        )

        messages = result["messages"]
        self.assertEqual(len(messages), 3)
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[1]["role"], "user")
        self.assertEqual(messages[2]["role"], "assistant")
        self.assertEqual(messages[2]["content"], "Hi there")

    def test_create_tool_call_messages_creates_two_rows(self):
        user = User.objects.create_user(username="create_tool", password="pass")
        organization = Organization.objects.create(name="CreateTool", slug="createtool")
        session = TSession.objects.create(
            organization=organization,
            title="Create Tool",
            created_by=user,
        )
        thread = Thread.objects.create(tsession=session, user=user)

        result = asyncio.run(
            create_tool_call_messages(
                str(thread.id),
                {"id": "tc1", "name": "check_disk", "arguments": {"path": "/"}},
                {"usage": "95%"},
                db_tool_call_id=None,
            )
        )

        self.assertIn("assistant_message_id", result)
        self.assertIn("tool_message_id", result)
        assistant_msg = Message.objects.get(id=result["assistant_message_id"])
        tool_msg = Message.objects.get(id=result["tool_message_id"])
        self.assertEqual(assistant_msg.role, Message.Role.ASSISTANT)
        self.assertEqual(assistant_msg.metadata["tool_calls"][0]["name"], "check_disk")
        self.assertEqual(tool_msg.role, Message.Role.TOOL)
        self.assertEqual(tool_msg.metadata["tool_call_id"], "tc1")
        self.assertEqual(tool_msg.metadata["name"], "check_disk")

    def test_create_tool_call_messages_accepts_list_result(self):
        user = User.objects.create_user(username="create_tool_list", password="pass")
        organization = Organization.objects.create(name="CreateToolList", slug="createtoollist")
        session = TSession.objects.create(
            organization=organization,
            title="Create Tool List",
            created_by=user,
        )
        thread = Thread.objects.create(tsession=session, user=user)

        result = asyncio.run(
            create_tool_call_messages(
                str(thread.id),
                {"id": "tc1", "name": "find_tools", "arguments": {"query": "disk"}},
                [{"name": "check_disk"}],
                db_tool_call_id=None,
            )
        )

        tool_msg = Message.objects.get(id=result["tool_message_id"])
        self.assertEqual(tool_msg.role, Message.Role.TOOL)
        self.assertEqual(tool_msg.content, '[{"name": "check_disk"}]')

    def test_create_tool_call_messages_links_to_db_tool_call(self):
        from apps.marvins.models import Capability

        user = User.objects.create_user(username="link_tool", password="pass")
        organization = Organization.objects.create(name="LinkTool", slug="linktool")
        session = TSession.objects.create(
            organization=organization,
            title="Link Tool",
            created_by=user,
        )
        thread = Thread.objects.create(tsession=session, user=user)
        capability = Capability.objects.create(
            organization=organization,
            name="check_disk",
            description="Check disk usage",
        )
        tool_call = ToolCall.objects.create(
            thread=thread,
            capability=capability,
            parameters={"path": "/"},
            status=ToolCall.Status.COMPLETED,
        )

        result = asyncio.run(
            create_tool_call_messages(
                str(thread.id),
                {"id": "tc1", "name": "check_disk", "arguments": {"path": "/"}},
                {"usage": "95%"},
                db_tool_call_id=str(tool_call.id),
            )
        )

        assistant_msg = Message.objects.get(id=result["assistant_message_id"])
        tool_msg = Message.objects.get(id=result["tool_message_id"])
        self.assertEqual(assistant_msg.tool_call, tool_call)
        self.assertEqual(tool_msg.tool_call, tool_call)

    @patch("services.llm.registry.get_llm_client")
    def test_call_llm_uses_session_provider_override(self, mock_get_client):
        from services.llm.base import LLMResponse

        mock_client = Mock()
        mock_client.chat = AsyncMock(return_value=LLMResponse(content="ok", tool_calls=[]))
        mock_get_client.return_value = mock_client

        user = User.objects.create_user(username="override_user", password="pass")
        organization = Organization.objects.create(name="Override", slug="override")
        override_provider = LLMProvider.objects.create(
            organization=organization,
            name="Override",
            provider_type=LLMProvider.ProviderType.OPENAI_COMPAT,
            model="override-model",
        )
        session = TSession.objects.create(
            organization=organization,
            title="Override Session",
            created_by=user,
            llm_provider=override_provider,
        )
        thread = Thread.objects.create(tsession=session, user=user)

        asyncio.run(call_llm(str(thread.id), [{"role": "user", "content": "hi"}]))

        mock_get_client.assert_called_once_with(str(organization.id), str(override_provider.id))

    @patch("services.llm.registry.get_llm_client")
    def test_call_llm_without_provider_override_uses_org_default(self, mock_get_client):
        from services.llm.base import LLMResponse

        mock_client = Mock()
        mock_client.chat = AsyncMock(return_value=LLMResponse(content="ok", tool_calls=[]))
        mock_get_client.return_value = mock_client

        user = User.objects.create_user(username="default_user", password="pass")
        organization = Organization.objects.create(name="Default2", slug="default2")
        LLMProvider.objects.create(
            organization=organization,
            name="Default",
            provider_type=LLMProvider.ProviderType.OLLAMA,
            model="default-model",
            is_default=True,
        )
        session = TSession.objects.create(
            organization=organization,
            title="Default Session",
            created_by=user,
        )
        thread = Thread.objects.create(tsession=session, user=user)

        asyncio.run(call_llm(str(thread.id), [{"role": "user", "content": "hi"}]))

        mock_get_client.assert_called_once_with(str(organization.id), None)

    @patch("services.llm.registry.get_llm_client")
    def test_call_llm_uses_explicit_provider_override(self, mock_get_client):
        from services.llm.base import LLMResponse

        mock_client = Mock()
        mock_client.chat = AsyncMock(return_value=LLMResponse(content="ok", tool_calls=[]))
        mock_get_client.return_value = mock_client

        user = User.objects.create_user(username="explicit_user", password="pass")
        organization = Organization.objects.create(name="Explicit", slug="explicit")
        session_provider = LLMProvider.objects.create(
            organization=organization,
            name="Session",
            provider_type=LLMProvider.ProviderType.OPENAI_COMPAT,
            model="session-model",
        )
        check_provider = LLMProvider.objects.create(
            organization=organization,
            name="Check",
            provider_type=LLMProvider.ProviderType.OPENAI_COMPAT,
            model="check-model",
        )
        session = TSession.objects.create(
            organization=organization,
            title="Explicit Session",
            created_by=user,
            llm_provider=session_provider,
        )
        thread = Thread.objects.create(tsession=session, user=user)

        asyncio.run(
            call_llm(
                str(thread.id),
                [{"role": "user", "content": "hi"}],
                str(check_provider.id),
            )
        )

        mock_get_client.assert_called_once_with(str(organization.id), str(check_provider.id))


def _create_fanout_fixture(slug="fanout"):
    from apps.marvins.models import Capability, Marvin, ResolvedTargetSet

    user = User.objects.create_user(username=f"{slug}_user", password="pass")
    organization = Organization.objects.create(name=f"{slug.title()} Org", slug=f"{slug}-org")
    session = TSession.objects.create(
        organization=organization,
        title=f"{slug.title()} Session",
        created_by=user,
    )
    thread = Thread.objects.create(tsession=session, user=user)
    capability = Capability.objects.create(
        organization=organization,
        name=f"{slug}.check",
        selection_strategy="all",
    )
    marvins = [
        Marvin.objects.create(
            organization=organization,
            name=f"{slug}-marvin-{index}",
            client_id=f"{slug}-client-{index}",
            status=Marvin.Status.ONLINE,
            labels=["env:prod", f"node:{index}"],
        )
        for index in range(2)
    ]
    for marvin in marvins:
        marvin.capabilities.add(capability)
    target_set = ResolvedTargetSet.objects.create(
        organization=organization,
        capability=capability,
        selector={"labels": {"env": "prod"}},
        snapshot=[str(marvin.id) for marvin in marvins],
        snapshot_metadata={"count": len(marvins)},
        expires_at=timezone.now(),
    )
    return user, organization, session, thread, capability, marvins, target_set


class TemporalFanoutActivityTests(TransactionTestCase):
    def test_route_and_execute_resolves_targets_and_creates_fanout_tool_call(self):
        _user, organization, _session, thread, capability, marvins, target_set = (
            _create_fanout_fixture("routefanout")
        )
        policy = Mock()
        policy.model_dump.return_value = {"max_targets": 2, "mode": "characterized"}

        with patch("apps.marvins.discovery.DiscoveryEngine") as discovery_engine:
            engine = discovery_engine.return_value
            engine.resolve_targets.return_value = target_set
            engine.check_execution_policy.return_value = (policy, Mock())

            tool_call, marvin_ids, error = _route_and_execute(
                organization,
                thread,
                {
                    "capability_name": capability.name,
                    "labels": "env:prod",
                    "parameters": {"path": "/var/log/app.log"},
                    "timeout_seconds": 45,
                },
            )

        assert error is None
        assert marvin_ids == [str(marvin.id) for marvin in marvins]
        assert tool_call.capability == capability
        assert tool_call.thread == thread
        assert tool_call.parameters == {"path": "/var/log/app.log"}
        assert tool_call.labels == {"env": "prod"}
        assert tool_call.marvin == marvins[0]
        assert tool_call.timeout_seconds == 45
        assert tool_call.status == ToolCall.Status.IN_PROGRESS
        assert tool_call.target_set == target_set
        assert tool_call.execution_mode == "fanout"
        assert tool_call.policy_snapshot == {"max_targets": 2, "mode": "characterized"}
        engine.resolve_targets.assert_called_once()
        engine.check_execution_policy.assert_called_once_with(
            organization,
            capability,
            2,
            session=thread.tsession,
        )

    def test_route_and_execute_empty_target_set_returns_error_without_tool_call(self):
        _user, organization, _session, thread, capability, _marvins, target_set = (
            _create_fanout_fixture("emptyfanout")
        )
        target_set.snapshot = []
        target_set.save(update_fields=["snapshot"])
        policy = Mock()
        policy.model_dump.return_value = {"max_targets": 2}

        with (
            patch("apps.marvins.discovery.DiscoveryEngine") as discovery_engine,
            patch("apps.sessions.budget.release_execution_budget") as release_budget,
        ):
            engine = discovery_engine.return_value
            engine.resolve_targets.return_value = target_set
            engine.check_execution_policy.return_value = (policy, Mock())

            tool_call, marvin_ids, error = _route_and_execute(
                organization,
                thread,
                {"capability_name": capability.name, "labels": "env:prod"},
            )

        assert tool_call is None
        assert marvin_ids == []
        assert error == {
            "error": (
                f"No online Marvin found for capability '{capability.name}' with matching labels"
            )
        }
        assert ToolCall.objects.count() == 0
        release_budget.assert_called_once_with(thread.tsession, 0)

    def test_create_executions_creates_pending_rows_in_result_index_order(self):
        _user, _organization, _session, thread, capability, marvins, _target_set = (
            _create_fanout_fixture("executionfanout")
        )
        tool_call = ToolCall.objects.create(
            thread=thread,
            capability=capability,
            status=ToolCall.Status.IN_PROGRESS,
            execution_mode="fanout",
        )

        result = _create_executions(str(tool_call.id), [str(marvin.id) for marvin in marvins])

        executions = list(Execution.objects.filter(tool_call=tool_call).order_by("result_index"))
        assert result == [{"id": str(execution.id)} for execution in executions]
        assert [execution.result_index for execution in executions] == [0, 1]
        assert [execution.status for execution in executions] == [
            Execution.Status.PENDING,
            Execution.Status.PENDING,
        ]
        assert [execution.tool_call_id for execution in executions] == [tool_call.id, tool_call.id]
        assert [execution.marvin_id for execution in executions] == [None, None]

    def test_execute_llm_tool_fanout_aggregates_partial_failures_with_exact_shape(self):
        _user, organization, _session, thread, capability, marvins, target_set = (
            _create_fanout_fixture("aggregatefanout")
        )
        policy = Mock()
        policy.model_dump.return_value = {"max_targets": 2}

        async def execute_side_effect(*args, **kwargs):
            result_index = kwargs["result_index"]
            if result_index == 1:
                raise RuntimeError("marvin exploded")
            return {"stdout": "healthy", "target": args[4], "index": result_index}

        with (
            patch("apps.marvins.discovery.DiscoveryEngine") as discovery_engine,
            patch("apps.sessions.budget.release_execution_budget") as release_budget,
            patch(
                "services.temporal_workers.activities.execute_capability",
                new=AsyncMock(side_effect=execute_side_effect),
            ) as execute_capability_mock,
        ):
            engine = discovery_engine.return_value
            engine.resolve_targets.return_value = target_set
            engine.check_execution_policy.return_value = (policy, Mock())

            result = asyncio.run(
                execute_llm_tool(
                    str(thread.id),
                    {
                        "name": "execute_tool",
                        "arguments": {
                            "capability_name": capability.name,
                            "labels": "env:prod",
                            "parameters": {"service": "api"},
                        },
                    },
                    None,
                )
            )

        expected_aggregation = {
            "success": False,
            "results": [
                {
                    "index": 0,
                    "marvin_id": str(marvins[0].id),
                    "result": {
                        "stdout": "healthy",
                        "target": str(marvins[0].id),
                        "index": 0,
                    },
                }
            ],
            "errors": [{"index": 1, "marvin_id": str(marvins[1].id), "error": "marvin exploded"}],
            "summary": {"total": 2, "succeeded": 1, "failed": 1},
        }
        assert set(result.keys()) == {"db_tool_call_id", "result"}
        assert result["result"] == expected_aggregation
        tool_call = ToolCall.objects.get(id=result["db_tool_call_id"])
        assert tool_call.result == expected_aggregation
        assert tool_call.status == ToolCall.Status.COMPLETED
        executions = list(Execution.objects.filter(tool_call=tool_call).order_by("result_index"))
        assert [execution.status for execution in executions] == [
            Execution.Status.COMPLETED,
            Execution.Status.FAILED,
        ]
        assert executions[0].result == {
            "stdout": "healthy",
            "target": str(marvins[0].id),
            "index": 0,
        }
        assert executions[1].error_message == "marvin exploded"
        assert execute_capability_mock.await_count == 2
        release_budget.assert_called_once_with(thread.tsession, 2)

    def test_execute_capability_deadline_is_300_seconds_in_future(self):
        """Regression: deadline must be computed without datetime.timezone.utc.

        The activity previously used ``datetime.now(datetime.timezone.utc)`` which
        raised ``TypeError`` during real execution (tests mocked the activity, so
        the bug was hidden). This exercises the real activity with a mocked gRPC
        stream and asserts the emitted deadline is ~300s in the future.
        """
        from services.temporal_workers import activities

        _user, _organization, session, thread, _capability, marvins, _target_set = (
            _create_fanout_fixture("deadline")
        )
        marvin = marvins[0]

        stream = Mock()
        stream.send_command = AsyncMock(return_value={"stdout": "ok"})
        grpc_service = Mock()
        grpc_service.get_marvin_stream.return_value = stream

        before_ms = int(datetime.now(UTC).timestamp() * 1000)
        with patch.object(activities, "_grpc_service", grpc_service):
            result = asyncio.run(
                activities.execute_capability(
                    str(session.id),
                    str(thread.id),
                    "deadline.check",
                    {"service": "api"},
                    str(marvin.id),
                )
            )
        after_ms = int(datetime.now(UTC).timestamp() * 1000)

        assert result == {"stdout": "ok"}
        grpc_service.get_marvin_stream.assert_called_once_with(marvin.client_id)
        command = stream.send_command.await_args.args[0]
        deadline = command["execute_capability"]["deadline_unix_ms"]
        assert before_ms + 300_000 <= deadline <= after_ms + 300_000
        assert abs(deadline - (before_ms + 300_000)) < 5_000


class AgentErrorTests(TransactionTestCase):
    def test_execute_llm_tool_no_marvin_matching_labels(self):
        from services.temporal_workers.activities import execute_llm_tool

        user = User.objects.create_user(username="err", password="pass")
        organization = Organization.objects.create(name="Err", slug="err")
        session = TSession.objects.create(
            organization=organization,
            title="Error Session",
            created_by=user,
        )
        thread = Thread.objects.create(tsession=session, user=user)

        result = asyncio.run(
            execute_llm_tool(
                str(thread.id),
                {
                    "name": "execute_tool",
                    "arguments": {
                        "capability_name": "nonexistent",
                        "labels": "env:production",
                    },
                },
                None,
            )
        )

        self.assertIsNone(result["db_tool_call_id"])
        self.assertIn("error", result["result"])
        self.assertIn("No online Marvin found", result["result"]["error"])

    @patch("services.llm.registry.get_llm_client")
    def test_call_llm_handles_api_error(self, mock_get_client):
        mock_client = Mock()
        mock_client.chat = AsyncMock(side_effect=RuntimeError("LLM API unreachable"))
        mock_get_client.return_value = mock_client

        user = User.objects.create_user(username="llm_err", password="pass")
        organization = Organization.objects.create(name="LLMErr", slug="llmerr")
        session = TSession.objects.create(
            organization=organization,
            title="LLM Error Session",
            created_by=user,
        )
        thread = Thread.objects.create(tsession=session, user=user)

        result = asyncio.run(
            call_llm(
                str(thread.id),
                [{"role": "user", "content": "Hello"}],
            )
        )
        self.assertEqual(result["reason"], "unknown_error")
        self.assertIn("LLM API unreachable", result["error"])


class SessionAPITests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="api_user", password="pass")
        self.organization = Organization.objects.create(name="API Org", slug="api-org")
        OrganizationMembership.objects.create(
            user=self.user,
            organization=self.organization,
            role=OrganizationMembership.Role.OWNER,
        )
        self.client.force_login(self.user)

    def test_session_list_api_returns_org_sessions(self):
        TSession.objects.create(
            organization=self.organization, title="API Session", created_by=self.user
        )
        response = self.client.get("/api/sessions/")
        self.assertEqual(response.status_code, 200)
        data = response.json()["results"]
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["title"], "API Session")

    def test_session_create_api(self):
        response = self.client.post(
            "/api/sessions/",
            {"organization": str(self.organization.id), "title": "New API Session"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["title"], "New API Session")

    def test_session_create_with_llm_provider(self):
        provider = LLMProvider.objects.create(
            organization=self.organization,
            name="Custom",
            provider_type=LLMProvider.ProviderType.OLLAMA,
            model="custom-model",
        )
        response = self.client.post(
            "/api/sessions/",
            {
                "organization": str(self.organization.id),
                "title": "Custom Model Session",
                "llm_provider": str(provider.id),
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertEqual(data["title"], "Custom Model Session")
        self.assertEqual(data["llm_provider"], str(provider.id))
        self.assertEqual(data["llm_provider_name"], "Custom")

    def test_session_update_llm_provider(self):
        session = TSession.objects.create(
            organization=self.organization, title="Updatable", created_by=self.user
        )
        provider = LLMProvider.objects.create(
            organization=self.organization,
            name="Updated",
            provider_type=LLMProvider.ProviderType.OLLAMA,
            model="updated-model",
        )
        response = self.client.patch(
            f"/api/sessions/{session.id}/",
            {"llm_provider": str(provider.id)},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["llm_provider"], str(provider.id))
        self.assertEqual(data["llm_provider_name"], "Updated")

    def test_thread_list_api_filtered_by_session(self):
        session = TSession.objects.create(
            organization=self.organization, title="Thread Session", created_by=self.user
        )
        thread = Thread.objects.create(tsession=session, user=self.user)
        response = self.client.get(f"/api/threads/?session={session.id}")
        self.assertEqual(response.status_code, 200)
        data = response.json()["results"]
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["id"], str(thread.id))

    def test_message_list_api_filtered_by_thread(self):
        session = TSession.objects.create(
            organization=self.organization, title="Msg Session", created_by=self.user
        )
        thread = Thread.objects.create(tsession=session, user=self.user)
        Message.objects.create(thread=thread, role=Message.Role.USER, content="Hello API")
        response = self.client.get(f"/api/messages/?thread={thread.id}")
        self.assertEqual(response.status_code, 200)
        data = response.json()["results"]
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["content"], "Hello API")

    def test_tool_call_list_api_filtered_by_thread(self):
        session = TSession.objects.create(
            organization=self.organization, title="Tool Session", created_by=self.user
        )
        thread = Thread.objects.create(tsession=session, user=self.user)
        ToolCall.objects.create(thread=thread, parameters={"cmd": "ls"})
        response = self.client.get(f"/api/tool-calls/?thread={thread.id}")
        self.assertEqual(response.status_code, 200)
        data = response.json()["results"]
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["parameters"]["cmd"], "ls")

    def test_agent_event_list_api_filtered_by_thread(self):
        session = TSession.objects.create(
            organization=self.organization, title="Event Session", created_by=self.user
        )
        thread = Thread.objects.create(tsession=session, user=self.user)
        AgentEvent.objects.create(
            thread=thread,
            kind=AgentEvent.Kind.THINKING,
            label="Analyzing logs",
            detail={"step": 1},
        )
        response = self.client.get(f"/api/agent-events/?thread={thread.id}")
        self.assertEqual(response.status_code, 200)
        data = response.json()["results"]
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["label"], "Analyzing logs")

    def test_architecture_request_list_api_filtered_by_thread(self):
        session = TSession.objects.create(
            organization=self.organization, title="Arch Session", created_by=self.user
        )
        thread = Thread.objects.create(tsession=session, user=self.user)
        ArchitectureRequest.objects.create(thread=thread, description="Need diagram")
        response = self.client.get(f"/api/architecture-requests/?thread={thread.id}")
        self.assertEqual(response.status_code, 200)
        data = response.json()["results"]
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["description"], "Need diagram")

    def test_api_cross_org_isolation(self):
        other_org = Organization.objects.create(name="Other", slug="other")
        TSession.objects.create(organization=other_org, title="Other Session", created_by=self.user)
        response = self.client.get("/api/sessions/")
        self.assertEqual(response.status_code, 200)
        data = response.json()["results"]
        self.assertEqual(len(data), 0)


class BuildChatItemsTests(TestCase):
    def test_tool_call_started_and_result_are_grouped(self):
        from apps.sessions.views import _build_chat_items

        messages = []
        events = [
            {
                "id": "e1",
                "kind": "tool_call_started",
                "label": "Calling tool x",
                "detail": {"name": "x"},
                "created_at": datetime(2024, 1, 1, 12, 0, 0),
            },
            {
                "id": "e2",
                "kind": "tool_result",
                "label": "Tool x completed",
                "detail": {"name": "x", "result": {"ok": True}},
                "created_at": datetime(2024, 1, 1, 12, 0, 1),
            },
        ]
        items = _build_chat_items(messages, events)
        self.assertEqual(items, [])

    def test_unmatched_tool_call_started_without_result_is_not_grouped(self):
        from apps.sessions.views import _build_chat_items

        events = [
            {
                "id": "e1",
                "kind": "tool_call_started",
                "label": "Calling tool x",
                "detail": {"name": "x"},
                "created_at": datetime(2024, 1, 1, 12, 0, 0),
            },
        ]
        items = _build_chat_items([], events)
        self.assertEqual(items, [])

    def test_grouped_items_maintain_chronological_order_with_messages(self):
        from apps.sessions.models import Message
        from apps.sessions.views import _build_chat_items

        messages = [
            {
                "id": "m1",
                "role": Message.Role.USER,
                "content": "Hello",
                "created_at": datetime(2024, 1, 1, 12, 0, 0),
            },
        ]
        events = [
            {
                "id": "e1",
                "kind": "tool_call_started",
                "label": "Calling tool x",
                "detail": {"name": "x"},
                "created_at": datetime(2024, 1, 1, 12, 0, 1),
            },
            {
                "id": "e2",
                "kind": "tool_result",
                "label": "Tool x completed",
                "detail": {"name": "x", "result": {"ok": True}},
                "created_at": datetime(2024, 1, 1, 12, 0, 2),
            },
        ]
        items = _build_chat_items(messages, events)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["type"], "message")
        self.assertEqual(items[1]["type"], "progress")

    def test_blank_messages_are_not_rendered_as_chat_items(self):
        from apps.sessions.models import Message
        from apps.sessions.views import _build_chat_items

        messages = [
            {
                "id": "m1",
                "role": Message.Role.ASSISTANT,
                "content": "",
                "created_at": datetime(2024, 1, 1, 12, 0, 0),
            },
            {
                "id": "m2",
                "role": Message.Role.ASSISTANT,
                "content": "Actual answer",
                "created_at": datetime(2024, 1, 1, 12, 0, 1),
            },
        ]

        items = _build_chat_items(messages, [])

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["content"], "Actual answer")

    def test_multiple_tool_groups_group_correctly(self):
        from apps.sessions.views import _build_chat_items

        events = [
            {
                "id": "e1",
                "kind": "tool_call_started",
                "label": "Calling tool a",
                "detail": {"name": "a"},
                "created_at": datetime(2024, 1, 1, 12, 0, 0),
            },
            {
                "id": "e2",
                "kind": "tool_result",
                "label": "Tool a completed",
                "detail": {"name": "a"},
                "created_at": datetime(2024, 1, 1, 12, 0, 1),
            },
            {
                "id": "e3",
                "kind": "tool_call_started",
                "label": "Calling tool b",
                "detail": {"name": "b"},
                "created_at": datetime(2024, 1, 1, 12, 0, 2),
            },
            {
                "id": "e4",
                "kind": "tool_result",
                "label": "Tool b completed",
                "detail": {"name": "b"},
                "created_at": datetime(2024, 1, 1, 12, 0, 3),
            },
        ]
        items = _build_chat_items([], events)
        self.assertEqual(items, [])

    def test_build_chat_items_filters_tool_messages(self):
        from apps.sessions.models import Message
        from apps.sessions.views import _build_chat_items

        messages = [
            {
                "id": "m1",
                "role": Message.Role.USER,
                "content": "Hello",
                "metadata": {},
                "created_at": datetime(2024, 1, 1, 12, 0, 0),
            },
            {
                "id": "m2",
                "role": Message.Role.ASSISTANT,
                "content": "",
                "metadata": {"tool_calls": [{"id": "tc1", "name": "x"}]},
                "created_at": datetime(2024, 1, 1, 12, 0, 1),
            },
            {
                "id": "m3",
                "role": Message.Role.TOOL,
                "content": '{"ok": true}',
                "metadata": {"tool_call_id": "tc1", "name": "x"},
                "created_at": datetime(2024, 1, 1, 12, 0, 2),
            },
            {
                "id": "m4",
                "role": Message.Role.ASSISTANT,
                "content": "Done",
                "metadata": {},
                "created_at": datetime(2024, 1, 1, 12, 0, 3),
            },
        ]
        items = _build_chat_items(messages, [])
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["type"], "message")
        self.assertEqual(items[0]["content"], "Hello")
        self.assertEqual(items[1]["type"], "message")
        self.assertEqual(items[1]["content"], "Done")

    def test_thinking_events_are_filtered_out(self):
        from apps.sessions.models import Message
        from apps.sessions.views import _build_chat_items

        messages = [
            {
                "id": "m1",
                "role": Message.Role.USER,
                "content": "Hello",
                "created_at": datetime(2024, 1, 1, 12, 0, 0),
            },
        ]
        events = [
            {
                "id": "e1",
                "kind": "thinking",
                "label": "Analyzing...",
                "detail": {},
                "created_at": datetime(2024, 1, 1, 12, 0, 1),
            },
            {
                "id": "e2",
                "kind": "tool_call_started",
                "label": "Calling tool x",
                "detail": {"name": "x"},
                "created_at": datetime(2024, 1, 1, 12, 0, 2),
            },
            {
                "id": "e3",
                "kind": "tool_result",
                "label": "Tool x completed",
                "detail": {"name": "x", "result": {"ok": True}},
                "created_at": datetime(2024, 1, 1, 12, 0, 3),
            },
        ]
        items = _build_chat_items(messages, events)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["type"], "message")
        self.assertEqual(items[0]["content"], "Hello")
        self.assertEqual(items[1]["type"], "progress")

    def test_progress_box_contains_tool_calls(self):
        from apps.sessions.models import Message
        from apps.sessions.views import _build_chat_items

        messages = [
            {
                "id": "m1",
                "role": Message.Role.USER,
                "content": "Hello",
                "created_at": datetime(2024, 1, 1, 12, 0, 0),
            },
        ]
        progress = {
            "label": "Working...",
            "active": True,
            "tool_calls_count": 1,
            "tool_calls": [{"name": "find_tools", "status": "completed", "result": {"ok": True}}],
            "reasoning": "Checking tools",
            "created_at": datetime(2024, 1, 1, 12, 0, 1).isoformat(),
        }

        items = _build_chat_items(messages, [], assistant_progress=progress)

        self.assertEqual(items[0]["type"], "message")
        self.assertEqual(items[1]["type"], "progress")
        self.assertEqual(items[1]["detail"]["tool_calls"][0]["name"], "find_tools")

    def test_progress_box_positioned_after_last_user_message(self):
        from apps.sessions.models import Message
        from apps.sessions.views import _build_chat_items

        messages = [
            {
                "id": "m1",
                "role": Message.Role.USER,
                "content": "First question",
                "created_at": datetime(2024, 1, 1, 12, 0, 0),
            },
            {
                "id": "m2",
                "role": Message.Role.ASSISTANT,
                "content": "First answer",
                "created_at": datetime(2024, 1, 1, 12, 0, 1),
            },
            {
                "id": "m3",
                "role": Message.Role.USER,
                "content": "Follow up",
                "created_at": datetime(2024, 1, 1, 12, 0, 2),
            },
        ]
        progress = {
            "label": "Working...",
            "active": True,
            "tool_calls_count": 0,
            "tool_calls": [],
            "reasoning": "",
            "created_at": datetime(2024, 1, 1, 12, 0, 3).isoformat(),
        }
        items = _build_chat_items(messages, [], assistant_progress=progress)
        self.assertEqual(len(items), 4)
        self.assertEqual(items[0]["type"], "message")
        self.assertEqual(items[0]["content"], "First question")
        self.assertEqual(items[1]["type"], "message")
        self.assertEqual(items[1]["content"], "First answer")
        self.assertEqual(items[2]["type"], "message")
        self.assertEqual(items[2]["content"], "Follow up")
        self.assertEqual(items[3]["type"], "progress")
        self.assertEqual(items[3]["label"], "Working...")

    def test_historical_progress_box_preserved_between_user_and_assistant(self):
        from apps.sessions.models import Message
        from apps.sessions.views import _build_chat_items

        messages = [
            {
                "id": "m1",
                "role": Message.Role.USER,
                "content": "Question 1",
                "created_at": datetime(2024, 1, 1, 12, 0, 0),
            },
            {
                "id": "m2",
                "role": Message.Role.ASSISTANT,
                "content": "Answer 1",
                "created_at": datetime(2024, 1, 1, 12, 0, 5),
            },
            {
                "id": "m3",
                "role": Message.Role.USER,
                "content": "Question 2",
                "created_at": datetime(2024, 1, 1, 12, 0, 6),
            },
        ]
        events = [
            {
                "id": "e1",
                "kind": "thinking",
                "label": "Analyzing...",
                "detail": {"reasoning": "Checking"},
                "created_at": datetime(2024, 1, 1, 12, 0, 1),
            },
            {
                "id": "e2",
                "kind": "tool_call_started",
                "label": "Calling x",
                "detail": {"name": "x"},
                "created_at": datetime(2024, 1, 1, 12, 0, 2),
            },
            {
                "id": "e3",
                "kind": "tool_result",
                "label": "Done",
                "detail": {"name": "x", "result": {"ok": True}},
                "created_at": datetime(2024, 1, 1, 12, 0, 3),
            },
        ]
        items = _build_chat_items(messages, events)
        types = [i["type"] for i in items]
        self.assertEqual(types, ["message", "progress", "message", "message"])
        self.assertEqual(items[0]["content"], "Question 1")
        self.assertEqual(items[1]["type"], "progress")
        self.assertFalse(items[1]["detail"]["active"])
        self.assertEqual(items[1]["detail"]["tool_calls_count"], 1)
        self.assertEqual(items[2]["content"], "Answer 1")
        self.assertEqual(items[3]["content"], "Question 2")

    def test_multiple_historical_progress_boxes_for_multiple_turns(self):
        from apps.sessions.models import Message
        from apps.sessions.views import _build_chat_items

        messages = [
            {
                "id": "m1",
                "role": Message.Role.USER,
                "content": "Q1",
                "created_at": datetime(2024, 1, 1, 12, 0, 0),
            },
            {
                "id": "m2",
                "role": Message.Role.ASSISTANT,
                "content": "A1",
                "created_at": datetime(2024, 1, 1, 12, 0, 5),
            },
            {
                "id": "m3",
                "role": Message.Role.USER,
                "content": "Q2",
                "created_at": datetime(2024, 1, 1, 12, 0, 6),
            },
            {
                "id": "m4",
                "role": Message.Role.ASSISTANT,
                "content": "A2",
                "created_at": datetime(2024, 1, 1, 12, 0, 11),
            },
        ]
        events = [
            {
                "id": "e1",
                "kind": "thinking",
                "label": "T1",
                "detail": {},
                "created_at": datetime(2024, 1, 1, 12, 0, 1),
            },
            {
                "id": "e2",
                "kind": "tool_call_started",
                "label": "X",
                "detail": {"name": "x"},
                "created_at": datetime(2024, 1, 1, 12, 0, 2),
            },
            {
                "id": "e3",
                "kind": "tool_result",
                "label": "X done",
                "detail": {"name": "x", "result": {}},
                "created_at": datetime(2024, 1, 1, 12, 0, 3),
            },
            {
                "id": "e4",
                "kind": "thinking",
                "label": "T2",
                "detail": {},
                "created_at": datetime(2024, 1, 1, 12, 0, 7),
            },
            {
                "id": "e5",
                "kind": "tool_call_started",
                "label": "Y",
                "detail": {"name": "y"},
                "created_at": datetime(2024, 1, 1, 12, 0, 8),
            },
            {
                "id": "e6",
                "kind": "tool_result",
                "label": "Y done",
                "detail": {"name": "y", "result": {}},
                "created_at": datetime(2024, 1, 1, 12, 0, 9),
            },
        ]
        items = _build_chat_items(messages, events)
        types = [i["type"] for i in items]
        self.assertEqual(
            types, ["message", "progress", "message", "message", "progress", "message"]
        )
        self.assertEqual(items[1]["detail"]["label"], "Analysis complete")
        self.assertEqual(items[1]["detail"]["tool_calls"][0]["name"], "x")
        self.assertEqual(items[4]["detail"]["tool_calls"][0]["name"], "y")

    def test_last_turn_progress_delegated_to_assistant_progress(self):
        from apps.sessions.models import Message
        from apps.sessions.views import _build_chat_items

        messages = [
            {
                "id": "m1",
                "role": Message.Role.USER,
                "content": "Q1",
                "created_at": datetime(2024, 1, 1, 12, 0, 0),
            },
            {
                "id": "m2",
                "role": Message.Role.ASSISTANT,
                "content": "A1",
                "created_at": datetime(2024, 1, 1, 12, 0, 5),
            },
            {
                "id": "m3",
                "role": Message.Role.USER,
                "content": "Q2",
                "created_at": datetime(2024, 1, 1, 12, 0, 6),
            },
        ]
        events = [
            {
                "id": "e1",
                "kind": "thinking",
                "label": "T1",
                "detail": {},
                "created_at": datetime(2024, 1, 1, 12, 0, 1),
            },
            {
                "id": "e2",
                "kind": "tool_call_started",
                "label": "X",
                "detail": {"name": "x"},
                "created_at": datetime(2024, 1, 1, 12, 0, 2),
            },
            {
                "id": "e3",
                "kind": "tool_result",
                "label": "X done",
                "detail": {"name": "x", "result": {}},
                "created_at": datetime(2024, 1, 1, 12, 0, 3),
            },
            {
                "id": "e4",
                "kind": "thinking",
                "label": "T2",
                "detail": {},
                "created_at": datetime(2024, 1, 1, 12, 0, 7),
            },
        ]
        progress = {
            "label": "Working...",
            "active": True,
            "tool_calls_count": 0,
            "tool_calls": [],
            "reasoning": "",
            "created_at": datetime(2024, 1, 1, 12, 0, 8).isoformat(),
        }
        items = _build_chat_items(messages, events, assistant_progress=progress)
        types = [i["type"] for i in items]
        self.assertEqual(types, ["message", "progress", "message", "message", "progress"])
        self.assertFalse(items[1]["detail"]["active"])
        self.assertTrue(items[4]["detail"]["active"])
        self.assertEqual(items[4]["label"], "Working...")

    def test_template_no_duplicate_progress_box_ids(self):
        from django.template.loader import render_to_string

        from apps.sessions.views import _thread_messages_context

        user = User.objects.create_user(username="tpl_user", password="pass")
        organization = Organization.objects.create(name="tpl", slug="tpl")
        OrganizationMembership.objects.create(
            user=user, organization=organization, role=OrganizationMembership.Role.OWNER
        )
        session = TSession.objects.create(organization=organization, title="TPL", created_by=user)
        thread = Thread.objects.create(tsession=session, user=user)
        Message.objects.create(thread=thread, role=Message.Role.USER, content="Q1")
        AgentEvent.objects.create(
            thread=thread, kind=AgentEvent.Kind.THINKING, label="T1", detail={}
        )
        Message.objects.create(thread=thread, role=Message.Role.ASSISTANT, content="A1")
        Message.objects.create(thread=thread, role=Message.Role.USER, content="Q2")
        AgentEvent.objects.create(
            thread=thread, kind=AgentEvent.Kind.THINKING, label="T2", detail={}
        )
        Message.objects.create(thread=thread, role=Message.Role.ASSISTANT, content="A2")

        html = render_to_string(
            "sessions/_chat_messages.html", _thread_messages_context(session, thread)
        )
        self.assertNotIn('id="agent-progress-box"', html)
        self.assertIn("agent-progress-box-complete", html)
        self.assertEqual(html.count("agent-progress-box-complete"), 2)


class MarkdownifyTests(TestCase):
    def test_markdownify_renders_headers_and_emphasis(self):
        from apps.sessions.templatetags.markdown_tags import markdownify

        html = markdownify("# Hello\n\n**bold** and *italic*")
        self.assertIn("<h1>", html)
        self.assertIn("<strong>bold</strong>", html)
        self.assertIn("<em>italic</em>", html)

    def test_markdownify_renders_code_blocks_with_pygments(self):
        from apps.sessions.templatetags.markdown_tags import markdownify

        html = markdownify("```python\nprint('hi')\n```")
        self.assertIn('class="codehilite"', html)
        self.assertIn("<pre>", html)
        self.assertIn("<code>", html)

    def test_markdownify_renders_tables(self):
        from apps.sessions.templatetags.markdown_tags import markdownify

        html = markdownify("| a | b |\n|---|---|\n| 1 | 2 |")
        self.assertIn("<table>", html)
        self.assertIn("<th>", html)
        self.assertIn("<td>1</td>", html)

    def test_markdownify_strips_javascript(self):
        from apps.sessions.templatetags.markdown_tags import markdownify

        html = markdownify("<script>alert('xss')</script>Hello")
        self.assertNotIn("<script>", html)
        self.assertIn("Hello", html)

    def test_json_pp_output_is_escaped_in_templates(self):
        from django.template import Context, Template

        rendered = Template("{% load markdown_tags %}<pre>{{ value|json_pp }}</pre>").render(
            Context({"value": {"payload": "</pre><img src=x onerror=alert(1)>"}})
        )

        self.assertIn("&lt;/pre&gt;&lt;img", rendered)
        self.assertNotIn("<img", rendered)

    def test_markdownify_empty_string(self):
        from apps.sessions.templatetags.markdown_tags import markdownify

        self.assertEqual(markdownify(""), "")


class ThreadModelTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="alice", password="pass")
        self.other = User.objects.create_user(username="bob", password="pass")
        self.organization = Organization.objects.create(name="Acme", slug="acme")
        OrganizationMembership.objects.create(
            user=self.user,
            organization=self.organization,
            role=OrganizationMembership.Role.OWNER,
        )
        OrganizationMembership.objects.create(
            user=self.other,
            organization=self.organization,
            role=OrganizationMembership.Role.MEMBER,
        )
        self.session = TSession.objects.create(
            organization=self.organization,
            title="Test Session",
            created_by=self.user,
        )

    def test_thread_can_view_owner_always(self):
        thread = Thread.objects.create(tsession=self.session, user=self.user)
        self.assertTrue(thread.can_view(self.user))

    def test_thread_can_view_public_for_org_member(self):
        thread = Thread.objects.create(
            tsession=self.session, user=self.user, visibility=Thread.Visibility.PUBLIC
        )
        self.assertTrue(thread.can_view(self.other))

    def test_thread_can_view_private_excludes_others(self):
        thread = Thread.objects.create(
            tsession=self.session, user=self.user, visibility=Thread.Visibility.PRIVATE
        )
        self.assertFalse(thread.can_view(self.other))

    def test_thread_can_view_shared_with_membership(self):
        thread = Thread.objects.create(
            tsession=self.session, user=self.user, visibility=Thread.Visibility.SHARED
        )
        ThreadMembership.objects.create(thread=thread, user=self.other)
        self.assertTrue(thread.can_view(self.other))

    def test_thread_can_view_shared_without_membership(self):
        thread = Thread.objects.create(
            tsession=self.session, user=self.user, visibility=Thread.Visibility.SHARED
        )
        self.assertFalse(thread.can_view(self.other))

    def test_visible_to_user_returns_primary_and_public(self):
        own = Thread.objects.create(tsession=self.session, user=self.user)
        public = Thread.objects.create(
            tsession=self.session, user=self.other, visibility=Thread.Visibility.PUBLIC
        )
        Thread.objects.create(
            tsession=self.session, user=self.user, visibility=Thread.Visibility.PRIVATE
        )
        visible = Thread.visible_to_user(self.session, self.user)
        self.assertIn(own, visible)
        self.assertIn(public, visible)

    def test_is_followed_by_returns_true_when_membership_exists(self):
        thread = Thread.objects.create(
            tsession=self.session, user=self.other, visibility=Thread.Visibility.PUBLIC
        )
        ThreadMembership.objects.create(thread=thread, user=self.user, is_following=True)
        self.assertTrue(thread.is_followed_by(self.user))

    def test_is_followed_by_returns_false_when_not_following(self):
        thread = Thread.objects.create(
            tsession=self.session, user=self.other, visibility=Thread.Visibility.PUBLIC
        )
        ThreadMembership.objects.create(thread=thread, user=self.user, is_following=False)
        self.assertFalse(thread.is_followed_by(self.user))

    def test_latest_messages_returns_all_messages(self):
        thread = Thread.objects.create(tsession=self.session, user=self.user)
        for i in range(55):
            Message.objects.create(
                thread=thread,
                role=Message.Role.USER,
                content=f"message {i}",
            )
        latest = thread.latest_messages()
        self.assertEqual(latest.count(), 55)
        timestamps = list(latest.values_list("created_at", flat=True))
        self.assertEqual(timestamps, sorted(timestamps))


class ThreadMembershipModelTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="alice", password="pass")
        self.organization = Organization.objects.create(name="Acme", slug="acme")
        OrganizationMembership.objects.create(
            user=self.user,
            organization=self.organization,
            role=OrganizationMembership.Role.OWNER,
        )
        self.session = TSession.objects.create(
            organization=self.organization,
            title="Test Session",
            created_by=self.user,
        )
        self.thread = Thread.objects.create(tsession=self.session, user=self.user)

    def test_membership_unique_together(self):
        ThreadMembership.objects.create(thread=self.thread, user=self.user)
        with self.assertRaises(Exception):
            ThreadMembership.objects.create(thread=self.thread, user=self.user)


class ThreadViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="alice", password="pass")
        self.other = User.objects.create_user(username="bob", password="pass")
        self.organization = Organization.objects.create(name="Acme", slug="acme")
        OrganizationMembership.objects.create(
            user=self.user,
            organization=self.organization,
            role=OrganizationMembership.Role.OWNER,
        )
        OrganizationMembership.objects.create(
            user=self.other,
            organization=self.organization,
            role=OrganizationMembership.Role.MEMBER,
        )
        self.client.force_login(self.user)
        self.session = TSession.objects.create(
            organization=self.organization,
            title="Test Session",
            status=TSession.Status.ACTIVE,
            created_by=self.user,
        )
        self.thread = Thread.objects.create(tsession=self.session, user=self.user)

    def test_session_detail_creates_primary_thread(self):
        response = self.client.get(
            reverse("sessions:session-detail", kwargs={"session_id": self.session.id})
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Your Thread")

    def test_thread_detail_partial_requires_view_permission(self):
        private_thread = Thread.objects.create(
            tsession=self.session, user=self.other, visibility=Thread.Visibility.PRIVATE
        )
        response = self.client.get(
            reverse(
                "sessions:thread-detail-partial",
                kwargs={"session_id": self.session.id, "thread_id": private_thread.id},
            )
        )
        self.assertEqual(response.status_code, 403)

    def test_thread_detail_partial_includes_usage_bar(self):
        LLMProvider.objects.create(
            organization=self.organization,
            name="Default",
            provider_type=LLMProvider.ProviderType.OLLAMA,
            model="test-model",
            enabled=True,
        )
        response = self.client.get(
            reverse(
                "sessions:thread-detail-partial",
                kwargs={"session_id": self.session.id, "thread_id": self.thread.id},
            )
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "session-usage-info")

    def test_thread_messages_context_includes_resolved_provider(self):
        from apps.sessions.views import _thread_messages_context

        context = _thread_messages_context(self.session, self.thread)
        self.assertIn("resolved_provider", context)

    def test_thread_messages_context_includes_is_mixed_models(self):
        from apps.sessions.views import _thread_messages_context

        Message.objects.create(
            thread=self.thread,
            role=Message.Role.ASSISTANT,
            content="msg1",
            model_name="model-a",
        )
        context = _thread_messages_context(self.session, self.thread)
        self.assertIn("is_mixed_models", context)
        self.assertFalse(context["is_mixed_models"])
        self.assertEqual(context["model_names"], {"model-a"})

    def test_mixed_models_indicator(self):
        other_thread = Thread.objects.create(tsession=self.session, user=self.other)
        Message.objects.create(
            thread=self.thread,
            role=Message.Role.ASSISTANT,
            content="msg1",
            model_name="model-a",
        )
        Message.objects.create(
            thread=other_thread,
            role=Message.Role.ASSISTANT,
            content="msg2",
            model_name="model-b",
        )
        response = self.client.get(
            reverse(
                "sessions:thread-detail-partial",
                kwargs={"session_id": self.session.id, "thread_id": self.thread.id},
            )
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Mixed models")
        self.assertContains(response, "Mixed pricing")

    def test_single_model_shows_name(self):
        LLMProvider.objects.create(
            organization=self.organization,
            name="Ollama",
            provider_type=LLMProvider.ProviderType.OLLAMA,
            model="test-model",
            enabled=True,
        )
        Message.objects.create(
            thread=self.thread,
            role=Message.Role.ASSISTANT,
            content="msg1",
            model_name="test-model",
        )
        response = self.client.get(
            reverse(
                "sessions:thread-detail-partial",
                kwargs={"session_id": self.session.id, "thread_id": self.thread.id},
            )
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ollama")
        self.assertNotContains(response, "Mixed models")

    def test_follow_thread_creates_membership(self):
        public_thread = Thread.objects.create(
            tsession=self.session, user=self.other, visibility=Thread.Visibility.PUBLIC
        )
        response = self.client.post(
            reverse(
                "sessions:thread-follow",
                kwargs={"session_id": self.session.id, "thread_id": public_thread.id},
            ),
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            ThreadMembership.objects.filter(thread=public_thread, user=self.user).exists()
        )

    def test_follow_thread_htmx_returns_nav_and_following_region(self):
        public_thread = Thread.objects.create(
            tsession=self.session, user=self.other, visibility=Thread.Visibility.PUBLIC
        )
        response = self.client.post(
            reverse(
                "sessions:thread-follow",
                kwargs={"session_id": self.session.id, "thread_id": public_thread.id},
            ),
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "sessions/_thread_follow_response.html")
        self.assertContains(response, "following-region")
        self.assertContains(response, "Following (1)")

    def test_thread_nav_partial_lists_new_visible_thread(self):
        Thread.objects.create(
            tsession=self.session, user=self.other, visibility=Thread.Visibility.PUBLIC
        )
        response = self.client.get(
            reverse("sessions:thread-nav-partial", kwargs={"session_id": self.session.id})
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "sessions/_thread_nav.html")
        self.assertContains(response, "bob")

    def test_unfollow_thread_deletes_membership(self):
        public_thread = Thread.objects.create(
            tsession=self.session, user=self.other, visibility=Thread.Visibility.PUBLIC
        )
        ThreadMembership.objects.create(thread=public_thread, user=self.user)
        response = self.client.post(
            reverse(
                "sessions:thread-unfollow",
                kwargs={"session_id": self.session.id, "thread_id": public_thread.id},
            ),
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            ThreadMembership.objects.filter(thread=public_thread, user=self.user).exists()
        )

    def test_thread_create_broadcasts_thread_list_update(self):
        sent = []

        class Layer:
            async def group_send(self, group, event):
                sent.append((group, event))

        with patch("apps.ws.signals.get_channel_layer", return_value=Layer()):
            thread = Thread.objects.create(
                tsession=self.session, user=self.other, visibility=Thread.Visibility.PUBLIC
            )

        self.assertIn(
            (
                f"session_{self.session.id}",
                {
                    "type": "thread_update",
                    "update_type": "thread_list",
                    "thread_id": str(thread.id),
                    "payload": {},
                },
            ),
            sent,
        )

    def test_message_broadcast_payload_contains_rendered_chat_html(self):
        sent = []

        class Layer:
            async def group_send(self, group, event):
                sent.append((group, event))

        with patch("apps.ws.signals.get_channel_layer", return_value=Layer()):
            Message.objects.create(
                thread=self.thread, role=Message.Role.USER, content="Hello over ws"
            )

        self.assertEqual(sent[0][0], f"thread_{self.thread.id}")
        event = sent[0][1]
        self.assertEqual(event["update_type"], "chat")
        self.assertIn("html", event["payload"])
        self.assertIn("Hello over ws", event["payload"]["html"])
        self.assertNotIn("items", event["payload"])

    def test_agent_event_broadcast_payload_keeps_tool_rows_inside_progress_html(self):
        sent = []

        class Layer:
            async def group_send(self, group, event):
                sent.append((group, event))

        AgentEvent.objects.create(
            thread=self.thread,
            kind=AgentEvent.Kind.THINKING,
            label="Analyzing request...",
            detail={},
        )
        AgentEvent.objects.create(
            thread=self.thread,
            kind=AgentEvent.Kind.TOOL_CALL_STARTED,
            label="Calling find_tools",
            detail={"name": "find_tools"},
        )
        with patch("apps.ws.signals.get_channel_layer", return_value=Layer()):
            AgentEvent.objects.create(
                thread=self.thread,
                kind=AgentEvent.Kind.TOOL_RESULT,
                label="find_tools completed",
                detail={"name": "find_tools", "result": {"ok": True}},
            )

        html = sent[0][1]["payload"]["html"]
        self.assertIn("agent-progress-box", html)
        self.assertIn("progress-tool-summary", html)
        self.assertIn("find_tools", html)
        self.assertNotIn("agent-event-tool_group", html)

    def test_session_detail_includes_websocket_reconnect_logic(self):
        response = self.client.get(
            reverse("sessions:session-detail", kwargs={"session_id": self.session.id})
        )

        self.assertContains(response, "connectWebSocket", html=False)
        self.assertContains(response, "scheduleReconnect", html=False)
        self.assertContains(response, "ws-status", html=False)

    def test_send_message_in_own_thread(self):
        with patch("apps.sessions.views.send_message_to_workflow_sync"):
            response = self.client.post(
                reverse(
                    "sessions:thread-send-message",
                    kwargs={"session_id": self.session.id, "thread_id": self.thread.id},
                ),
                {"content": "Hello"},
                HTTP_HX_REQUEST="true",
            )
        self.assertEqual(response.status_code, 204)
        self.assertTrue(
            Message.objects.filter(
                thread=self.thread, role=Message.Role.USER, content="Hello"
            ).exists()
        )

    def test_thread_pane_renders_messages_without_live_hx_get(self):
        Message.objects.create(thread=self.thread, role=Message.Role.USER, content="Initial msg")

        response = self.client.get(
            reverse(
                "sessions:thread-detail-partial",
                kwargs={"session_id": self.session.id, "thread_id": self.thread.id},
            )
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Initial msg")
        self.assertNotContains(response, "thread-message-list")
        self.assertNotContains(response, 'hx-trigger="load"')

    def test_send_message_in_other_thread_forbidden(self):
        other_thread = Thread.objects.create(tsession=self.session, user=self.other)
        response = self.client.post(
            reverse(
                "sessions:thread-send-message",
                kwargs={"session_id": self.session.id, "thread_id": other_thread.id},
            ),
            {"content": "Hello"},
        )
        self.assertEqual(response.status_code, 403)

    def test_thread_message_list_returns_messages(self):
        Message.objects.create(thread=self.thread, role=Message.Role.USER, content="Test msg")
        response = self.client.get(
            reverse(
                "sessions:thread-message-list",
                kwargs={"session_id": self.session.id, "thread_id": self.thread.id},
            )
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Test msg")

    def test_thread_following_card_returns_partial(self):
        public_thread = Thread.objects.create(
            tsession=self.session, user=self.other, visibility=Thread.Visibility.PUBLIC
        )
        ThreadMembership.objects.create(thread=public_thread, user=self.user)
        Message.objects.create(
            thread=public_thread, role=Message.Role.ASSISTANT, content="Latest update"
        )
        response = self.client.get(
            reverse(
                "sessions:thread-card-partial",
                kwargs={"session_id": self.session.id, "thread_id": public_thread.id},
            )
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Latest update")
        self.assertContains(response, "following-card")

    def test_thread_following_card_renders_markdown_preview(self):
        public_thread = Thread.objects.create(
            tsession=self.session, user=self.other, visibility=Thread.Visibility.PUBLIC
        )
        ThreadMembership.objects.create(thread=public_thread, user=self.user)
        Message.objects.create(
            thread=public_thread,
            role=Message.Role.ASSISTANT,
            content="The site is **reachable** with `TLS`.",
        )
        response = self.client.get(
            reverse(
                "sessions:thread-card-partial",
                kwargs={"session_id": self.session.id, "thread_id": public_thread.id},
            )
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "<strong>reachable</strong>", html=False)
        self.assertContains(response, "<code>TLS</code>", html=False)

    def test_thread_following_card_requires_following(self):
        public_thread = Thread.objects.create(
            tsession=self.session, user=self.other, visibility=Thread.Visibility.PUBLIC
        )
        response = self.client.get(
            reverse(
                "sessions:thread-card-partial",
                kwargs={"session_id": self.session.id, "thread_id": public_thread.id},
            )
        )
        self.assertEqual(response.status_code, 403)


class ThreadMembershipAPITests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="alice", password="pass")
        self.other = User.objects.create_user(username="bob", password="pass")
        self.organization = Organization.objects.create(name="Acme", slug="acme")
        OrganizationMembership.objects.create(
            user=self.user,
            organization=self.organization,
            role=OrganizationMembership.Role.OWNER,
        )
        self.client.force_login(self.user)
        self.session = TSession.objects.create(
            organization=self.organization,
            title="Test Session",
            created_by=self.user,
        )
        self.thread = Thread.objects.create(
            tsession=self.session, user=self.other, visibility=Thread.Visibility.PUBLIC
        )

    def test_thread_membership_list_returns_user_memberships(self):
        ThreadMembership.objects.create(thread=self.thread, user=self.user)
        response = self.client.get("/api/thread-memberships/")
        self.assertEqual(response.status_code, 200)
        data = response.json()["results"]
        self.assertEqual(len(data), 1)

    def test_thread_membership_create(self):
        response = self.client.post(
            "/api/thread-memberships/",
            {"thread": str(self.thread.id)},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertTrue(
            ThreadMembership.objects.filter(thread=self.thread, user=self.user).exists()
        )

    def test_thread_membership_create_fails_without_view_permission(self):
        private_thread = Thread.objects.create(
            tsession=self.session, user=self.other, visibility=Thread.Visibility.PRIVATE
        )
        response = self.client.post(
            "/api/thread-memberships/",
            {"thread": str(private_thread.id)},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)

    def test_thread_list_filtered_by_session(self):
        response = self.client.get(f"/api/threads/?session={self.session.id}")
        self.assertEqual(response.status_code, 200)
        data = response.json()["results"]
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["id"], str(self.thread.id))

    def test_thread_list_isolated_across_orgs(self):
        other_org = Organization.objects.create(name="Other", slug="other")
        other_session = TSession.objects.create(
            organization=other_org, title="Other Session", created_by=self.user
        )
        Thread.objects.create(tsession=other_session, user=self.user)
        response = self.client.get(f"/api/threads/?session={other_session.id}")
        self.assertEqual(response.status_code, 200)
        data = response.json()["results"]
        self.assertEqual(len(data), 1)


class ThreadWebSocketTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="alice", password="pass")
        self.other = User.objects.create_user(username="bob", password="pass")
        self.organization = Organization.objects.create(name="Acme", slug="acme")
        OrganizationMembership.objects.create(
            user=self.user,
            organization=self.organization,
            role=OrganizationMembership.Role.OWNER,
        )
        OrganizationMembership.objects.create(
            user=self.other,
            organization=self.organization,
            role=OrganizationMembership.Role.MEMBER,
        )
        self.session = TSession.objects.create(
            organization=self.organization,
            title="Test Session",
            created_by=self.user,
        )

    def test_visible_thread_ids_matches_expected(self):
        own = Thread.objects.create(tsession=self.session, user=self.user)
        public = Thread.objects.create(
            tsession=self.session, user=self.other, visibility=Thread.Visibility.PUBLIC
        )
        private = Thread.objects.create(
            tsession=self.session, user=self.other, visibility=Thread.Visibility.PRIVATE
        )
        visible = Thread.visible_to_user(self.session, self.user)
        self.assertIn(own, visible)
        self.assertIn(public, visible)
        self.assertNotIn(private, visible)


class CallLLMErrorClassificationTests(TransactionTestCase):
    def _create_thread(self, username="llm_errors"):
        user = User.objects.create_user(username=username, password="pass")
        organization = Organization.objects.create(name=username, slug=username)
        session = TSession.objects.create(
            organization=organization,
            title="LLM Error Classification",
            created_by=user,
        )
        return Thread.objects.create(tsession=session, user=user), session

    def _api_error(self, status_code):
        try:
            error = APIError("api failed", request=Mock(), body=None)
        except TypeError:
            error = APIError("api failed")
        error.status_code = status_code
        return error

    def _api_timeout_error(self):
        try:
            return APITimeoutError(request=Mock())
        except TypeError:
            return APITimeoutError("request timed out")

    @patch("services.llm.registry.get_llm_client")
    def test_call_llm_returns_timeout_reason(self, mock_get_client):
        thread, _session = self._create_thread("llm_timeout")
        mock_client = Mock()
        mock_client.chat = AsyncMock(side_effect=self._api_timeout_error())
        mock_get_client.return_value = mock_client

        result = asyncio.run(call_llm(str(thread.id), [{"role": "user", "content": "hi"}]))

        self.assertEqual(result["reason"], "timeout")
        self.assertTrue(result["error"])
        self.assertEqual(result["content"], "")
        self.assertEqual(result["tool_calls"], [])

    @patch("services.llm.registry.get_llm_client")
    def test_call_llm_returns_transient_error_for_5xx(self, mock_get_client):
        thread, _session = self._create_thread("llm_5xx")
        mock_client = Mock()
        mock_client.chat = AsyncMock(side_effect=self._api_error(503))
        mock_get_client.return_value = mock_client

        result = asyncio.run(call_llm(str(thread.id), [{"role": "user", "content": "hi"}]))

        self.assertEqual(result["reason"], "transient_error")

    @patch("services.llm.registry.get_llm_client")
    def test_call_llm_returns_permanent_error_for_4xx(self, mock_get_client):
        thread, _session = self._create_thread("llm_4xx")
        mock_client = Mock()
        mock_client.chat = AsyncMock(side_effect=self._api_error(401))
        mock_get_client.return_value = mock_client

        result = asyncio.run(call_llm(str(thread.id), [{"role": "user", "content": "hi"}]))

        self.assertEqual(result["reason"], "permanent_error")

    @patch("services.llm.registry.get_llm_client")
    def test_call_llm_returns_unknown_error_for_generic_exception(self, mock_get_client):
        thread, _session = self._create_thread("llm_unknown")
        mock_client = Mock()
        mock_client.chat = AsyncMock(side_effect=ValueError("boom"))
        mock_get_client.return_value = mock_client

        result = asyncio.run(call_llm(str(thread.id), [{"role": "user", "content": "hi"}]))

        self.assertEqual(result["reason"], "unknown_error")

    @patch("services.llm.registry.get_llm_client")
    def test_call_llm_returns_reasoning(self, mock_get_client):
        thread, _session = self._create_thread("llm_reasoning")
        mock_client = Mock()
        mock_client.chat = AsyncMock(
            return_value=LLMResponse(content="hi", tool_calls=[], reasoning="step 1: think")
        )
        mock_get_client.return_value = mock_client

        result = asyncio.run(call_llm(str(thread.id), [{"role": "user", "content": "hi"}]))

        self.assertEqual(result["reasoning"], "step 1: think")

    @patch("services.llm.registry.get_llm_client")
    def test_call_llm_handles_api_error_returns_unknown_reason(self, mock_get_client):
        thread, _session = self._create_thread("llm_runtime")
        mock_client = Mock()
        mock_client.chat = AsyncMock(side_effect=RuntimeError("LLM API unreachable"))
        mock_get_client.return_value = mock_client

        result = asyncio.run(call_llm(str(thread.id), [{"role": "user", "content": "hi"}]))

        self.assertEqual(result["reason"], "unknown_error")
        self.assertIn("LLM API unreachable", result["error"])

    def test_set_session_status_updates_db(self):
        _thread, session = self._create_thread("llm_set_status")

        asyncio.run(set_session_status(str(session.id), "paused"))

        session.refresh_from_db()
        self.assertEqual(session.status, TSession.Status.PAUSED)


class TroubleshootWorkflowReasonAwareTests(TestCase):
    def _make_execute_activity(
        self,
        *,
        calls=None,
        call_llm=None,
        check_completion=True,
        on_execute_tool=None,
    ):
        if calls is None:
            calls = []

        async def execute_activity(name, *args, **kwargs):
            calls.append((name, args, kwargs))
            activity_args = kwargs.get("args", [])

            if name == "initialize_session":
                return {
                    "session_id": "session-1",
                    "thread_id": "thread-1",
                    "status": "initialized",
                }
            if name == "set_session_status":
                return {"session_id": activity_args[0], "status": activity_args[1]}
            if name == "build_llm_context":
                message = activity_args[1]
                return {"messages": [{"role": "user", "content": message["content"]}]}
            if name == "call_llm":
                if callable(call_llm):
                    return call_llm(activity_args, calls)
                if call_llm is not None:
                    return call_llm
                return {"content": "Done", "tool_calls": []}
            if name == "record_agent_event":
                return {"kind": activity_args[1]}
            if name == "execute_llm_tool":
                if on_execute_tool is not None:
                    await on_execute_tool(activity_args, calls)
                return {"result": {"ok": True}, "db_tool_call_id": None}
            if name == "create_tool_call_messages":
                return {"assistant_message_id": "a", "tool_message_id": "t"}
            if name == "create_assistant_message":
                return {"message_id": "msg-1", "thread_id": "thread-1"}
            if name == "check_completion":
                if callable(check_completion):
                    return check_completion(activity_args, calls)
                return check_completion
            return {}

        return execute_activity

    def _run_workflow(self, execute_activity=None, message=None, max_iterations=12):
        workflow_instance = TroubleshootWorkflow()
        if message is None:
            message = {"content": "Check disk", "role": Message.Role.USER, "thread_id": "thread-1"}

        if execute_activity is None:
            execute_activity = self._make_execute_activity()

        sent_message = False

        async def fake_wait_condition(predicate):
            nonlocal sent_message
            if not predicate():
                if not sent_message:
                    sent_message = True
                    await workflow_instance.user_message(message)
                else:
                    await workflow_instance.complete()

        with (
            patch(
                "services.temporal_workers.workflows.workflow.execute_activity",
                execute_activity,
            ),
            patch(
                "services.temporal_workers.workflows.workflow.wait_condition",
                AsyncMock(side_effect=fake_wait_condition),
            ),
            patch("services.temporal_workers.workflows.workflow.logger", Mock()),
        ):
            return (
                asyncio.run(
                    workflow_instance.run("session-1", "thread-1", max_iterations=max_iterations)
                ),
                workflow_instance,
            )

    def test_workflow_creates_continue_prompt_on_timeout(self):
        calls = []
        execute_activity = self._make_execute_activity(
            calls=calls,
            call_llm={
                "reason": "timeout",
                "error": "API timed out",
                "content": "",
                "tool_calls": [],
            },
        )

        state, _ = self._run_workflow(execute_activity)

        self.assertEqual(state["status"], "completed")
        create_calls = [c for c in calls if c[0] == "create_assistant_message"]
        self.assertEqual(len(create_calls), 1)
        self.assertIn("Continue", create_calls[0][2]["args"][1])
        set_status_calls = [c for c in calls if c[0] == "set_session_status"]
        paused_calls = [c for c in set_status_calls if c[2].get("args") == ["session-1", "paused"]]
        self.assertEqual(len(paused_calls), 1)

    def test_workflow_creates_retry_prompt_on_transient_error(self):
        calls = []
        execute_activity = self._make_execute_activity(
            calls=calls,
            call_llm={
                "reason": "transient_error",
                "error": "503",
                "content": "",
                "tool_calls": [],
            },
        )

        self._run_workflow(execute_activity)

        create_calls = [c for c in calls if c[0] == "create_assistant_message"]
        self.assertEqual(len(create_calls), 1)
        self.assertIn("retry", create_calls[0][2]["args"][1].lower())

    def test_workflow_creates_error_prompt_on_permanent_error(self):
        calls = []
        execute_activity = self._make_execute_activity(
            calls=calls,
            call_llm={
                "reason": "permanent_error",
                "error": "auth failed",
                "content": "",
                "tool_calls": [],
            },
        )

        self._run_workflow(execute_activity)

        create_calls = [c for c in calls if c[0] == "create_assistant_message"]
        self.assertEqual(len(create_calls), 1)
        self.assertIn("auth failed", create_calls[0][2]["args"][1])

    def test_workflow_interrupts_on_new_message_during_tool_loop(self):
        workflow_instance = TroubleshootWorkflow()
        first_message = {"content": "First", "role": Message.Role.USER, "thread_id": "thread-1"}
        second_message = {"content": "Second", "role": Message.Role.USER, "thread_id": "thread-1"}
        calls = []
        sent_first_message = False
        second_message_sent = False

        async def fake_wait_condition(predicate):
            nonlocal sent_first_message
            if not predicate():
                if not sent_first_message:
                    sent_first_message = True
                    await workflow_instance.user_message(first_message)
                else:
                    await workflow_instance.complete()

        def call_llm(activity_args, _calls):
            if activity_args[1][0]["content"] == "First":
                return {
                    "content": "",
                    "tool_calls": [
                        {"name": "find_tools", "id": "tc1", "arguments": {"query": "disk"}}
                    ],
                }
            return {"content": "Second turn complete", "tool_calls": []}

        async def on_execute_tool(_activity_args, _calls):
            nonlocal second_message_sent
            if not second_message_sent:
                second_message_sent = True
                await workflow_instance.user_message(second_message)

        def check_completion(_activity_args, _calls):
            return workflow_instance.state["processed_messages"] >= 2

        execute_activity = self._make_execute_activity(
            calls=calls,
            call_llm=call_llm,
            check_completion=check_completion,
            on_execute_tool=on_execute_tool,
        )

        with (
            patch(
                "services.temporal_workers.workflows.workflow.execute_activity",
                execute_activity,
            ),
            patch(
                "services.temporal_workers.workflows.workflow.wait_condition",
                AsyncMock(side_effect=fake_wait_condition),
            ),
            patch("services.temporal_workers.workflows.workflow.logger", Mock()),
        ):
            state = asyncio.run(workflow_instance.run("session-1", "thread-1"))

        self.assertEqual(state["processed_messages"], 2)
        assistant_message_calls = [
            c[2]["args"] for c in calls if c[0] == "create_assistant_message"
        ]
        self.assertEqual(len(assistant_message_calls), 1)
        self.assertEqual(
            assistant_message_calls[0], ["thread-1", "Second turn complete", 0, 0, "0.00", ""]
        )

    def test_workflow_passes_model_name_to_assistant_message(self):
        calls = []
        execute_activity = self._make_execute_activity(
            calls=calls,
            call_llm={
                "content": "Done",
                "tool_calls": [],
                "input_tokens": 7,
                "output_tokens": 3,
                "cost": "0.0002",
                "model": "gpt-oss:20b",
            },
        )

        self._run_workflow(execute_activity)

        create_calls = [c for c in calls if c[0] == "create_assistant_message"]
        self.assertEqual(len(create_calls), 1)
        self.assertEqual(
            create_calls[0][2]["args"],
            ["thread-1", "Done", 7, 3, "0.0002", "gpt-oss:20b"],
        )

    def test_workflow_creates_continue_prompt_on_iteration_limit(self):
        calls = []
        call_llm_count = 0

        def call_llm(_activity_args, _calls):
            nonlocal call_llm_count
            call_llm_count += 1
            return {
                "content": "",
                "tool_calls": [{"name": "find_tools", "id": "1", "arguments": {}}],
            }

        execute_activity = self._make_execute_activity(calls=calls, call_llm=call_llm)

        state, _workflow_instance = self._run_workflow(
            execute_activity,
            {"content": "Loop", "role": Message.Role.USER, "thread_id": "thread-1"},
        )

        self.assertEqual(call_llm_count, 12)
        create_calls = [c for c in calls if c[0] == "create_assistant_message"]
        self.assertEqual(len(create_calls), 1)
        self.assertIn("Continue", create_calls[0][2]["args"][1])
        self.assertEqual(state["status"], "completed")
        set_status_calls = [c for c in calls if c[0] == "set_session_status"]
        paused_calls = [c for c in set_status_calls if c[2]["args"] == ["session-1", "paused"]]
        self.assertEqual(len(paused_calls), 1)

    def test_workflow_executes_multiple_tools_in_parallel(self):
        calls = []
        call_llm_count = 0

        def call_llm(_activity_args, _calls):
            nonlocal call_llm_count
            call_llm_count += 1
            if call_llm_count == 1:
                return {
                    "content": "",
                    "tool_calls": [
                        {"name": "find_tools", "id": "tc1", "arguments": {"query": "disk"}},
                        {"name": "execute_tool", "id": "tc2", "arguments": {"cmd": "df -h"}},
                        {"name": "get_prometheus_alerts", "id": "tc3", "arguments": {}},
                    ],
                }
            return {"content": "Done", "tool_calls": []}

        execute_activity = self._make_execute_activity(
            calls=calls,
            call_llm=call_llm,
        )

        state, _ = self._run_workflow(execute_activity)

        self.assertEqual(state["status"], "completed")
        execute_calls = [c for c in calls if c[0] == "execute_llm_tool"]
        self.assertEqual(len(execute_calls), 3)
        create_msg_calls = [c for c in calls if c[0] == "create_tool_call_messages"]
        self.assertEqual(len(create_msg_calls), 3)

    def test_workflow_isolates_tool_call_failures(self):
        calls = []
        call_llm_count = 0

        async def execute_activity(name, *args, **kwargs):
            calls.append((name, args, kwargs))
            activity_args = kwargs.get("args", [])

            if name == "initialize_session":
                return {
                    "session_id": "session-1",
                    "thread_id": "thread-1",
                    "status": "initialized",
                }
            if name == "set_session_status":
                return {"session_id": activity_args[0], "status": activity_args[1]}
            if name == "build_llm_context":
                message = activity_args[1]
                return {"messages": [{"role": "user", "content": message["content"]}]}
            if name == "call_llm":
                nonlocal call_llm_count
                call_llm_count += 1
                if call_llm_count == 1:
                    return {
                        "content": "",
                        "tool_calls": [
                            {"name": "find_tools", "id": "tc1", "arguments": {"query": "disk"}},
                            {"name": "execute_tool", "id": "tc2", "arguments": {"cmd": "df -h"}},
                        ],
                    }
                return {"content": "Done", "tool_calls": []}
            if name == "record_agent_event":
                return {"kind": activity_args[1]}
            if name == "execute_llm_tool":
                tc = activity_args[1]
                if tc.get("name") == "execute_tool":
                    raise RuntimeError("Simulated tool failure")
                return {"result": {"ok": True}, "db_tool_call_id": None}
            if name == "create_tool_call_messages":
                return {"assistant_message_id": "a", "tool_message_id": "t"}
            if name == "create_assistant_message":
                return {"message_id": "msg-1", "thread_id": "thread-1"}
            if name == "check_completion":
                return True
            return {}

        state, _ = self._run_workflow(execute_activity)

        self.assertEqual(state["status"], "completed")
        execute_calls = [c for c in calls if c[0] == "execute_llm_tool"]
        self.assertEqual(len(execute_calls), 2)
        create_msg_calls = [c for c in calls if c[0] == "create_tool_call_messages"]
        self.assertEqual(len(create_msg_calls), 2)


class ContinueFlagTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="continue_user", password="pass")
        self.organization = Organization.objects.create(name="Continue", slug="continue")
        OrganizationMembership.objects.create(
            user=self.user,
            organization=self.organization,
            role=OrganizationMembership.Role.OWNER,
        )
        self.client.force_login(self.user)

    def test_send_message_includes_is_continue_when_paused(self):
        session = TSession.objects.create(
            organization=self.organization,
            title="Paused Session",
            status=TSession.Status.PAUSED,
            created_by=self.user,
        )
        thread = Thread.objects.create(tsession=session, user=self.user)

        with patch("apps.sessions.views.send_message_to_workflow_sync") as send_message:
            self.client.post(
                reverse(
                    "sessions:thread-send-message",
                    kwargs={"session_id": session.id, "thread_id": thread.id},
                ),
                {"content": "hello"},
            )

        send_message.assert_called_once()
        payload = send_message.call_args.args[1]
        self.assertTrue(payload["is_continue"])

    def test_continue_button_payload_signals_paused_workflow(self):
        session = TSession.objects.create(
            organization=self.organization,
            title="Paused Session",
            status=TSession.Status.PAUSED,
            created_by=self.user,
        )
        thread = Thread.objects.create(tsession=session, user=self.user)

        with patch("apps.sessions.views.send_message_to_workflow_sync") as send_message:
            response = self.client.post(
                reverse(
                    "sessions:thread-send-message",
                    kwargs={"session_id": session.id, "thread_id": thread.id},
                ),
                {"content": "Continue"},
                HTTP_HX_REQUEST="true",
            )

        self.assertEqual(response.status_code, 204)
        send_message.assert_called_once()
        payload = send_message.call_args.args[1]
        self.assertEqual(payload["content"], "Continue")
        self.assertTrue(payload["is_continue"])

    def test_thread_pane_shows_continue_button_when_paused(self):
        session = TSession.objects.create(
            organization=self.organization,
            title="Paused Session",
            status=TSession.Status.PAUSED,
            created_by=self.user,
        )
        thread = Thread.objects.create(tsession=session, user=self.user)

        response = self.client.get(
            reverse(
                "sessions:thread-detail-partial",
                kwargs={"session_id": session.id, "thread_id": thread.id},
            )
        )

        self.assertContains(response, "Continue")
        self.assertContains(response, 'hx-vals=\'{"content": "Continue"}\'', html=False)
        self.assertContains(response, "button", html=False)


class BuildAssistantProgressTests(TestCase):
    def _create_thread(self, username="progress_user"):
        user = User.objects.create_user(username=username, password="pass")
        organization = Organization.objects.create(name=username, slug=username)
        session = TSession.objects.create(
            organization=organization,
            title="Assistant Progress",
            created_by=user,
        )
        return Thread.objects.create(tsession=session, user=user)

    def test_returns_none_when_no_recent_events(self):
        from apps.sessions.views import _build_assistant_progress

        thread = self._create_thread("progress_none")

        result = _build_assistant_progress(thread)

        self.assertIsNone(result)

    def test_returns_progress_with_tools(self):
        from apps.sessions.views import _build_assistant_progress

        thread = self._create_thread("progress_tools")
        AgentEvent.objects.create(
            thread=thread,
            kind=AgentEvent.Kind.THINKING,
            label="Thinking",
            detail={"reasoning": "Looking at symptoms"},
        )
        AgentEvent.objects.create(
            thread=thread,
            kind=AgentEvent.Kind.TOOL_CALL_STARTED,
            label="Calling find_tools",
            detail={"id": "tc1", "name": "find_tools", "arguments": {}},
        )
        AgentEvent.objects.create(
            thread=thread,
            kind=AgentEvent.Kind.TOOL_RESULT,
            label="find_tools completed",
            detail={"id": "tc1", "name": "find_tools", "result": {"ok": True}},
        )

        result = _build_assistant_progress(thread)

        self.assertIsNotNone(result)
        assert result is not None
        self.assertIn("label", result)
        self.assertEqual(result["tool_calls_count"], 1)
        self.assertEqual(result["tool_calls"][0]["status"], "completed")

    def test_returns_completed_progress_when_assistant_answer_arrived_after_thinking(self):
        from apps.sessions.views import _build_assistant_progress

        thread = self._create_thread("progress_answered")
        AgentEvent.objects.create(
            thread=thread,
            kind=AgentEvent.Kind.THINKING,
            label="Thinking",
            detail={"reasoning": "Looking at symptoms"},
        )
        AgentEvent.objects.create(
            thread=thread,
            kind=AgentEvent.Kind.TOOL_CALL_STARTED,
            label="Calling find_tools",
            detail={"id": "tc1", "name": "find_tools", "arguments": {}},
        )
        Message.objects.create(
            thread=thread,
            role=Message.Role.ASSISTANT,
            content="Here is the answer.",
        )

        result = _build_assistant_progress(thread)

        self.assertIsNotNone(result)
        assert result is not None
        self.assertFalse(result["active"])
        self.assertEqual(result["label"], "Analysis complete")
        self.assertEqual(result["tool_calls_count"], 1)

    def test_completed_progress_template_has_no_spinner(self):
        from django.template.loader import render_to_string

        from apps.sessions.views import _thread_messages_context

        thread = self._create_thread("progress_completed_template")
        Message.objects.create(thread=thread, role=Message.Role.USER, content="Question")
        AgentEvent.objects.create(
            thread=thread,
            kind=AgentEvent.Kind.THINKING,
            label="Thinking",
            detail={"reasoning": "Looking at symptoms"},
        )
        AgentEvent.objects.create(
            thread=thread,
            kind=AgentEvent.Kind.TOOL_CALL_STARTED,
            label="Calling find_tools",
            detail={"id": "tc1", "name": "find_tools", "arguments": {}},
        )
        AgentEvent.objects.create(
            thread=thread,
            kind=AgentEvent.Kind.TOOL_RESULT,
            label="find_tools completed",
            detail={"id": "tc1", "name": "find_tools", "result": {"ok": True}},
        )
        Message.objects.create(thread=thread, role=Message.Role.ASSISTANT, content="Answer")

        html = render_to_string(
            "sessions/_chat_messages.html", _thread_messages_context(thread.tsession, thread)
        )

        self.assertIn("agent-progress-box-complete", html)
        self.assertIn("progress-tool-summary", html)
        self.assertIn("progress-tool-body", html)
        self.assertIn("Analysis complete", html)
        self.assertIn("find_tools", html)
        self.assertNotIn('class="spinner"', html)

    def test_returns_progress_with_reasoning(self):
        from apps.sessions.views import _build_assistant_progress

        thread = self._create_thread("progress_reasoning")
        AgentEvent.objects.create(
            thread=thread,
            kind=AgentEvent.Kind.THINKING,
            label="Thinking",
            detail={"reasoning": "Analyze CPU..."},
        )

        result = _build_assistant_progress(thread)

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["reasoning"], "Analyze CPU...")

    def test_returns_progress_with_nested_reasoning_detail(self):
        from apps.sessions.views import _build_assistant_progress

        thread = self._create_thread("progress_nested_reasoning")
        AgentEvent.objects.create(
            thread=thread,
            kind=AgentEvent.Kind.THINKING,
            label="Reasoning",
            detail={"label": "Reasoning", "detail": {"reasoning": "Nested reasoning..."}},
        )

        result = _build_assistant_progress(thread)

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["reasoning"], "Nested reasoning...")

    def test_repeated_same_name_tool_calls_pair_results_fifo(self):
        from apps.sessions.views import _build_assistant_progress

        thread = self._create_thread("progress_repeated_tools")
        AgentEvent.objects.create(
            thread=thread,
            kind=AgentEvent.Kind.THINKING,
            label="Thinking",
            detail={},
        )
        AgentEvent.objects.create(
            thread=thread,
            kind=AgentEvent.Kind.TOOL_CALL_STARTED,
            label="Calling execute_tool",
            detail={"name": "execute_tool", "arguments": {"cmd": "first"}},
        )
        AgentEvent.objects.create(
            thread=thread,
            kind=AgentEvent.Kind.TOOL_CALL_STARTED,
            label="Calling execute_tool",
            detail={"name": "execute_tool", "arguments": {"cmd": "second"}},
        )
        AgentEvent.objects.create(
            thread=thread,
            kind=AgentEvent.Kind.TOOL_RESULT,
            label="execute_tool completed",
            detail={"name": "execute_tool", "result": {"output": "first result"}},
        )
        AgentEvent.objects.create(
            thread=thread,
            kind=AgentEvent.Kind.TOOL_RESULT,
            label="execute_tool completed",
            detail={"name": "execute_tool", "result": {"output": "second result"}},
        )

        result = _build_assistant_progress(thread)

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["tool_calls_count"], 2)
        self.assertEqual(result["tool_calls"][0]["arguments"], {"cmd": "first"})
        self.assertEqual(result["tool_calls"][0]["result"], {"output": "first result"})
        self.assertEqual(result["tool_calls"][1]["arguments"], {"cmd": "second"})
        self.assertEqual(result["tool_calls"][1]["result"], {"output": "second result"})

    def test_anchors_from_most_recent_user_message(self):
        from apps.sessions.views import _build_assistant_progress

        thread = self._create_thread("progress_anchor")
        AgentEvent.objects.create(
            thread=thread,
            kind=AgentEvent.Kind.THINKING,
            label="First thinking",
            detail={},
        )
        AgentEvent.objects.create(
            thread=thread,
            kind=AgentEvent.Kind.TOOL_CALL_STARTED,
            label="Old tool",
            detail={"name": "old_tool"},
        )
        AgentEvent.objects.create(
            thread=thread,
            kind=AgentEvent.Kind.TOOL_RESULT,
            label="Old tool completed",
            detail={"name": "old_tool", "result": {"ok": True}},
        )
        Message.objects.create(thread=thread, role=Message.Role.USER, content="New question")
        AgentEvent.objects.create(
            thread=thread,
            kind=AgentEvent.Kind.THINKING,
            label="Second thinking",
            detail={},
        )
        AgentEvent.objects.create(
            thread=thread,
            kind=AgentEvent.Kind.TOOL_CALL_STARTED,
            label="New tool",
            detail={"name": "new_tool"},
        )

        result = _build_assistant_progress(thread)

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["tool_calls_count"], 1)
        self.assertEqual(result["tool_calls"][0]["name"], "new_tool")
        self.assertEqual(result["tool_calls"][0]["status"], "started")

    def test_falls_back_to_time_window_when_no_thinking_event(self):
        from apps.sessions.views import _build_assistant_progress

        thread = self._create_thread("progress_fallback")
        AgentEvent.objects.create(
            thread=thread,
            kind=AgentEvent.Kind.TOOL_CALL_STARTED,
            label="Calling tool",
            detail={"name": "fallback_tool"},
        )

        result = _build_assistant_progress(thread)

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["tool_calls_count"], 1)
        self.assertEqual(result["tool_calls"][0]["name"], "fallback_tool")


class LLMBudgetActivityTests(TransactionTestCase):
    def _create_thread(self, username="llm_budget"):
        user = User.objects.create_user(username=username, password="pass")
        organization = Organization.objects.create(name=username, slug=username)
        session = TSession.objects.create(
            organization=organization,
            title="Budget Session",
            created_by=user,
        )
        return Thread.objects.create(tsession=session, user=user), session

    @patch("services.llm.registry.get_llm_client")
    def test_call_llm_blocks_when_context_budget_exceeded(self, mock_get_client):
        mock_client = Mock()
        mock_client.chat = AsyncMock(return_value=LLMResponse(content="ok", tool_calls=[]))
        mock_get_client.return_value = mock_client

        thread, session = self._create_thread("context_budget")
        session.execution_budget = {"max_context_tokens": 10}
        session.cumulative_context_tokens = 10
        session.save()

        result = asyncio.run(call_llm(str(thread.id), [{"role": "user", "content": "hi"}]))

        self.assertEqual(result["reason"], "budget_exceeded")
        self.assertIn("Context token budget exceeded", result["error"])
        self.assertFalse(mock_client.chat.called)

    @patch("services.llm.registry.get_llm_client")
    def test_call_llm_blocks_when_cost_budget_exceeded(self, mock_get_client):
        mock_client = Mock()
        mock_client.chat = AsyncMock(return_value=LLMResponse(content="ok", tool_calls=[]))
        mock_get_client.return_value = mock_client

        thread, session = self._create_thread("cost_budget")
        session.execution_budget = {"max_cost_per_session": "0.01"}
        session.total_cost = Decimal("0.01")
        session.save()

        result = asyncio.run(call_llm(str(thread.id), [{"role": "user", "content": "hi"}]))

        self.assertEqual(result["reason"], "budget_exceeded")
        self.assertIn("Cost budget exceeded", result["error"])
        self.assertFalse(mock_client.chat.called)

    @patch("services.llm.registry.get_llm_client")
    def test_call_llm_blocks_when_token_budget_exceeded(self, mock_get_client):
        mock_client = Mock()
        mock_client.chat = AsyncMock(return_value=LLMResponse(content="ok", tool_calls=[]))
        mock_get_client.return_value = mock_client

        thread, session = self._create_thread("token_budget")
        session.execution_budget = {"max_tokens_per_session": 100}
        session.total_tokens = 100
        session.save()

        result = asyncio.run(call_llm(str(thread.id), [{"role": "user", "content": "hi"}]))

        self.assertEqual(result["reason"], "budget_exceeded")
        self.assertIn("Token budget exceeded", result["error"])
        self.assertFalse(mock_client.chat.called)

    @patch("services.llm.registry.get_llm_client")
    def test_call_llm_with_zero_budget_limits_allows_call(self, mock_get_client):
        mock_client = Mock()
        mock_client.chat = AsyncMock(
            return_value=LLMResponse(content="ok", tool_calls=[], input_tokens=5, output_tokens=3)
        )
        mock_get_client.return_value = mock_client

        thread, session = self._create_thread("unlimited")
        result = asyncio.run(call_llm(str(thread.id), [{"role": "user", "content": "hi"}]))

        self.assertEqual(result["content"], "ok")
        self.assertEqual(result["input_tokens"], 5)
        self.assertEqual(result["output_tokens"], 3)
        self.assertTrue(mock_client.chat.called)

        session.refresh_from_db()
        self.assertEqual(session.total_input_tokens, 5)
        self.assertEqual(session.total_output_tokens, 3)
        self.assertEqual(session.total_tokens, 8)
        self.assertEqual(session.cumulative_context_tokens, 8)

    def test_create_assistant_message_persists_token_and_cost_fields(self):
        user = User.objects.create_user(username="token_msg", password="pass")
        organization = Organization.objects.create(name="TokenMsg", slug="tokenmsg")
        session = TSession.objects.create(
            organization=organization,
            title="Token Message",
            created_by=user,
        )
        thread = Thread.objects.create(tsession=session, user=user)

        result = asyncio.run(
            create_assistant_message(
                str(thread.id),
                "Hello",
                input_tokens=10,
                output_tokens=5,
                cost=Decimal("0.0001"),
            )
        )

        message = Message.objects.get(id=result["message_id"])
        self.assertEqual(message.input_tokens, 10)
        self.assertEqual(message.output_tokens, 5)
        self.assertEqual(message.cost, Decimal("0.0001"))

    def test_create_assistant_message_persists_model_name(self):
        user = User.objects.create_user(username="model_msg", password="pass")
        organization = Organization.objects.create(name="ModelMsg", slug="modelmsg")
        session = TSession.objects.create(
            organization=organization,
            title="Model Message",
            created_by=user,
        )
        thread = Thread.objects.create(tsession=session, user=user)

        result = asyncio.run(
            create_assistant_message(
                str(thread.id),
                "Hello",
                input_tokens=10,
                output_tokens=5,
                cost=Decimal("0.0001"),
                model_name="gpt-oss:20b",
            )
        )

        message = Message.objects.get(id=result["message_id"])
        self.assertEqual(message.model_name, "gpt-oss:20b")

    def test_create_assistant_message_defaults_model_name_blank(self):
        user = User.objects.create_user(username="model_blank", password="pass")
        organization = Organization.objects.create(name="ModelBlank", slug="modelblank")
        session = TSession.objects.create(
            organization=organization,
            title="Model Blank",
            created_by=user,
        )
        thread = Thread.objects.create(tsession=session, user=user)

        result = asyncio.run(create_assistant_message(str(thread.id), "Hello"))

        message = Message.objects.get(id=result["message_id"])
        self.assertEqual(message.model_name, "")

    @patch("services.llm.registry.get_llm_client")
    def test_call_llm_computes_cost_with_provider_pricing(self, mock_get_client):
        from decimal import Decimal

        mock_client = Mock()
        mock_client.chat = AsyncMock(
            return_value=LLMResponse(
                content="ok", tool_calls=[], input_tokens=1000, output_tokens=500
            )
        )
        mock_get_client.return_value = mock_client

        user = User.objects.create_user(username="pricing", password="pass")
        organization = Organization.objects.create(name="Pricing", slug="pricing")
        provider = LLMProvider.objects.create(
            organization=organization,
            name="Priced",
            provider_type=LLMProvider.ProviderType.OPENAI_COMPAT,
            model="priced-model",
            cost_per_1m_input_tokens=Decimal("0.50"),
            cost_per_1m_output_tokens=Decimal("1.50"),
        )
        session = TSession.objects.create(
            organization=organization,
            title="Cost Session",
            created_by=user,
            llm_provider=provider,
        )
        thread = Thread.objects.create(tsession=session, user=user)

        result = asyncio.run(call_llm(str(thread.id), [{"role": "user", "content": "hi"}]))

        expected_cost = Decimal("1000") * Decimal("0.50") / Decimal("1000000") + Decimal(
            "500"
        ) * Decimal("1.50") / Decimal("1000000")
        self.assertEqual(Decimal(result["cost"]), expected_cost)
        session.refresh_from_db()
        self.assertEqual(session.total_cost, expected_cost)


class TroubleshootWorkflowBudgetTests(TestCase):
    def _make_execute_activity(self, *, call_llm=None):
        async def execute_activity(name, *args, **kwargs):
            activity_args = kwargs.get("args", [])
            if name == "initialize_session":
                return {"session_id": "s", "thread_id": "t", "status": "initialized"}
            if name == "set_session_status":
                return {"session_id": activity_args[0], "status": activity_args[1]}
            if name == "build_llm_context":
                return {"messages": [{"role": "user", "content": "hi"}]}
            if name == "call_llm":
                return call_llm or {"content": "Done", "tool_calls": []}
            if name == "record_agent_event":
                return {"kind": activity_args[1]}
            if name == "create_assistant_message":
                return {"message_id": "msg-1", "thread_id": "t"}
            if name == "check_completion":
                return True
            return {}

        return execute_activity

    def _run_workflow(self, execute_activity=None, message=None):
        workflow_instance = TroubleshootWorkflow()
        if message is None:
            message = {"content": "hi", "role": "user", "thread_id": "t"}

        if execute_activity is None:
            execute_activity = self._make_execute_activity()

        sent_message = False

        async def fake_wait_condition(predicate):
            nonlocal sent_message
            if not predicate():
                if not sent_message:
                    sent_message = True
                    await workflow_instance.user_message(message)
                else:
                    await workflow_instance.complete()

        with (
            patch(
                "services.temporal_workers.workflows.workflow.execute_activity",
                execute_activity,
            ),
            patch(
                "services.temporal_workers.workflows.workflow.wait_condition",
                AsyncMock(side_effect=fake_wait_condition),
            ),
            patch("services.temporal_workers.workflows.workflow.logger", Mock()),
        ):
            return asyncio.run(workflow_instance.run("s", "t"))

    def test_workflow_tracks_cumulative_tokens_and_cost(self):
        execute_activity = self._make_execute_activity(
            call_llm={
                "content": "Done",
                "tool_calls": [],
                "input_tokens": 10,
                "output_tokens": 5,
                "cost": "0.50",
            }
        )
        state = self._run_workflow(execute_activity)

        self.assertEqual(state["cumulative_tokens"], 15)
        self.assertEqual(state["cumulative_cost"], "0.50")
        self.assertEqual(state["cumulative_context_tokens"], 15)

    def test_workflow_budget_exceeded_reason(self):
        execute_activity = self._make_execute_activity(
            call_llm={
                "reason": "budget_exceeded",
                "error": "Context token budget exceeded.",
                "content": "",
                "tool_calls": [],
            }
        )
        state = self._run_workflow(execute_activity)

        self.assertEqual(state["last_failure_reason"], "budget_exceeded")
        self.assertEqual(state["last_failure_detail"], "Context token budget exceeded.")


class MessageModelNameBackfillTests(TestCase):
    def test_backfill_sets_model_name_from_session_provider(self):
        import importlib

        from django.apps import apps as global_apps

        backfill = importlib.import_module(
            "apps.sessions.migrations.0013_message_model_name"
        ).backfill_model_name

        user = User.objects.create_user(username="backfill", password="pass")
        organization = Organization.objects.create(name="Backfill", slug="backfill")
        provider = LLMProvider.objects.create(
            organization=organization,
            name="Backfill Provider",
            provider_type=LLMProvider.ProviderType.OPENAI_COMPAT,
            model="backfill-model",
        )
        session = TSession.objects.create(
            organization=organization,
            title="Backfill Session",
            created_by=user,
            llm_provider=provider,
        )
        thread = Thread.objects.create(tsession=session, user=user)
        with_tokens = Message.objects.create(
            thread=thread,
            role=Message.Role.ASSISTANT,
            content="has tokens",
            input_tokens=10,
            output_tokens=5,
        )
        without_tokens = Message.objects.create(
            thread=thread,
            role=Message.Role.ASSISTANT,
            content="no tokens",
        )

        backfill(global_apps, None)

        with_tokens.refresh_from_db()
        without_tokens.refresh_from_db()
        self.assertEqual(with_tokens.model_name, "backfill-model")
        self.assertEqual(without_tokens.model_name, "")

    def test_backfill_falls_back_to_org_default_provider(self):
        import importlib

        from django.apps import apps as global_apps

        backfill = importlib.import_module(
            "apps.sessions.migrations.0013_message_model_name"
        ).backfill_model_name

        user = User.objects.create_user(username="backfill_default", password="pass")
        organization = Organization.objects.create(name="BackfillDefault", slug="backfilldefault")
        LLMProvider.objects.create(
            organization=organization,
            name="Default Provider",
            provider_type=LLMProvider.ProviderType.OPENAI_COMPAT,
            model="default-model",
            is_default=True,
        )
        session = TSession.objects.create(
            organization=organization,
            title="Backfill Default Session",
            created_by=user,
        )
        thread = Thread.objects.create(tsession=session, user=user)
        message = Message.objects.create(
            thread=thread,
            role=Message.Role.ASSISTANT,
            content="has tokens",
            input_tokens=1,
        )

        backfill(global_apps, None)

        message.refresh_from_db()
        self.assertEqual(message.model_name, "default-model")
