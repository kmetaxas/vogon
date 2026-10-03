"""Temporal workflows for Vogon troubleshooting sessions."""

# pyright: reportMissingImports=false

import asyncio
import json
import re
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

MAX_PARALLEL_TOOL_CALLS = 6

with workflow.unsafe.imports_passed_through():
    pass


@workflow.defn
class TroubleshootWorkflow:
    """Manages the lifecycle of a troubleshooting session."""

    def __init__(self) -> None:
        self.state: dict = {
            "session_id": None,
            "thread_id": None,
            "status": "pending",
            "processed_messages": 0,
            "pending_messages": 0,
            "last_message": None,
            "iteration_count": 0,
            "cumulative_tool_time_ms": 0,
            "interrupted": False,
            "last_failure_reason": None,
            "last_failure_detail": None,
            "cumulative_tokens": 0,
            "cumulative_cost": "0.00",
            "cumulative_context_tokens": 0,
        }
        self._pending_messages: list[dict] = []
        self._completion_requested = False

    @workflow.signal
    async def user_message(self, message: dict) -> None:
        """Accept a user message for the workflow to process."""
        self._pending_messages.append(message)
        self.state["pending_messages"] = len(self._pending_messages)
        self.state["last_message"] = message
        if self.state["status"] == "pending":
            self.state["status"] = "waiting_for_user"
        elif self.state["status"] == "paused_waiting_for_continue":
            self.state["status"] = "processing"

    @workflow.signal
    async def complete(self) -> None:
        self._completion_requested = True

    @workflow.query
    def get_status(self) -> dict:
        """Return the current workflow state."""
        return {
            **self.state,
            "pending_messages": len(self._pending_messages),
        }

    @workflow.run
    async def run(
        self,
        session_id: str,
        thread_id: str,
        max_iterations: int = 8,
    ) -> dict:
        """Run the troubleshooting workflow for a given session and thread."""
        workflow.logger.info(f"Starting TroubleshootWorkflow for session {session_id}")

        self.state.update(
            {
                "session_id": session_id,
                "thread_id": thread_id,
                "status": "initializing",
                "processed_messages": 0,
                "pending_messages": 0,
                "last_message": None,
                "max_iterations": max_iterations,
            }
        )

        # Initialize session state
        state = await workflow.execute_activity(
            "initialize_session",
            args=[session_id, thread_id],
            start_to_close_timeout=timedelta(seconds=30),
        )
        self.state.update(state)
        self.state["status"] = "waiting_for_user"

        while True:
            workflow.logger.info(f"Waiting for next message for session {session_id}")
            await workflow.wait_condition(
                lambda: bool(self._pending_messages) or self._completion_requested
            )

            if self._pending_messages:
                message = self._pending_messages.pop(0)
                # Ensure DB reflects we're processing (resumes from paused if needed)
                await workflow.execute_activity(
                    "set_session_status",
                    args=[session_id, "active"],
                    start_to_close_timeout=timedelta(seconds=10),
                )
                self.state["status"] = "processing"
                self.state["last_message"] = message
                msg_thread_id = message.get("thread_id", thread_id)

                context = await workflow.execute_activity(
                    "build_llm_context",
                    args=[msg_thread_id, message],
                    start_to_close_timeout=timedelta(seconds=30),
                )

                assistant_response = ""
                assistant_message_created = False
                resp: dict = {}
                reason: str | None = None
                for iteration in range(max_iterations):
                    self.state["iteration_count"] = iteration
                    # Check if user sent a new message while we were working
                    if self._pending_messages:
                        self.state["interrupted"] = True
                        context["messages"].append(
                            {
                                "role": "system",
                                "content": (
                                    "[INTERRUPTION] The user sent a new message while "
                                    f"the assistant was working on the previous request. "
                                    f"The assistant had completed {iteration} tool-call "
                                    "iteration(s). Please acknowledge the interruption "
                                    "and address the new user message."
                                ),
                            }
                        )
                        break

                    await workflow.execute_activity(
                        "record_agent_event",
                        args=[msg_thread_id, "thinking", {"label": "Analyzing request..."}],
                        start_to_close_timeout=timedelta(seconds=10),
                    )
                    resp = await workflow.execute_activity(
                        "call_llm",
                        args=[msg_thread_id, context["messages"]],
                        start_to_close_timeout=timedelta(seconds=600),
                    )

                    if resp.get("reasoning"):
                        await workflow.execute_activity(
                            "record_agent_event",
                            args=[
                                msg_thread_id,
                                "thinking",
                                {
                                    "label": "Reasoning",
                                    "detail": {"reasoning": resp["reasoning"]},
                                },
                            ],
                            start_to_close_timeout=timedelta(seconds=10),
                        )

                    reason = resp.get("reason")

                    if reason is None:
                        from decimal import Decimal

                        self.state["cumulative_tokens"] += resp.get("input_tokens", 0) + resp.get(
                            "output_tokens", 0
                        )
                        self.state["cumulative_cost"] = str(
                            Decimal(self.state["cumulative_cost"])
                            + Decimal(resp.get("cost", "0.00"))
                        )
                        self.state["cumulative_context_tokens"] += resp.get(
                            "input_tokens", 0
                        ) + resp.get("output_tokens", 0)

                    if reason == "budget_exceeded":
                        self.state["last_failure_reason"] = "budget_exceeded"
                        self.state["last_failure_detail"] = resp.get("error")
                        await workflow.execute_activity(
                            "create_assistant_message",
                            args=[
                                msg_thread_id,
                                (
                                    "The assistant cannot continue because the session budget has "
                                    f"been exceeded: {resp.get('error', 'Unknown budget limit')}"
                                ),
                                0,
                                0,
                                "0.00",
                            ],
                            start_to_close_timeout=timedelta(seconds=30),
                            retry_policy=RetryPolicy(
                                initial_interval=timedelta(seconds=1),
                                maximum_interval=timedelta(seconds=5),
                                maximum_attempts=3,
                            ),
                        )
                        assistant_message_created = True
                        self.state["status"] = "paused_waiting_for_continue"
                        await workflow.execute_activity(
                            "set_session_status",
                            args=[session_id, "paused"],
                            start_to_close_timeout=timedelta(seconds=10),
                        )
                        break

                    if reason == "timeout":
                        self.state["last_failure_reason"] = "timeout"
                        self.state["last_failure_detail"] = resp.get("error")
                        context["messages"].append(
                            {
                                "role": "system",
                                "content": (
                                    f"[TIMEOUT] The assistant's previous response timed out: "
                                    f"{resp.get('error', 'Unknown timeout')}. Please continue "
                                    f"from where you left off."
                                ),
                            }
                        )
                        await workflow.execute_activity(
                            "create_assistant_message",
                            args=[
                                msg_thread_id,
                                (
                                    "The assistant was working on your request but "
                                    "ran into a time limit. Click **Continue** to "
                                    "let the assistant pick up where it left off."
                                ),
                            ],
                            start_to_close_timeout=timedelta(seconds=30),
                            retry_policy=RetryPolicy(
                                initial_interval=timedelta(seconds=1),
                                maximum_interval=timedelta(seconds=5),
                                maximum_attempts=3,
                            ),
                        )
                        assistant_message_created = True
                        self.state["status"] = "paused_waiting_for_continue"
                        await workflow.execute_activity(
                            "set_session_status",
                            args=[session_id, "paused"],
                            start_to_close_timeout=timedelta(seconds=10),
                        )
                        break

                    if reason in ("transient_error", "unknown_error"):
                        self.state["last_failure_reason"] = reason
                        self.state["last_failure_detail"] = resp.get("error")
                        context["messages"].append(
                            {
                                "role": "system",
                                "content": (
                                    f"[ERROR] The assistant encountered a retryable error: "
                                    f"{resp.get('error', 'Unknown error')}. Please retry."
                                ),
                            }
                        )
                        await workflow.execute_activity(
                            "create_assistant_message",
                            args=[
                                msg_thread_id,
                                (
                                    "The assistant encountered a temporary error while processing "
                                    "your request. Please retry in a moment."
                                ),
                            ],
                            start_to_close_timeout=timedelta(seconds=30),
                            retry_policy=RetryPolicy(
                                initial_interval=timedelta(seconds=1),
                                maximum_interval=timedelta(seconds=5),
                                maximum_attempts=3,
                            ),
                        )
                        assistant_message_created = True
                        break

                    if reason == "permanent_error":
                        self.state["last_failure_reason"] = "permanent_error"
                        self.state["last_failure_detail"] = resp.get("error")
                        await workflow.execute_activity(
                            "create_assistant_message",
                            args=[
                                msg_thread_id,
                                (
                                    "The assistant encountered an error while processing your "
                                    f"request: {resp.get('error', 'Unknown error')}"
                                ),
                            ],
                            start_to_close_timeout=timedelta(seconds=30),
                            retry_policy=RetryPolicy(
                                initial_interval=timedelta(seconds=1),
                                maximum_interval=timedelta(seconds=5),
                                maximum_attempts=3,
                            ),
                        )
                        assistant_message_created = True
                        break

                    assistant_response = resp["content"]

                    if resp.get("tool_calls"):
                        tool_calls = resp["tool_calls"]
                        for tc in tool_calls:
                            await workflow.execute_activity(
                                "record_agent_event",
                                args=[
                                    msg_thread_id,
                                    "thinking",
                                    {"label": f"Calling {tc['name']}..."},
                                ],
                                start_to_close_timeout=timedelta(seconds=10),
                            )
                            await workflow.execute_activity(
                                "record_agent_event",
                                args=[
                                    msg_thread_id,
                                    "tool_call_started",
                                    {
                                        "id": tc.get("id", ""),
                                        "name": tc.get("name", ""),
                                        "arguments": tc.get("arguments", {}),
                                    },
                                ],
                                start_to_close_timeout=timedelta(seconds=10),
                            )

                        _all_execute_results: list[Any] = []
                        for i in range(0, len(tool_calls), MAX_PARALLEL_TOOL_CALLS):
                            batch = tool_calls[i : i + MAX_PARALLEL_TOOL_CALLS]
                            batch_tasks = [
                                workflow.execute_activity(
                                    "execute_llm_tool",
                                    args=[msg_thread_id, tc],
                                    start_to_close_timeout=timedelta(seconds=330),
                                    retry_policy=RetryPolicy(
                                        initial_interval=timedelta(seconds=1),
                                        maximum_interval=timedelta(seconds=10),
                                        maximum_attempts=2,
                                    ),
                                )
                                for tc in batch
                            ]
                            batch_results = await asyncio.gather(
                                *batch_tasks, return_exceptions=True
                            )
                            _all_execute_results.extend(batch_results)

                        for tc, execute_result in zip(tool_calls, _all_execute_results):
                            if isinstance(execute_result, BaseException):
                                tool_name = tc.get("name", "")
                                workflow.logger.warning(
                                    f"execute_llm_tool failed for {tool_name}: {execute_result}"
                                )
                                tool_result = {"error": str(execute_result)}
                                db_tool_call_id = None
                                await workflow.execute_activity(
                                    "record_agent_event",
                                    args=[
                                        msg_thread_id,
                                        "tool_result",
                                        {
                                            "id": tc.get("id", ""),
                                            "name": tc.get("name", ""),
                                            "result": tool_result,
                                        },
                                    ],
                                    start_to_close_timeout=timedelta(seconds=10),
                                )
                                await workflow.execute_activity(
                                    "create_tool_call_messages",
                                    args=[msg_thread_id, tc, tool_result, db_tool_call_id],
                                    start_to_close_timeout=timedelta(seconds=10),
                                )
                                context["messages"].append(
                                    {"role": "assistant", "tool_calls": [tc]}
                                )
                                context["messages"].append(
                                    {
                                        "role": "tool",
                                        "tool_call_id": tc.get("id", ""),
                                        "name": tc.get("name", ""),
                                        "content": json.dumps(tool_result),
                                    }
                                )
                                continue

                            if (
                                isinstance(execute_result, dict)
                                and "db_tool_call_id" in execute_result
                            ):
                                db_tool_call_id = execute_result.get("db_tool_call_id")
                                tool_result = execute_result["result"]
                            else:
                                db_tool_call_id = None
                                tool_result = execute_result
                            await workflow.execute_activity(
                                "record_agent_event",
                                args=[
                                    msg_thread_id,
                                    "tool_result",
                                    {
                                        "id": tc.get("id", ""),
                                        "name": tc.get("name", ""),
                                        "result": tool_result,
                                    },
                                ],
                                start_to_close_timeout=timedelta(seconds=10),
                            )
                            await workflow.execute_activity(
                                "create_tool_call_messages",
                                args=[msg_thread_id, tc, tool_result, db_tool_call_id],
                                start_to_close_timeout=timedelta(seconds=10),
                            )
                            context["messages"].append({"role": "assistant", "tool_calls": [tc]})
                            context["messages"].append(
                                {
                                    "role": "tool",
                                    "tool_call_id": tc.get("id", ""),
                                    "name": tc.get("name", ""),
                                    "content": json.dumps(tool_result),
                                }
                            )
                        await workflow.execute_activity(
                            "record_agent_event",
                            args=[msg_thread_id, "thinking", {"label": "Processing results..."}],
                            start_to_close_timeout=timedelta(seconds=10),
                        )
                    else:
                        break
                else:
                    # Loop exhausted all 8 iterations without breaking
                    self.state["last_failure_reason"] = "limit"
                    self.state["last_failure_detail"] = "Maximum tool-call iterations reached"
                    context["messages"].append(
                        {
                            "role": "system",
                            "content": (
                                "[LIMIT] The assistant's previous response was interrupted "
                                "because the maximum number of iterations was reached. "
                                "Please continue from where you left off."
                            ),
                        }
                    )
                    await workflow.execute_activity(
                        "create_assistant_message",
                        args=[
                            msg_thread_id,
                            (
                                "The assistant was working on your request but ran into a time "
                                "limit. Click **Continue** to let the assistant pick up where it "
                                "left off."
                            ),
                        ],
                        start_to_close_timeout=timedelta(seconds=30),
                        retry_policy=RetryPolicy(
                            initial_interval=timedelta(seconds=1),
                            maximum_interval=timedelta(seconds=5),
                            maximum_attempts=3,
                        ),
                    )
                    assistant_message_created = True
                    self.state["status"] = "paused_waiting_for_continue"
                    await workflow.execute_activity(
                        "set_session_status",
                        args=[session_id, "paused"],
                        start_to_close_timeout=timedelta(seconds=10),
                    )

                if not self.state["interrupted"] and not assistant_message_created:
                    assistant_args = [msg_thread_id, assistant_response]
                    if reason is None:
                        assistant_args += [
                            resp.get("input_tokens", 0),
                            resp.get("output_tokens", 0),
                            resp.get("cost", "0.00"),
                            resp.get("model", ""),
                        ]
                    await workflow.execute_activity(
                        "create_assistant_message",
                        args=assistant_args,
                        start_to_close_timeout=timedelta(seconds=30),
                        retry_policy=RetryPolicy(
                            initial_interval=timedelta(seconds=1),
                            maximum_interval=timedelta(seconds=5),
                            maximum_attempts=3,
                        ),
                    )
                self.state["interrupted"] = False

                self.state["processed_messages"] += 1
                self.state["pending_messages"] = len(self._pending_messages)

                if self.state["status"] == "paused_waiting_for_continue":
                    continue

            if self._completion_requested or await workflow.execute_activity(
                "check_completion",
                args=[session_id, self.state],
                start_to_close_timeout=timedelta(seconds=10),
            ):
                self.state["status"] = "completed"
                break

            self.state["status"] = "waiting_for_user"
            self.state["pending_messages"] = len(self._pending_messages)

        workflow.logger.info(f"TroubleshootWorkflow completed for session {session_id}")
        return self.state


@workflow.defn
class ThreadWorkflow:
    """Manages individual threads within a troubleshooting session."""

    @workflow.run
    async def run(self, thread_id: str, user_id: str) -> str:
        """Run a thread workflow for a specific user's investigation."""
        workflow.logger.info(f"Starting ThreadWorkflow for thread {thread_id}")

        # Thread-specific logic - can reference other threads for context
        context = await workflow.execute_activity(
            "gather_thread_context",
            args=[thread_id],
            start_to_close_timeout=timedelta(seconds=30),
        )

        # Thread can run independently or coordinate with session workflow
        # This allows multiple users to investigate simultaneously

        workflow.logger.info(f"ThreadWorkflow completed for thread {thread_id}")
        return context


@workflow.defn
class CheckWorkflow:
    """Executes a scheduled Check in deterministic or AI-assisted mode."""

    def __init__(self) -> None:
        self.state: dict = {
            "check_id": None,
            "execution_id": None,
            "status": "pending",
            "mode": None,
            "cumulative_tokens": 0,
            "cumulative_cost": "0.00",
            "last_failure_reason": None,
            "last_failure_detail": None,
        }

    @workflow.query
    def get_status(self) -> dict:
        return self.state

    @workflow.run
    async def run(
        self,
        check_id: str,
        version_id: str,
        dry_run: bool = False,
    ) -> dict:
        workflow.logger.info(f"Starting CheckWorkflow for check {check_id}")
        self.state["check_id"] = check_id
        self.state["status"] = "initializing"

        context = await workflow.execute_activity(
            "load_check_context",
            args=[check_id, version_id],
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(
                initial_interval=timedelta(seconds=1),
                maximum_interval=timedelta(seconds=5),
                maximum_attempts=3,
            ),
        )
        self.state["mode"] = context.get("execution_mode", "deterministic")
        self.state.update(context)

        if context.get("error"):
            raise ApplicationError(f"Check not found: {context['error']}")

        execution = await workflow.execute_activity(
            "update_check_execution",
            args=[check_id, None, "running", "unknown", {}],
            start_to_close_timeout=timedelta(seconds=30),
        )
        self.state["execution_id"] = execution.get("id")

        try:
            capability_results = []
            capabilities = context.get("evaluation_config", {}).get("capabilities", [])
            if capabilities:
                self.state["status"] = "executing_capabilities"

                async def execute_one_capability(cap_config: dict) -> dict:
                    try:
                        result = await workflow.execute_activity(
                            "execute_capability",
                            args=[
                                check_id,
                                self.state["execution_id"],
                                cap_config["name"],
                                cap_config.get("parameters", {}),
                                cap_config.get("marvin_id", ""),
                            ],
                            start_to_close_timeout=timedelta(minutes=5),
                            retry_policy=RetryPolicy(
                                initial_interval=timedelta(seconds=1),
                                maximum_interval=timedelta(seconds=30),
                                maximum_attempts=3,
                            ),
                        )
                        return {"capability": cap_config["name"], "success": True, "result": result}
                    except Exception as exc:
                        return {
                            "capability": cap_config["name"],
                            "success": False,
                            "error": str(exc),
                        }

                capability_results = await asyncio.gather(
                    *(execute_one_capability(capability) for capability in capabilities)
                )

            evaluation_config = context.get("evaluation_config", {})

            if self.state["mode"] == "deterministic":
                self.state["status"] = "evaluating"
                rules = evaluation_config.get("rules", [])
                execution_result = {
                    "capabilities": capability_results,
                    "timestamp": datetime.utcnow().isoformat(),
                }
                evaluation = await workflow.execute_activity(
                    "evaluate_check",
                    args=[check_id, execution_result, rules],
                    start_to_close_timeout=timedelta(seconds=30),
                )
            else:
                self.state["status"] = "ai_evaluating"
                system_prompt = (
                    "You are evaluating infrastructure health check results. "
                    "Review the collected evidence and provide a structured assessment.\n\n"
                    "Respond with JSON containing:\n"
                    "- state: one of healthy/degraded/critical/unknown\n"
                    "- confidence: float 0.0-1.0\n"
                    "- findings: list of {severity, message, path, expected, actual}\n"
                    "- summary: brief text summary"
                )
                messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps({"capabilities": capability_results})},
                ]
                resp = await workflow.execute_activity(
                    "call_llm",
                    args=[
                        None,
                        messages,
                        context.get("llm_provider_id"),
                        context.get("organization_id"),
                    ],
                    start_to_close_timeout=timedelta(minutes=2),
                    retry_policy=RetryPolicy(
                        initial_interval=timedelta(seconds=1),
                        maximum_interval=timedelta(seconds=30),
                        maximum_attempts=3,
                    ),
                )
                evaluation = self._parse_llm_evaluation(resp.get("content", ""))
                self.state["cumulative_tokens"] += resp.get("input_tokens", 0) + resp.get(
                    "output_tokens", 0
                )
                self.state["cumulative_cost"] = str(
                    Decimal(self.state["cumulative_cost"]) + Decimal(str(resp.get("cost", "0.00")))
                )

            await workflow.execute_activity(
                "update_check_execution",
                args=[
                    check_id,
                    self.state["execution_id"],
                    "completed",
                    evaluation.get("state", "unknown"),
                    {
                        "findings": evaluation.get("findings", []),
                        "summary": evaluation.get("summary", ""),
                        "confidence": evaluation.get("confidence", 0.5),
                        "capabilities": capability_results,
                    },
                ],
                start_to_close_timeout=timedelta(seconds=30),
            )

            if not dry_run:
                await workflow.execute_activity(
                    "dispatch_actions",
                    args=[check_id, self.state["execution_id"], evaluation.get("findings", [])],
                    start_to_close_timeout=timedelta(seconds=30),
                )

            self.state["status"] = "completed"
            workflow.logger.info(f"CheckWorkflow completed for check {check_id}")
            return {
                **self.state,
                "evaluation": evaluation,
                "capability_results": capability_results,
            }
        except Exception as exc:
            await workflow.execute_activity(
                "update_check_execution",
                args=[
                    check_id,
                    self.state["execution_id"],
                    "failed",
                    "unknown",
                    {"error": str(exc)},
                ],
                start_to_close_timeout=timedelta(seconds=30),
            )
            raise

    def _parse_llm_evaluation(self, content: str) -> dict:
        json_match = re.search(r"```json\s*(.*?)\s*```", content, re.DOTALL)
        if json_match:
            try:
                return self._normalize_evaluation(json.loads(json_match.group(1)))
            except json.JSONDecodeError:
                pass

        try:
            return self._normalize_evaluation(json.loads(content))
        except json.JSONDecodeError:
            pass

        state = "unknown"
        content_lower = content.lower()
        if "critical" in content_lower:
            state = "critical"
        elif "degraded" in content_lower:
            state = "degraded"
        elif "healthy" in content_lower:
            state = "healthy"

        return {
            "state": state,
            "confidence": 0.5,
            "findings": [{"severity": "warning", "message": content[:500]}],
            "summary": content[:1000],
        }

    def _normalize_evaluation(self, evaluation: dict) -> dict:
        state = evaluation.get("state", "unknown")
        if state not in {"healthy", "degraded", "critical", "unknown"}:
            state = "unknown"
        findings = evaluation.get("findings")
        if not isinstance(findings, list):
            findings = []
        return {
            "state": state,
            "confidence": evaluation.get("confidence", 0.5),
            "findings": findings,
            "summary": evaluation.get("summary", ""),
        }


@workflow.defn
class AutonomousInvestigationWorkflow:
    """Non-interactive LLM-driven investigation for autonomous Checks."""

    def __init__(self) -> None:
        self.state: dict = {
            "check_id": None,
            "execution_id": None,
            "status": "pending",
            "iteration_count": 0,
            "cumulative_tokens": 0,
            "cumulative_cost": "0.00",
            "cumulative_context_tokens": 0,
            "last_failure_reason": None,
            "last_failure_detail": None,
        }

    @workflow.query
    def get_status(self) -> dict:
        return self.state

    @workflow.run
    async def run(
        self,
        check_id: str,
        version_id: str,
        dry_run: bool = False,
        max_iterations: int = 8,
    ) -> dict:
        workflow.logger.info(f"Starting AutonomousInvestigationWorkflow for check {check_id}")
        self.state["check_id"] = check_id
        self.state["status"] = "initializing"

        context = await workflow.execute_activity(
            "load_check_context",
            args=[check_id, version_id],
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(
                initial_interval=timedelta(seconds=1),
                maximum_interval=timedelta(seconds=5),
                maximum_attempts=3,
            ),
        )
        self.state.update(context)

        if context.get("error"):
            raise ApplicationError(f"Check not found: {context['error']}")

        execution = await workflow.execute_activity(
            "update_check_execution",
            args=[check_id, None, "running", "unknown", {}],
            start_to_close_timeout=timedelta(seconds=30),
        )
        self.state["execution_id"] = execution.get("id")

        try:
            system_prompt = (
                "You are an autonomous infrastructure investigation agent. "
                "Your goal is to investigate the following check:\n\n"
                f"Check: {context.get('check_name', 'Unknown')}\n"
                f"Goal: {context.get('investigation_goal', 'Investigate and report findings')}\n\n"
                "You have access to tools to gather information. "
                "When you have gathered sufficient information, provide a final summary "
                "with your findings and a health assessment (healthy/degraded/critical/unknown). "
                "Include confidence score (0.0-1.0) and detailed findings."
            )
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": "Begin investigation."},
            ]
            investigation_results = []
            self.state["status"] = "investigating"

            for iteration in range(max_iterations):
                self.state["iteration_count"] = iteration + 1
                workflow.logger.info(f"Autonomous iteration {iteration + 1}/{max_iterations}")

                resp = await workflow.execute_activity(
                    "call_llm",
                    args=[
                        None,
                        messages,
                        context.get("llm_provider_id"),
                        context.get("organization_id"),
                    ],
                    start_to_close_timeout=timedelta(minutes=2),
                    retry_policy=RetryPolicy(
                        initial_interval=timedelta(seconds=1),
                        maximum_interval=timedelta(seconds=30),
                        maximum_attempts=3,
                    ),
                )
                self._track_llm_usage(resp)

                reason = resp.get("reason")
                if reason:
                    self.state["last_failure_reason"] = reason
                    self.state["last_failure_detail"] = resp.get("error")
                    messages.append(
                        {
                            "role": "system",
                            "content": (
                                f"[LLM_ERROR] The investigation LLM returned {reason}: "
                                f"{resp.get('error', 'Unknown error')}. "
                                "Continue with available evidence."
                            ),
                        }
                    )
                    investigation_results.append(
                        {"iteration": iteration + 1, "error": resp.get("error", "")}
                    )
                    break

                tool_calls = resp.get("tool_calls", [])
                if tool_calls:
                    tool_results = []
                    for tool_call in tool_calls:
                        tool_result = await workflow.execute_activity(
                            "execute_llm_tool",
                            args=[tool_call],
                            start_to_close_timeout=timedelta(minutes=2),
                            retry_policy=RetryPolicy(
                                initial_interval=timedelta(seconds=1),
                                maximum_interval=timedelta(seconds=30),
                                maximum_attempts=3,
                            ),
                        )
                        tool_results.append(tool_result)

                    messages.append(
                        {
                            "role": "assistant",
                            "content": resp.get("content", ""),
                            "tool_calls": tool_calls,
                        }
                    )
                    for tool_call, tool_result in zip(tool_calls, tool_results):
                        messages.append(
                            {
                                "role": "tool",
                                "content": json.dumps(tool_result),
                                "tool_call_id": (
                                    tool_result.get("tool_call_id", tool_call.get("id", ""))
                                    if isinstance(tool_result, dict)
                                    else tool_call.get("id", "")
                                ),
                                "name": tool_call.get("name", ""),
                            }
                        )
                    investigation_results.append(
                        {"iteration": iteration + 1, "tools": tool_results}
                    )
                    continue

                messages.append({"role": "assistant", "content": resp.get("content", "")})
                investigation_results.append(
                    {"iteration": iteration + 1, "response": resp.get("content", "")}
                )
                break
            else:
                self.state["status"] = "max_iterations_reached"
                self.state["last_failure_reason"] = "limit"
                self.state["last_failure_detail"] = "Maximum investigation iterations reached"
                workflow.logger.warning(
                    f"Autonomous investigation reached max iterations ({max_iterations})"
                )

            final_prompt = (
                "Based on your investigation, provide a structured JSON response with:\n"
                "- state: one of healthy/degraded/critical/unknown\n"
                "- confidence: float 0.0-1.0\n"
                "- findings: list of {severity, message, path, expected, actual}\n"
                "- summary: brief text summary"
            )
            messages.append({"role": "user", "content": final_prompt})

            final_resp = await workflow.execute_activity(
                "call_llm",
                args=[
                    None,
                    messages,
                    context.get("llm_provider_id"),
                    context.get("organization_id"),
                ],
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=RetryPolicy(
                    initial_interval=timedelta(seconds=1),
                    maximum_interval=timedelta(seconds=30),
                    maximum_attempts=3,
                ),
            )
            self._track_llm_usage(final_resp)

            evaluation = self._parse_evaluation(final_resp.get("content", ""))
            await workflow.execute_activity(
                "update_check_execution",
                args=[
                    check_id,
                    self.state["execution_id"],
                    "completed",
                    evaluation["state"],
                    {
                        "findings": evaluation["findings"],
                        "summary": evaluation.get("summary", ""),
                        "confidence": evaluation.get("confidence", 0.5),
                    },
                ],
                start_to_close_timeout=timedelta(seconds=30),
            )

            if not dry_run:
                await workflow.execute_activity(
                    "dispatch_actions",
                    args=[check_id, self.state["execution_id"], evaluation["findings"]],
                    start_to_close_timeout=timedelta(seconds=30),
                )

            self.state["status"] = "completed"
            workflow.logger.info(f"AutonomousInvestigationWorkflow completed for check {check_id}")
            return {
                **self.state,
                "evaluation": evaluation,
                "investigation_results": investigation_results,
            }
        except Exception as exc:
            await workflow.execute_activity(
                "update_check_execution",
                args=[
                    check_id,
                    self.state["execution_id"],
                    "failed",
                    "unknown",
                    {"error": str(exc)},
                ],
                start_to_close_timeout=timedelta(seconds=30),
            )
            raise

    def _track_llm_usage(self, resp: dict) -> None:
        input_tokens = resp.get("input_tokens", 0)
        output_tokens = resp.get("output_tokens", 0)
        total_tokens = input_tokens + output_tokens
        self.state["cumulative_tokens"] += total_tokens
        self.state["cumulative_context_tokens"] += total_tokens
        self.state["cumulative_cost"] = str(
            Decimal(self.state["cumulative_cost"]) + Decimal(str(resp.get("cost", "0.00")))
        )

    def _parse_evaluation(self, content: str) -> dict:
        json_match = re.search(r"```json\s*(.*?)\s*```", content, re.DOTALL)
        if json_match:
            try:
                return self._normalize_evaluation(json.loads(json_match.group(1)))
            except json.JSONDecodeError:
                pass

        try:
            return self._normalize_evaluation(json.loads(content))
        except json.JSONDecodeError:
            pass

        state = "unknown"
        content_lower = content.lower()
        if "critical" in content_lower:
            state = "critical"
        elif "degraded" in content_lower:
            state = "degraded"
        elif "healthy" in content_lower:
            state = "healthy"

        return {
            "state": state,
            "confidence": 0.5,
            "findings": [{"severity": "warning", "message": content[:500]}],
            "summary": content[:1000],
        }

    def _normalize_evaluation(self, evaluation: dict) -> dict:
        state = evaluation.get("state", "unknown")
        if state not in {"healthy", "degraded", "critical", "unknown"}:
            state = "unknown"
        findings = evaluation.get("findings")
        if not isinstance(findings, list):
            findings = []
        return {
            "state": state,
            "confidence": evaluation.get("confidence", 0.5),
            "findings": findings,
            "summary": evaluation.get("summary", ""),
        }


@workflow.defn
class CapabilityExecutionWorkflow:
    """Dedicated workflow for executing a single capability on a Marvin."""

    @workflow.run
    async def run(
        self,
        session_id: str,
        thread_id: str,
        capability_name: str,
        parameters: dict,
        marvin_ids: str | list[str],
    ) -> dict:
        """Execute a capability on a Marvin agent."""
        target_ids = [marvin_ids] if isinstance(marvin_ids, str) else marvin_ids
        workflow.logger.info(
            f"Executing capability {capability_name} on {len(target_ids)} Marvin(s)"
        )

        async def execute_one(marvin_id: str) -> dict[str, Any]:
            try:
                execution_result = await workflow.execute_activity(
                    "execute_capability",
                    args=[
                        session_id,
                        thread_id,
                        capability_name,
                        parameters,
                        marvin_id,
                    ],
                    start_to_close_timeout=timedelta(minutes=5),
                    retry_policy=RetryPolicy(
                        initial_interval=timedelta(seconds=1),
                        maximum_interval=timedelta(seconds=30),
                        maximum_attempts=3,
                    ),
                )
                return {"marvin_id": marvin_id, "success": True, "result": execution_result}
            except Exception as exc:
                return {"marvin_id": marvin_id, "success": False, "error": str(exc)}

        results = await asyncio.gather(*(execute_one(str(marvin_id)) for marvin_id in target_ids))
        errors = [
            execution_result for execution_result in results if not execution_result["success"]
        ]
        result = {
            "results": results,
            "errors": errors,
            "summary": {
                "target_count": len(results),
                "success_count": len(results) - len(errors),
                "error_count": len(errors),
            },
        }

        workflow.logger.info(f"Capability {capability_name} execution completed")
        return result
