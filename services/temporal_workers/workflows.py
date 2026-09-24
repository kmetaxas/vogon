"""Temporal workflows for Vogon troubleshooting sessions."""

# pyright: reportMissingImports=false

import asyncio
import json
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

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
    async def run(self, session_id: str, thread_id: str) -> dict:
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
                for iteration in range(8):
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
                        for tc in resp["tool_calls"]:
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
                            try:
                                execute_result = await workflow.execute_activity(
                                    "execute_llm_tool",
                                    args=[msg_thread_id, tc],
                                    start_to_close_timeout=timedelta(seconds=330),
                                    retry_policy=RetryPolicy(
                                        initial_interval=timedelta(seconds=1),
                                        maximum_interval=timedelta(seconds=10),
                                        maximum_attempts=2,
                                    ),
                                )
                            except Exception as exc:
                                workflow.logger.warning(
                                    f"execute_llm_tool failed for {tc.get('name', '')}: {exc}"
                                )
                                execute_result = {"error": str(exc)}
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
                    await workflow.execute_activity(
                        "create_assistant_message",
                        args=[msg_thread_id, assistant_response],
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
