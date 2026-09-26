"""Temporal activities for Vogon."""

# pyright: reportMissingImports=false, reportAttributeAccessIssue=false

import asyncio
import json
import uuid
from datetime import datetime
from typing import Any, cast

import temporalio.activity as activity
from asgiref.sync import sync_to_async

# These will be imported from the gRPC service when running
_grpc_service = None


def set_grpc_service(service):
    """Set the gRPC service reference for activities to use."""
    global _grpc_service
    _grpc_service = service


def _setup_django_models():
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "vogon.settings")

    from django import setup as django_setup

    django_setup()


@activity.defn
async def execute_capability(
    session_id: str,
    thread_id: str,
    capability_name: str,
    parameters: dict,
    marvin_id: str,
    target_set_id: str | None = None,
    execution_mode: str | None = None,
    result_index: int = 0,
) -> dict:
    """Execute a capability on a Marvin agent via gRPC.

    This activity is executed by the Temporal worker running in the gRPC service.
    It finds the Marvin's active gRPC connection and sends the command.
    """
    if _grpc_service is None:
        raise RuntimeError("gRPC service not available")

    # marvin_id is the Marvin model UUID; we need the client_id for the stream lookup
    _setup_django_models()
    from apps.marvins.models import Marvin

    marvin = await sync_to_async(Marvin.objects.get)(id=marvin_id)
    marvin_stream = _grpc_service.get_marvin_stream(marvin.client_id)
    if marvin_stream is None:
        raise ValueError(f"Marvin {marvin.name} ({marvin.client_id}) is not connected")

    command = {
        "command_id": str(uuid.uuid4()),
        "execute_capability": {
            "session_id": session_id,
            "thread_id": thread_id,
            "capability_name": capability_name,
            "parameters_json": json.dumps(parameters),
            "deadline_unix_ms": int((datetime.utcnow().timestamp() + 300) * 1000),
            "target_set_id": target_set_id or "",
            "execution_mode": execution_mode or "single",
            "result_index": result_index,
        },
    }

    # Send command and wait for result (with timeout)
    try:
        result = await asyncio.wait_for(
            marvin_stream.send_command(command),
            timeout=300,  # 5 minutes
        )
        return result
    except TimeoutError:
        raise TimeoutError(f"Capability execution timed out for Marvin {marvin.name}")


@activity.defn
async def generate_capability_embedding(capability_id: str) -> dict:
    """Generate an embedding for a capability after creation/update."""
    _setup_django_models()

    from apps.marvins.models import Capability
    from services.embeddings.generator import EmbeddingGenerator
    from services.embeddings.registry import EmbeddingProviderFactory

    try:
        provider = EmbeddingProviderFactory.get_default_provider()
        generator = EmbeddingGenerator(provider)
        capability = await sync_to_async(Capability.objects.get)(id=capability_id)
        success = await generator.generate_for_capability(capability)
        return {"success": success, "capability_id": capability_id}
    except Capability.DoesNotExist:
        return {"success": False, "error": "Capability not found"}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


@activity.defn
async def initialize_session(session_id: str, thread_id: str) -> dict:
    """Initialize a troubleshooting session in the database."""
    return await sync_to_async(_initialize_session)(session_id, thread_id)


def _initialize_session(session_id: str, thread_id: str) -> dict:
    _setup_django_models()

    from apps.sessions.models import Thread, TSession

    session = TSession.objects.get(id=session_id)
    thread = Thread.objects.get(id=thread_id)

    session.status = TSession.Status.ACTIVE
    session.save()

    thread.status = Thread.Status.ACTIVE
    thread.save()

    return {"session_id": session_id, "thread_id": thread_id, "status": "initialized"}


@activity.defn
async def set_session_status(session_id: str, status: str) -> dict:
    """Update the session status in the database."""
    return await sync_to_async(_set_session_status)(session_id, status)


def _set_session_status(session_id: str, status: str) -> dict:
    _setup_django_models()
    from apps.sessions.models import TSession

    session = TSession.objects.get(id=session_id)
    session.status = status
    session.save(update_fields=["status", "updated_at"])
    return {"session_id": session_id, "status": status}


@activity.defn
async def check_completion(session_id: str, state: dict) -> bool:
    """Check if the troubleshooting session is complete."""
    return await sync_to_async(_check_completion)(session_id, state)


def _check_completion(session_id: str, state: dict) -> bool:
    _setup_django_models()

    from apps.sessions.models import TSession

    session = TSession.objects.get(id=session_id)

    # Session is complete if status is COMPLETED or FAILED
    return session.status in [TSession.Status.COMPLETED, TSession.Status.FAILED]


@activity.defn
async def gather_thread_context(thread_id: str) -> dict:
    """Gather context from other threads in the same session."""
    return await sync_to_async(_gather_thread_context)(thread_id)


def _gather_thread_context(thread_id: str) -> dict:
    _setup_django_models()

    from apps.sessions.models import Thread, ToolCall

    thread = Thread.objects.get(id=thread_id)
    session = thread.tsession

    # Get tool calls from other visible threads
    other_threads = session.threads.exclude(id=thread_id)
    if thread.visibility == Thread.Visibility.PRIVATE:
        other_threads = other_threads.filter(visibility=Thread.Visibility.PUBLIC)

    context = []
    for other_thread in other_threads:
        tool_calls = other_thread.tool_calls.filter(
            status=ToolCall.Status.COMPLETED,
        ).values("capability__name", "parameters", "result")
        context.extend(list(tool_calls))

    return {"thread_id": thread_id, "context": context}


@activity.defn
async def create_assistant_message(thread_id: str, content: str) -> dict:
    """Create an assistant message in the Django database."""
    return await sync_to_async(_create_assistant_message)(thread_id, content)


def _create_assistant_message(thread_id: str, content: str) -> dict:
    _setup_django_models()

    from apps.sessions.models import Message, Thread

    if not content.strip():
        return {"message_id": None, "thread_id": thread_id, "skipped": True}

    thread = Thread.objects.get(id=thread_id)
    message = Message.objects.create(
        thread=thread,
        role=Message.Role.ASSISTANT,
        content=content,
    )

    return {"message_id": str(message.id), "thread_id": thread_id}


@activity.defn
async def create_tool_call_messages(
    thread_id: str,
    tool_call: dict,
    result: Any,
    db_tool_call_id: str | None = None,
) -> dict:
    """Persist assistant tool-call and tool-result messages for LLM context."""
    return await sync_to_async(_create_tool_call_messages)(
        thread_id, tool_call, result, db_tool_call_id
    )


def _create_tool_call_messages(
    thread_id: str,
    tool_call: dict,
    result: Any,
    db_tool_call_id: str | None = None,
) -> dict:
    _setup_django_models()

    import json

    from apps.sessions.models import Message, ToolCall

    tool_call_obj = None
    if db_tool_call_id:
        try:
            tool_call_obj = ToolCall.objects.get(id=db_tool_call_id)
        except ToolCall.DoesNotExist:
            pass

    assistant_message = Message.objects.create(
        thread_id=thread_id,
        role=Message.Role.ASSISTANT,
        content="",
        tool_call=tool_call_obj,
        metadata={"tool_calls": [tool_call]},
    )

    tool_message = Message.objects.create(
        thread_id=thread_id,
        role=Message.Role.TOOL,
        content=json.dumps(result),
        tool_call=tool_call_obj,
        metadata={
            "tool_call_id": tool_call.get("id", ""),
            "name": tool_call.get("name", ""),
        },
    )

    return {
        "assistant_message_id": str(assistant_message.id),
        "tool_message_id": str(tool_message.id),
    }


@activity.defn
async def get_user_messages(thread_id: str, since: str | None = None) -> list[dict[str, Any]]:
    """Return user messages for a thread, optionally filtered by timestamp."""
    return await sync_to_async(_get_user_messages)(thread_id, since)


def _get_user_messages(thread_id: str, since: str | None = None) -> list[dict[str, Any]]:
    _setup_django_models()

    from apps.sessions.models import Message, Thread

    thread = Thread.objects.get(id=thread_id)
    messages = thread.messages.filter(role=Message.Role.USER).order_by("created_at")

    if since:
        parsed_since = datetime.fromisoformat(since.replace("Z", "+00:00"))
        if parsed_since is not None:
            messages = messages.filter(created_at__gte=parsed_since)

    return [
        {
            "content": message.content,
            "created_at": message.created_at.isoformat(),
        }
        for message in messages
    ]


SYSTEM_PROMPT = (
    "You are an expert site reliability engineering assistant. "
    "You help operators troubleshoot production issues. "
    "Be concise and actionable. "
    "When looking for capabilities, you can use natural-language queries with find_tools "
    "(e.g., 'why is this Kafka consumer falling behind?', 'high CPU on a pod', "
    "'disk full warnings')."
    "At your disposal, you have agents running on the infrastructure. "
    "These provide investigative and testing capabilities that can be looked-up with "
    "find_tools() and then executed. "
    "When multiple independent tools are needed, request them all at once isntead of one at a time"
)


@activity.defn
async def build_llm_context(thread_id: str, user_message: dict) -> dict:
    """Build the LLM message context for a thread."""
    return await sync_to_async(_build_llm_context)(thread_id, user_message)


def _build_llm_context(thread_id: str, user_message: dict) -> dict:
    _setup_django_models()

    from apps.sessions.models import Message, Thread

    thread = Thread.objects.get(id=thread_id)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    for message in thread.messages.order_by("created_at"):
        if message.role == Message.Role.TOOL:
            msg_dict = {
                "role": message.role,
                "content": message.content,
                "tool_call_id": message.metadata.get("tool_call_id", ""),
                "name": message.metadata.get("name", ""),
            }
        elif message.metadata.get("tool_calls"):
            msg_dict = {
                "role": message.role,
                "content": message.content or None,
                "tool_calls": message.metadata["tool_calls"],
            }
        else:
            if not message.content.strip():
                continue
            content = message.content
            refs = message.metadata.get("references", [])
            if refs:
                from apps.infradesigns.models import InfrastructureDesign

                design_ids = [r["id"] for r in refs if r.get("type") == "infrastructure_design"]
                if design_ids:
                    designs = InfrastructureDesign.objects.filter(id__in=design_ids)
                    extra = []
                    for d in designs:
                        extra.append(
                            f"\n[Referenced Infrastructure Design: {d.name} ({d.environment})]\n"
                            f"Description: {d.description}\n"
                            f"Marvin selector: {d.marvin_selector}\n"
                            f"Topology:\n{d.mermaid_topology}\n"
                        )
                    if extra:
                        content = content + "\n" + "\n".join(extra)
            msg_dict = {"role": message.role, "content": content}

        messages.append(msg_dict)

    return {"messages": messages}


@activity.defn
async def call_llm(thread_id: str, messages: list[dict]) -> dict:
    """Call the LLM and return the response."""
    from httpx import TimeoutException
    from openai import APIError, APITimeoutError

    from apps.sessions.models import Thread
    from services.llm.base import LLMMessage, ToolCall
    from services.llm.registry import get_llm_client
    from services.llm.tools import STANDARD_TOOLS

    thread = await sync_to_async(Thread.objects.select_related("tsession__organization").get)(
        id=thread_id
    )
    provider_id = str(thread.tsession.llm_provider_id) if thread.tsession.llm_provider_id else None
    client = await sync_to_async(get_llm_client)(str(thread.tsession.organization_id), provider_id)
    llm_messages = [
        LLMMessage(
            role=m["role"],
            content=m.get("content"),
            tool_calls=[
                ToolCall(
                    id=tc.get("id", ""),
                    name=tc.get("name", ""),
                    arguments=tc.get("arguments", {}),
                )
                for tc in m.get("tool_calls", [])
            ]
            or None,
            tool_call_id=m.get("tool_call_id"),
            name=m.get("name"),
        )
        for m in messages
    ]
    try:
        resp = await client.chat(llm_messages, tools=STANDARD_TOOLS)
    except (TimeoutError, APITimeoutError, TimeoutException) as exc:
        return {
            "content": "",
            "tool_calls": [],
            "error": f"The LLM API timed out while generating a response: {exc}",
            "reason": "timeout",
            "reasoning": "",
        }
    except APIError as exc:
        status = getattr(exc, "status_code", None)
        if status and status >= 500:
            return {
                "content": "",
                "tool_calls": [],
                "error": f"The LLM API returned a server error ({status}): {exc}",
                "reason": "transient_error",
                "reasoning": "",
            }
        else:
            return {
                "content": "",
                "tool_calls": [],
                "error": f"The LLM API returned an error: {exc}",
                "reason": "permanent_error",
                "reasoning": "",
            }
    except Exception as exc:
        return {
            "content": "",
            "tool_calls": [],
            "error": f"An unexpected error occurred: {exc}",
            "reason": "unknown_error",
            "reasoning": "",
        }
    return {
        "content": resp.content or "",
        "tool_calls": [
            {"id": t.id, "name": t.name, "arguments": t.arguments} for t in resp.tool_calls
        ],
        "reasoning": resp.reasoning or "",
    }


@activity.defn
async def execute_llm_tool(thread_id: str, tool_call: dict) -> dict | list:
    """Execute a tool call requested by the LLM."""
    from apps.sessions.models import Thread

    name = tool_call.get("name", "")
    args = tool_call.get("arguments") or tool_call.get("args") or {}

    thread = await sync_to_async(Thread.objects.select_related("tsession__organization").get)(
        id=thread_id
    )
    org = thread.tsession.organization

    if name == "find_tools":
        return await sync_to_async(_find_tools)(
            org.id,
            query=args.get("query"),
            labels=args.get("labels"),
            limit=args.get("limit", 10),
            capability_name=args.get("capability_name"),
            filters=args.get("filters"),
        )
    elif name == "execute_tool":
        tool_call, marvin_ids, error = await sync_to_async(_route_and_execute)(org, thread, args)
        if error:
            return {"db_tool_call_id": None, "result": error}
        capability_name = args.get("capability_name") or args.get("capability") or ""
        parameters = args.get("parameters", {})
        target_set_id = str(tool_call.target_set_id) if tool_call.target_set_id else None
        execution_mode = tool_call.execution_mode
        executions = await sync_to_async(_create_executions)(tool_call.id, marvin_ids)

        async def _execute_one(index: int, marvin_id: str) -> dict:
            execution_id = executions[index]["id"]
            await sync_to_async(_mark_execution_running)(execution_id)
            try:
                result = await execute_capability(
                    str(thread.tsession.id),
                    thread_id,
                    capability_name,
                    parameters,
                    marvin_id,
                    target_set_id=target_set_id,
                    execution_mode=execution_mode,
                    result_index=index,
                )
            except Exception as exc:
                await sync_to_async(_mark_execution_failed)(execution_id, str(exc))
                return {"index": index, "marvin_id": marvin_id, "error": str(exc)}

            await sync_to_async(_mark_execution_completed)(execution_id, result)
            return {"index": index, "marvin_id": marvin_id, "result": result}

        from apps.sessions.budget import release_execution_budget

        try:
            execution_results = await asyncio.gather(
                *[_execute_one(index, marvin_id) for index, marvin_id in enumerate(marvin_ids)]
            )
        finally:
            await sync_to_async(release_execution_budget)(thread.tsession, len(marvin_ids))
        results = [item for item in execution_results if "result" in item]
        errors = [item for item in execution_results if "error" in item]
        aggregated = {
            "success": not errors,
            "results": results,
            "errors": errors,
            "summary": {
                "total": len(execution_results),
                "succeeded": len(results),
                "failed": len(errors),
            },
        }
        await sync_to_async(_complete_tool_call)(tool_call.id, aggregated)
        return {"db_tool_call_id": str(tool_call.id), "result": aggregated}
    elif name == "get_prometheus_alerts":
        from django.conf import settings

        if not settings.LLM_PROMETHEUS_TOOLS_ENABLED:
            return {"error": "Prometheus tools are disabled"}
        from services.standard_tools.prometheus import get_prometheus_alerts

        return await get_prometheus_alerts()
    elif name == "query_prometheus":
        from django.conf import settings

        if not settings.LLM_PROMETHEUS_TOOLS_ENABLED:
            return {"error": "Prometheus tools are disabled"}
        from services.standard_tools.prometheus import query_prometheus

        return await query_prometheus(args.get("query", ""))
    elif name == "request_architecture_design":
        from services.standard_tools.architecture import request_architecture_design

        return await sync_to_async(request_architecture_design)(thread, args.get("description", ""))
    elif name == "get_architecture_design":
        from services.standard_tools.architecture import get_architecture_design

        return await sync_to_async(get_architecture_design)(thread)
    else:
        return {"error": f"Unknown tool {name}"}


def _find_tools(
    organization_id, query=None, labels=None, limit=10, capability_name=None, filters=None
):
    _setup_django_models()

    from apps.core.models import Organization
    from apps.marvins.discovery import DiscoveryEngine
    from apps.marvins.labels import LabelSelectorError, parse_label_selector
    from apps.marvins.selectors import TargetSelector

    org = Organization.objects.get(id=organization_id)
    try:
        selector = (
            TargetSelector.from_dict({"labels": parse_label_selector(labels)}) if labels else None
        )
    except LabelSelectorError as exc:
        return {"error": str(exc)}
    engine = DiscoveryEngine()
    response = engine.find_capabilities(
        org,
        query=query,
        selector=selector,
        limit=limit,
        capability_name=capability_name,
        filters=filters,
    )
    capabilities = response.get("results", response)
    for capability in capabilities:
        if "online_count" in capability and "online_marvins" not in capability:
            capability["online_marvins"] = capability["online_count"]
    return capabilities


def _route_and_execute(org, thread, args):
    _setup_django_models()

    from apps.marvins.discovery import BudgetExceeded, DiscoveryEngine, DiscoveryError
    from apps.marvins.labels import LabelSelectorError, parse_label_selector
    from apps.marvins.models import Capability, Marvin
    from apps.marvins.selectors import TargetSelector
    from apps.sessions.models import ToolCall

    capability_name = args.get("capability_name") or args.get("capability")
    selector_str = args.get("selector") or args.get("labels")
    timeout = args.get("timeout_seconds") or args.get("timeout", 300)

    selector = None
    try:
        if isinstance(selector_str, dict):
            selector = TargetSelector.from_dict(selector_str)
        elif selector_str:
            selector = TargetSelector.from_dict({"labels": parse_label_selector(selector_str)})
    except LabelSelectorError as exc:
        return (
            None,
            [],
            {"error": str(exc)},
        )

    from apps.sessions.budget import release_execution_budget

    engine = DiscoveryEngine()
    target_set = None
    try:
        target_set = engine.resolve_targets(
            org,
            capability_name,
            selector=selector,
            session_scope=thread.tsession.target_scope,
        )
        capability = Capability.objects.get(id=target_set.capability_id)
        snapshot = cast(list[str], target_set.snapshot)
        policy, _budget = engine.check_execution_policy(
            org,
            capability,
            len(snapshot),
            session=thread.tsession,
        )
    except BudgetExceeded as exc:
        return (
            None,
            [],
            {"error": (f"Execution budget exceeded for capability '{capability_name}': {exc}")},
        )
    except DiscoveryError as exc:
        return (
            None,
            [],
            {
                "error": (
                    f"No online Marvin found for capability '{capability_name}' "
                    f"with matching labels: {exc}"
                )
            },
        )

    snapshot = cast(list[str], target_set.snapshot)
    if not snapshot:
        release_execution_budget(thread.tsession, len(snapshot))
        return (
            None,
            [],
            {
                "error": (
                    f"No online Marvin found for capability '{capability_name}' "
                    "with matching labels"
                )
            },
        )

    try:
        marvins = list(Marvin.objects.filter(id__in=snapshot, organization=org))
        marvins_by_id = {str(marvin.id): marvin for marvin in marvins}
        marvin_ids = [str(marvin_id) for marvin_id in snapshot if str(marvin_id) in marvins_by_id]
        if not marvin_ids:
            release_execution_budget(thread.tsession, len(snapshot))
            return (
                None,
                [],
                {"error": "Resolved target set did not contain available Marvin executors"},
            )
        labels_dict = parse_label_selector(selector_str) if isinstance(selector_str, str) else {}
        execution_mode = "fanout" if len(marvin_ids) > 1 else "single"

        tool_call = ToolCall.objects.create(
            thread=thread,
            capability=capability,
            parameters=args.get("parameters", {}),
            labels=labels_dict,
            marvin=marvins_by_id.get(marvin_ids[0]),
            timeout_seconds=timeout,
            status=ToolCall.Status.IN_PROGRESS,
            target_set=target_set,
            execution_mode=execution_mode,
            policy_snapshot=policy.model_dump(),
        )

        return tool_call, marvin_ids, None
    except Exception:
        release_execution_budget(thread.tsession, len(snapshot))
        raise


def _create_executions(tool_call_id: str, marvin_ids: list[str]) -> list[dict[str, str]]:
    _setup_django_models()

    from apps.marvins.models import Marvin
    from apps.sessions.models import Execution, ToolCall

    tool_call = ToolCall.objects.get(id=tool_call_id)
    marvins = Marvin.objects.in_bulk(marvin_ids)
    executions = [
        Execution.objects.create(
            tool_call=tool_call,
            marvin=marvins.get(marvin_id),
            result_index=index,
        )
        for index, marvin_id in enumerate(marvin_ids)
    ]
    return [{"id": str(execution.id)} for execution in executions]


def _mark_execution_running(execution_id: str) -> None:
    _setup_django_models()

    from django.utils import timezone

    from apps.sessions.models import Execution

    Execution.objects.filter(id=execution_id).update(
        status=Execution.Status.RUNNING,
        started_at=timezone.now(),
    )


def _mark_execution_completed(execution_id: str, result: dict) -> None:
    _setup_django_models()

    from django.utils import timezone

    from apps.sessions.models import Execution

    Execution.objects.filter(id=execution_id).update(
        status=Execution.Status.COMPLETED,
        result=result,
        completed_at=timezone.now(),
    )


def _mark_execution_failed(execution_id: str, error: str) -> None:
    _setup_django_models()

    from django.utils import timezone

    from apps.sessions.models import Execution

    Execution.objects.filter(id=execution_id).update(
        status=Execution.Status.FAILED,
        error_message=error,
        completed_at=timezone.now(),
    )


@activity.defn
async def release_execution_budget_activity(session_id: str, target_count: int) -> dict:
    return await sync_to_async(_release_execution_budget)(session_id, target_count)


def _release_execution_budget(session_id: str, target_count: int) -> dict:
    _setup_django_models()

    from apps.sessions.budget import release_execution_budget
    from apps.sessions.models import TSession

    session = TSession.objects.get(id=session_id)
    release_execution_budget(session, target_count)
    return {"session_id": session_id, "target_count": target_count, "released": True}


def _complete_tool_call(tool_call_id: str, result: dict) -> None:
    _setup_django_models()

    from apps.sessions.models import ToolCall

    tool_call = ToolCall.objects.get(id=tool_call_id)
    tool_call.result = result
    tool_call.status = ToolCall.Status.COMPLETED
    tool_call.save()


@activity.defn
async def record_agent_event(thread_id: str, kind: str, detail: dict) -> dict:
    """Record an agent event (thinking, tool_call_started, tool_result, error)."""
    return await sync_to_async(_record_agent_event)(thread_id, kind, detail)


def _record_agent_event(thread_id: str, kind: str, detail: dict) -> dict:
    _setup_django_models()

    from apps.sessions.models import AgentEvent, Thread

    thread = Thread.objects.get(id=thread_id)

    label = detail.get("label", "")
    if not label:
        if kind == "tool_call_started":
            label = f"Calling tool {detail.get('name', '...')}"
        elif kind == "tool_result":
            label = f"Tool {detail.get('name', '...')} completed"
        elif kind == "error":
            label = f"Error: {detail.get('message', '')}"
        else:
            label = kind

    event = AgentEvent.objects.create(
        thread=thread,
        kind=kind,
        label=label,
        detail=detail,
    )
    return {"event_id": str(event.id), "kind": kind, "label": label}
