"""Temporal activities for Vogon."""

# pyright: reportMissingImports=false, reportAttributeAccessIssue=false

import asyncio
import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal
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


def _resolve_selector_marvin_id(check_id: str, capability_name: str, selector: dict) -> str:
    """Resolve a single Marvin id for a Check capability selector."""
    _setup_django_models()

    from apps.checks.models import Check
    from apps.marvins.discovery import DiscoveryEngine
    from apps.marvins.selectors import TargetSelector

    check = Check.objects.get(id=check_id)
    target_set = DiscoveryEngine().resolve_targets(
        check.organization,
        capability_name,
        selector=TargetSelector.from_dict(selector),
    )
    snapshot: list[str] = list(target_set.snapshot or [])  # type: ignore[arg-type]
    if not snapshot:
        raise ValueError(
            f"No online Marvin found for capability '{capability_name}' with selector {selector}"
        )
    return str(snapshot[0])


@activity.defn
async def execute_capability(
    session_id: str,
    thread_id: str,
    capability_name: str,
    parameters: dict,
    marvin_id: str,
    selector: dict | None = None,
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

    if not marvin_id and selector:
        marvin_id = await sync_to_async(_resolve_selector_marvin_id)(
            session_id, capability_name, selector
        )

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
            "deadline_unix_ms": int((datetime.now(UTC).timestamp() + 300) * 1000),
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
    thread = Thread.objects.select_related("tsession__personality").get(id=thread_id)

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

    thread = Thread.objects.select_related("tsession__personality").get(id=thread_id)
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
async def create_assistant_message(
    thread_id: str,
    content: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost: str = "0.00",
    model_name: str = "",
) -> dict:
    """Create an assistant message in the Django database."""
    return await sync_to_async(_create_assistant_message)(
        thread_id, content, input_tokens, output_tokens, cost, model_name
    )


def _create_assistant_message(
    thread_id: str,
    content: str,
    input_tokens: int,
    output_tokens: int,
    cost: str,
    model_name: str = "",
) -> dict:
    _setup_django_models()

    from decimal import Decimal

    from apps.sessions.models import Message, Thread

    if not content.strip():
        return {"message_id": None, "thread_id": thread_id, "skipped": True}

    thread = Thread.objects.select_related("tsession__personality").get(id=thread_id)
    message = Message.objects.create(
        thread=thread,
        role=Message.Role.ASSISTANT,
        content=content,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost=Decimal(cost),
        model_name=model_name,
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

    thread = Thread.objects.select_related("tsession__personality").get(id=thread_id)
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

    thread = Thread.objects.select_related("tsession__personality").get(id=thread_id)
    system_prompt = SYSTEM_PROMPT
    if thread.tsession.personality:
        system_prompt += "\n\n" + thread.tsession.personality.prompt_text
    messages = [{"role": "system", "content": system_prompt}]

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
async def call_llm(
    thread_id: str | None = None,
    messages: list[dict] | None = None,
    llm_provider_id: str | None = None,
    organization_id: str | None = None,
) -> dict:
    """Call the LLM and return the response.

    ``llm_provider_id`` is an optional explicit provider override (used by Check
    workflows, which carry their own per-Check provider). When omitted, the
    provider configured on the Thread's session is used.

    ``thread_id`` is optional: Check workflows have no Thread, so they pass
    ``organization_id`` instead. When ``thread_id`` is omitted, all session
    budget checks are skipped and ``organization_id`` is used to resolve the
    LLM client.
    """
    from decimal import Decimal

    from httpx import TimeoutException
    from openai import APIError, APITimeoutError

    from apps.sessions.budget import SessionBudget
    from apps.sessions.models import Thread
    from services.llm.base import LLMMessage, ToolCall
    from services.llm.registry import get_llm_client
    from services.llm.tools import STANDARD_TOOLS

    messages = messages or []

    thread = None
    if thread_id:
        thread = await sync_to_async(
            Thread.objects.select_related(
                "tsession__organization", "tsession__llm_provider", "tsession__personality"
            ).get
        )(id=thread_id)

        # Budget pre-check: if limits are set and already exceeded, block the call.
        budget = SessionBudget.from_session(thread.tsession)
        if (
            budget.max_context_tokens > 0
            and thread.tsession.cumulative_context_tokens >= budget.max_context_tokens
        ):
            return {
                "content": "",
                "tool_calls": [],
                "error": "Context token budget exceeded.",
                "reason": "budget_exceeded",
                "reasoning": "",
                "input_tokens": 0,
                "output_tokens": 0,
                "cost": "0.00",
                "model": "",
            }
        if (
            budget.max_cost_per_session > 0
            and thread.tsession.total_cost >= budget.max_cost_per_session
        ):
            return {
                "content": "",
                "tool_calls": [],
                "error": "Cost budget exceeded.",
                "reason": "budget_exceeded",
                "reasoning": "",
                "input_tokens": 0,
                "output_tokens": 0,
                "cost": "0.00",
                "model": "",
            }
        if (
            budget.max_tokens_per_session > 0
            and thread.tsession.total_tokens >= budget.max_tokens_per_session
        ):
            return {
                "content": "",
                "tool_calls": [],
                "error": "Token budget exceeded.",
                "reason": "budget_exceeded",
                "reasoning": "",
                "input_tokens": 0,
                "output_tokens": 0,
                "cost": "0.00",
                "model": "",
            }

    if thread is not None:
        provider_id = llm_provider_id or (
            str(thread.tsession.llm_provider_id) if thread.tsession.llm_provider_id else None
        )
        client_organization_id = str(thread.tsession.organization_id)
    else:
        if organization_id is None:
            raise ValueError("call_llm requires either thread_id or organization_id")
        provider_id = llm_provider_id
        client_organization_id = organization_id

    client = await sync_to_async(get_llm_client)(client_organization_id, provider_id)
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
            "input_tokens": 0,
            "output_tokens": 0,
            "cost": "0.00",
            "model": "",
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
                "input_tokens": 0,
                "output_tokens": 0,
                "cost": "0.00",
                "model": "",
            }
        else:
            return {
                "content": "",
                "tool_calls": [],
                "error": f"The LLM API returned an error: {exc}",
                "reason": "permanent_error",
                "reasoning": "",
                "input_tokens": 0,
                "output_tokens": 0,
                "cost": "0.00",
                "model": "",
            }
    except Exception as exc:
        return {
            "content": "",
            "tool_calls": [],
            "error": f"An unexpected error occurred: {exc}",
            "reason": "unknown_error",
            "reasoning": "",
            "input_tokens": 0,
            "output_tokens": 0,
            "cost": "0.00",
            "model": "",
        }

    # Resolve provider: explicit override → session provider → org default.
    cost = Decimal("0.00")
    provider = None
    if llm_provider_id:
        from apps.llm.models import LLMProvider

        provider = await sync_to_async(LLMProvider.objects.filter(id=llm_provider_id).first)()
    if not provider and thread is not None:
        provider = thread.tsession.llm_provider
    if not provider:
        from apps.llm.models import LLMProvider

        fallback_organization_id = (
            str(thread.tsession.organization_id) if thread is not None else organization_id
        )
        provider = await sync_to_async(
            lambda: (
                LLMProvider.objects.filter(organization_id=fallback_organization_id, enabled=True)
                .order_by("-is_default")
                .first()
            )
        )()
    if provider:
        cost = Decimal(resp.input_tokens) * provider.cost_per_1m_input_tokens / Decimal(
            "1000000"
        ) + Decimal(resp.output_tokens) * provider.cost_per_1m_output_tokens / Decimal("1000000")

    # Update TSession counters atomically.
    if thread is not None:
        await sync_to_async(_update_session_usage)(
            str(thread.tsession.id),
            resp.input_tokens,
            resp.output_tokens,
            str(cost),
        )

    return {
        "content": resp.content or "",
        "tool_calls": [
            {"id": t.id, "name": t.name, "arguments": t.arguments} for t in resp.tool_calls
        ],
        "reasoning": resp.reasoning or "",
        "input_tokens": resp.input_tokens,
        "output_tokens": resp.output_tokens,
        "cost": str(cost),
        "model": resp.model or "",
    }


@activity.defn
async def execute_llm_tool(
    thread_id: str | None, tool_call: dict, organization_id: str | None = None
) -> dict | list:
    """Execute a tool call requested by the LLM."""
    from apps.core.models import Organization
    from apps.sessions.models import Thread

    name = tool_call.get("name", "")
    args = tool_call.get("arguments") or tool_call.get("args") or {}

    org = None
    thread = None
    if thread_id:
        thread = await sync_to_async(
            Thread.objects.select_related("tsession__organization", "tsession__personality").get
        )(id=thread_id)
        org = thread.tsession.organization
    elif organization_id:
        org = await sync_to_async(Organization.objects.get)(id=organization_id)
    else:
        return {"error": "No thread_id or organization_id provided for execute_llm_tool"}

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
        if not thread:
            return {"error": "execute_tool requires a thread context"}
        assert thread is not None
        assert thread_id is not None
        tool_call, marvin_ids, error = await sync_to_async(_route_and_execute)(org, thread, args)
        if error:
            return {"db_tool_call_id": None, "result": error}
        capability_name = args.get("capability_name") or args.get("capability") or ""
        parameters = args.get("parameters", {})
        target_set_id = str(tool_call.target_set_id) if tool_call.target_set_id else None
        execution_mode = tool_call.execution_mode
        executions = await sync_to_async(_create_executions)(tool_call.id, marvin_ids)

        from services.temporal_workers import fanout

        async def update_status(
            execution_id: str, status: str, result: dict | None, error: str | None
        ) -> None:
            await sync_to_async(fanout.update_execution_status)(
                execution_id,
                status,
                result,
                error,
                update_record=_update_session_execution_status,
            )

        from apps.sessions.budget import release_execution_budget

        try:
            execution_results = await fanout.execute_fanout(
                marvin_ids,
                capability_name,
                parameters,
                execute_capability=execute_capability,
                execution_ids=[execution["id"] for execution in executions],
                update_status=update_status,
                session_id=str(thread.tsession.id),
                thread_id=str(thread_id),
                target_set_id=target_set_id,
                execution_mode=execution_mode,
            )
        finally:
            await sync_to_async(release_execution_budget)(thread.tsession, len(marvin_ids))
        aggregated = fanout.aggregate_results(execution_results)
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
        if not thread:
            return {"error": "request_architecture_design requires a thread context"}
        from services.standard_tools.architecture import request_architecture_design

        return await sync_to_async(request_architecture_design)(thread, args.get("description", ""))
    elif name == "get_architecture_design":
        if not thread:
            return {"error": "get_architecture_design requires a thread context"}
        from services.standard_tools.architecture import get_architecture_design

        return await sync_to_async(get_architecture_design)(thread)
    else:
        return {"error": f"Unknown tool {name}"}


def _find_tools(
    organization_id, query=None, labels=None, limit=10, capability_name=None, filters=None
):
    _setup_django_models()

    from apps.core.models import Organization
    from apps.marvins.discovery import CapabilityDiscoveryError, resolve_capabilities

    org = Organization.objects.get(id=organization_id)
    try:
        response = resolve_capabilities(
            org,
            query=query,
            labels=labels,
            limit=limit,
            capability_name=capability_name,
            filters=filters,
        )
    except CapabilityDiscoveryError as exc:
        return {"error": str(exc)}
    capabilities = response.get("results", response)
    for capability in capabilities:
        if "online_count" in capability and "online_marvins" not in capability:
            capability["online_marvins"] = capability["online_count"]
    return capabilities


def _route_and_execute(org, thread, args):
    _setup_django_models()

    from apps.marvins.discovery import BudgetExceeded, DiscoveryEngine, DiscoveryError
    from apps.marvins.labels import LabelSelectorError, parse_label_selector
    from apps.marvins.models import Marvin
    from apps.marvins.selectors import TargetSelector
    from apps.sessions.models import ToolCall
    from services.temporal_workers import fanout

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
        target_set, capability, marvin_ids, policy_snapshot = fanout.resolve_targets(
            engine,
            org,
            capability_name,
            selector=selector,
            target_scope=thread.tsession.target_scope,
            session=thread.tsession,
        )
        snapshot = cast(list[str], target_set.snapshot)
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
            policy_snapshot=policy_snapshot,
        )

        return tool_call, marvin_ids, None
    except Exception:
        release_execution_budget(thread.tsession, len(snapshot))
        raise


def _create_executions(tool_call_id: str, marvin_ids: list[str]) -> list[dict[str, str]]:
    _setup_django_models()

    from apps.marvins.models import Marvin
    from apps.sessions.models import Execution, ToolCall
    from services.temporal_workers import fanout

    tool_call = ToolCall.objects.get(id=tool_call_id)
    marvins = Marvin.objects.in_bulk(marvin_ids)
    return fanout.create_execution_records(
        "session_tool_call",
        str(tool_call_id),
        marvin_ids,
        tool_call.capability,
        tool_call.target_set,
        tool_call.policy_snapshot,
        create_record=lambda _parent_type, _parent_id, marvin_id, _capability, _target_set, _policy, index: (  # noqa: E501
            Execution.objects.create(
                tool_call=tool_call,
                marvin=marvins.get(marvin_id),
                result_index=index,
            )
        ),
    )


def _mark_execution_running(execution_id: str) -> None:
    from services.temporal_workers import fanout

    fanout.update_execution_status(
        execution_id,
        "running",
        update_record=_update_session_execution_status,
    )


def _mark_execution_completed(execution_id: str, result: dict) -> None:
    from services.temporal_workers import fanout

    fanout.update_execution_status(
        execution_id,
        "completed",
        result=result,
        update_record=_update_session_execution_status,
    )


def _mark_execution_failed(execution_id: str, error: str) -> None:
    from services.temporal_workers import fanout

    fanout.update_execution_status(
        execution_id,
        "failed",
        error=error,
        update_record=_update_session_execution_status,
    )


def _update_session_execution_status(
    execution_id: str, status: str, result: dict | None = None, error: str | None = None
) -> None:
    _setup_django_models()

    from django.utils import timezone

    from apps.sessions.models import Execution

    updates: dict[str, Any] = {}
    if status == "running":
        updates = {"status": Execution.Status.RUNNING, "started_at": timezone.now()}
    elif status == "completed":
        updates = {
            "status": Execution.Status.COMPLETED,
            "result": result,
            "completed_at": timezone.now(),
        }
    elif status == "failed":
        updates = {
            "status": Execution.Status.FAILED,
            "error_message": error,
            "completed_at": timezone.now(),
        }
    else:
        raise ValueError(f"Unsupported execution status: {status}")
    Execution.objects.filter(id=execution_id).update(**updates)


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


def _update_session_usage(
    session_id: str, input_tokens: int, output_tokens: int, cost: str
) -> dict:
    _setup_django_models()
    from decimal import Decimal

    from django.db.models import F
    from django.utils import timezone

    from apps.sessions.models import TSession

    TSession.objects.filter(id=session_id).update(
        total_input_tokens=F("total_input_tokens") + input_tokens,
        total_output_tokens=F("total_output_tokens") + output_tokens,
        total_tokens=F("total_tokens") + input_tokens + output_tokens,
        total_cost=F("total_cost") + Decimal(cost),
        cumulative_context_tokens=F("cumulative_context_tokens") + input_tokens + output_tokens,
        updated_at=timezone.now(),
    )
    # Manually trigger usage broadcast since QuerySet.update() doesn't emit post_save
    from apps.ws.signals import broadcast_usage

    session = TSession.objects.get(id=session_id)
    broadcast_usage(TSession, session, created=False)
    return {"session_id": session_id, "updated": True}


@activity.defn
async def record_agent_event(thread_id: str, kind: str, detail: dict) -> dict:
    """Record an agent event (thinking, tool_call_started, tool_result, error)."""
    return await sync_to_async(_record_agent_event)(thread_id, kind, detail)


def _record_agent_event(thread_id: str, kind: str, detail: dict) -> dict:
    _setup_django_models()

    from apps.sessions.models import AgentEvent, Thread

    thread = Thread.objects.select_related("tsession__personality").get(id=thread_id)

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


@activity.defn
async def load_check_context(check_id: str, version_id: str) -> dict:
    """Load Check + Version snapshot for workflow execution."""
    _setup_django_models()
    from apps.checks.models import Check, CheckVersion

    try:
        check = await sync_to_async(
            Check.objects.select_related(
                "organization", "llm_provider", "target_scope", "personality", "created_by"
            ).get
        )(id=check_id)
    except Check.DoesNotExist:
        return {"error": "Check not found", "check_id": check_id}

    version = None
    if version_id:
        try:
            version = await sync_to_async(CheckVersion.objects.get)(id=version_id, check=check)
        except CheckVersion.DoesNotExist:
            pass

    from apps.checks.models import normalize_execution_budget

    context = {
        "check_id": str(check.id),
        "check_name": check.name,
        "organization_id": str(check.organization_id),
        "notification_config": check.notification_config or {},
        "execution_budget": normalize_execution_budget(check.execution_budget),
        "llm_provider_id": str(check.llm_provider_id) if check.llm_provider_id else None,
        "target_scope_id": str(check.target_scope_id) if check.target_scope_id else None,
        "personality_prompt": check.personality.prompt_text if check.personality else None,
        "investigation_goal": "Investigate and report findings",
    }

    if version:
        context["version_snapshot"] = version.definition_snapshot

    return context


@activity.defn
async def update_check_execution(
    check_id: str,
    execution_id: str | None,
    status: str,
    health_state: str,
    result: dict,
    evidence: dict | list | None = None,
    resolved_targets: dict | None = None,
) -> dict:
    """Update or create a CheckExecution record."""
    _setup_django_models()
    from django.utils import timezone

    from apps.checks.models import Check, CheckExecution

    status_enum = getattr(
        CheckExecution.ExecutionStatus,
        status.upper(),
        CheckExecution.ExecutionStatus.PENDING,
    )
    health_enum = getattr(
        CheckExecution.HealthState,
        health_state.upper(),
        CheckExecution.HealthState.UNKNOWN,
    )
    terminal_statuses = (
        CheckExecution.ExecutionStatus.COMPLETED,
        CheckExecution.ExecutionStatus.FAILED,
    )
    now = timezone.now()

    if execution_id:
        try:
            execution = await sync_to_async(CheckExecution.objects.get)(id=execution_id)
            execution.execution_status = status_enum
            execution.health_state = health_enum
            execution.evaluation_result = result
            if evidence is not None:
                execution.evidence = evidence
            if resolved_targets is not None:
                execution.resolved_targets = resolved_targets
            execution.input_tokens = result.get("input_tokens", execution.input_tokens)
            execution.output_tokens = result.get("output_tokens", execution.output_tokens)
            execution.cost = Decimal(result.get("cost", str(execution.cost)))
            if status_enum == CheckExecution.ExecutionStatus.RUNNING and not execution.started_at:
                execution.started_at = now
            if status_enum in terminal_statuses:
                if not execution.started_at:
                    execution.started_at = now
                execution.completed_at = now
            await sync_to_async(execution.save)()
            return {"id": str(execution.id), "status": status, "health_state": health_state}
        except CheckExecution.DoesNotExist:
            pass

    try:
        check = await sync_to_async(Check.objects.get)(id=check_id)
    except Check.DoesNotExist:
        return {
            "id": execution_id,
            "status": status,
            "health_state": health_state,
            "error": "Check not found",
        }

    create_kwargs: dict[str, Any] = {
        "check": check,
        "execution_status": status_enum,
        "health_state": health_enum,
        "evaluation_result": result,
        "input_tokens": result.get("input_tokens", 0),
        "output_tokens": result.get("output_tokens", 0),
        "cost": Decimal(result.get("cost", "0.00")),
    }
    if evidence is not None:
        create_kwargs["evidence"] = evidence
    if resolved_targets is not None:
        create_kwargs["resolved_targets"] = resolved_targets
    if status_enum == CheckExecution.ExecutionStatus.RUNNING:
        create_kwargs["started_at"] = now
    if status_enum in terminal_statuses:
        create_kwargs["started_at"] = now
        create_kwargs["completed_at"] = now

    execution = await sync_to_async(CheckExecution.objects.create)(**create_kwargs)
    return {"id": str(execution.id), "status": status, "health_state": health_state}


@activity.defn
async def create_autonomous_session(check_id: str, execution_id: str) -> dict:
    return await sync_to_async(_create_autonomous_session)(check_id, execution_id)


def _create_autonomous_session(check_id: str, execution_id: str) -> dict:
    _setup_django_models()

    from django.db import transaction

    from apps.checks.models import CheckExecution
    from apps.sessions.models import Message, Thread, TSession

    try:
        with transaction.atomic():
            execution = CheckExecution.objects.select_related(
                "check",
                "check__organization",
                "check__llm_provider",
                "check__created_by",
            ).get(id=execution_id, check_id=check_id)

            evaluation_result = execution.evaluation_result or {}
            session_id = evaluation_result.get("autonomous_session_id")
            thread_id = evaluation_result.get("autonomous_thread_id")
            if session_id and thread_id:
                return {"session_id": str(session_id), "thread_id": str(thread_id)}

            check = execution.check
            initial_content = (
                check.instructions or check.description or "Investigate and report findings"
            )
            if session_id:
                session = TSession.objects.get(id=session_id)
                thread = Thread.objects.filter(
                    tsession=session,
                    title="Autonomous Investigation",
                ).first()
                if thread is None:
                    thread = Thread.objects.create(
                        tsession=session,
                        title="Autonomous Investigation",
                        user=None,
                        status=Thread.Status.ACTIVE,
                    )
                    Message.objects.create(
                        thread=thread,
                        role=Message.Role.USER,
                        content=initial_content,
                    )

                evaluation_result["autonomous_thread_id"] = str(thread.id)
                execution.evaluation_result = evaluation_result
                execution.save(update_fields=["evaluation_result"])
                return {"session_id": str(session.id), "thread_id": str(thread.id)}

            session = TSession.objects.create(
                organization=check.organization,
                title=f"Autonomous Check: {check.name}",
                status=TSession.Status.ACTIVE,
                is_autonomous=True,
                target_scope=check.target_scope,
                execution_budget=check.execution_budget or {},
                llm_provider=check.llm_provider,
                created_by=check.created_by,
            )
            thread = Thread.objects.create(
                tsession=session,
                title="Autonomous Investigation",
                user=None,
                status=Thread.Status.ACTIVE,
            )
            Message.objects.create(
                thread=thread,
                role=Message.Role.USER,
                content=initial_content,
            )

            evaluation_result.update(
                {
                    "autonomous_session_id": str(session.id),
                    "autonomous_thread_id": str(thread.id),
                }
            )
            execution.evaluation_result = evaluation_result
            execution.save(update_fields=["evaluation_result"])

            return {"session_id": str(session.id), "thread_id": str(thread.id)}
    except CheckExecution.DoesNotExist:
        return {
            "error": "CheckExecution not found",
            "check_id": check_id,
            "execution_id": execution_id,
        }


@activity.defn
async def dispatch_actions(
    check_id: str,
    execution_id: str,
    findings: list[dict],
    dry_run: bool = False,
) -> dict:
    """Dispatch notifications for Check findings via the ActionDispatcher."""
    _setup_django_models()
    from apps.checks.models import Check, CheckExecution
    from services.checks.actions import ActionDispatcher

    check = await sync_to_async(Check.objects.get)(id=check_id)

    execution = None
    if execution_id:
        try:
            execution = await sync_to_async(CheckExecution.objects.get)(id=execution_id)
        except CheckExecution.DoesNotExist:
            pass

    if dry_run:
        return {"dispatched": [], "dry_run": True}

    logs = await sync_to_async(ActionDispatcher.dispatch)(check, execution, findings)

    actions_dispatched = [
        {
            "action_id": str(log.id),
            "action_type": log.action_type,
            "status": log.delivery_status,
        }
        for log in logs
    ]

    return {"dispatched": actions_dispatched, "dry_run": False}
