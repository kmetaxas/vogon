# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportAssignmentType=false
# ruff: noqa: E402

"""MCP server for exposing Vogon capabilities as tools."""

import asyncio
import logging
import os
import time
from collections.abc import Callable
from functools import wraps
from typing import Any, cast

import django
from django.conf import settings
from django.utils import timezone
from fastmcp import FastMCP

# Setup Django for model access
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "vogon.settings")
django.setup()

from apps.core.models import Organization
from apps.marvins.discovery import (
    CapabilityNotFound,
    DiscoveryEngine,
    FanOutLimitExceeded,
    SelectorRequiresResource,
)
from apps.marvins.labels import LabelSelectorError, parse_label_selector
from apps.marvins.models import Capability, Marvin, ResolvedTargetSet
from apps.marvins.policies import ExecutionPolicy
from apps.marvins.selectors import TargetSelector
from apps.sessions.models import Thread, ToolCall, TSession
from services.mcp_server.auth import MCPAuthError, validate_mcp_token
from services.temporal_workers.client import get_temporal_client
from services.temporal_workers.workflows import CapabilityExecutionWorkflow

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

mcp = FastMCP("vogon")


def log_tool_call(func: Callable) -> Callable:
    """Log MCP tool calls with timing and masked arguments."""

    @wraps(func)
    async def wrapper(*args, **kwargs):
        tool_name = func.__name__
        safe_kwargs = {k: "<masked>" if k in {"api_token"} else v for k, v in kwargs.items()}
        logger.info("[MCP CALL] %s args=%s kwargs=%s", tool_name, args, safe_kwargs)
        start = time.monotonic()
        try:
            result = await func(*args, **kwargs)
            duration_ms = (time.monotonic() - start) * 1000
            if isinstance(result, dict):
                success = result.get("success")
                error = result.get("error")
                summary = f"success={success}"
                if error:
                    summary += f" error={error!r}"
            else:
                summary = f"result_type={type(result).__name__}"
            logger.info("[MCP DONE] %s %s duration=%.1fms", tool_name, summary, duration_ms)
            return result
        except Exception as exc:
            duration_ms = (time.monotonic() - start) * 1000
            logger.error("[MCP FAIL] %s error=%s duration=%.1fms", tool_name, exc, duration_ms)
            raise

    return wrapper


def _selector_from_labels(labels: str | None) -> TargetSelector | None:
    if not labels:
        return None
    return TargetSelector(labels=parse_label_selector(labels))


def _unauthorized_error(error: MCPAuthError) -> dict:
    return {"error": "Unauthorized", "detail": str(error)}


def _marvin_preview(marvin: Marvin) -> dict:
    """Build a Marvin preview dict for the LLM/MCP, keeping metadata and labels separate."""
    labels_dict: dict[str, str | None] = {}
    marvin_labels: list[str] = marvin.labels or []
    for label in marvin_labels:
        if ":" in label:
            k, v = label.split(":", 1)
            labels_dict[k.strip()] = v.strip()
        else:
            labels_dict[label.strip()] = None
    return {
        "id": str(marvin.id),
        "name": marvin.name,
        "status": marvin.status,
        "host_metadata": {
            "client_id": marvin.client_id,
            "hostname": marvin.hostname,
            "provider": marvin.provider,
            "region": marvin.region,
            "availability_zone": marvin.availability_zone,
        },
        "labels": labels_dict,
    }


def _target_set_preview(target_set: ResolvedTargetSet, limit: int) -> dict:
    marvin_ids = [str(marvin_id) for marvin_id in cast(list[Any], target_set.snapshot)]
    marvins = list(
        Marvin.objects.filter(
            organization=target_set.organization,
            id__in=marvin_ids[:limit],
        )
    )
    marvins_by_id = {str(marvin.id): marvin for marvin in marvins}
    matched_count = len(marvin_ids)
    metadata = target_set.snapshot_metadata or {}
    payload = {
        "success": True,
        "target_set_id": str(target_set.id),
        "capability": target_set.capability.name,
        "matched_count": matched_count,
        "selector": target_set.selector,
        "expires_at": target_set.expires_at.isoformat(),
        "aggregates": metadata.get("aggregates", {}),
        "metadata": metadata,
    }
    sample = [
        _marvin_preview(marvins_by_id[marvin_id])
        for marvin_id in marvin_ids[:limit]
        if marvin_id in marvins_by_id
    ]
    if matched_count <= limit:
        payload["targets"] = sample
    else:
        payload["sample"] = sample
    return payload


def _validate_target_set(organization: Organization, target_set_id: str) -> ResolvedTargetSet:
    return ResolvedTargetSet.objects.select_related("capability", "organization").get(
        id=target_set_id,
        organization=organization,
        expires_at__gt=timezone.now(),
    )


@mcp.tool()
@log_tool_call
async def find_tools(
    api_token: str,
    organization_slug: str,
    query: str = None,
    labels: str = None,
    limit: int = 10,
    capability_name: str = None,
    filters: dict = None,
) -> list | dict:
    """Find executable capabilities backed by online Marvins.

    Args:
        organization_slug: The slug of the organization
        query: Natural-language or keyword search over capability
            name, description, and keywords.
        labels: Label selector to filter Marvins.
        limit: Maximum number of results to return
        capability_name: Exact capability name for direct lookup
        filters: Optional structured filters, e.g. {"providers": ["kafka"], "scopes": ["cluster"]}

    Returns:
        List of matching capabilities with online Marvin counts, scores, and search method
    """
    try:
        org = validate_mcp_token(api_token, organization_slug)
        from asgiref.sync import sync_to_async

        return await sync_to_async(DiscoveryEngine().find_capabilities)(
            org,
            query=query,
            selector=_selector_from_labels(labels),
            limit=limit,
            capability_name=capability_name,
            filters=filters,
        )
    except LabelSelectorError as e:
        return {"success": False, "error": str(e), "code": "invalid_label_selector"}
    except MCPAuthError as e:
        return _unauthorized_error(e)
    except Exception as e:
        logger.error(f"Error finding tools: {e}")
        return []


@mcp.tool()
@log_tool_call
async def find_targets(
    api_token: str,
    organization_slug: str,
    capability_name: str,
    selector: dict = None,
    limit: int = 50,
) -> dict:
    try:
        org = validate_mcp_token(api_token, organization_slug)
        capability = Capability.objects.get(organization=org, name=capability_name, enabled=True)
        target_selector = TargetSelector.from_dict(selector) if selector else None
        target_set = DiscoveryEngine().resolve_targets(org, capability, target_selector)
        return _target_set_preview(target_set, limit)
    except MCPAuthError as e:
        return _unauthorized_error(e)
    except Capability.DoesNotExist:
        return {"success": False, "error": "Capability not found or disabled"}
    except SelectorRequiresResource as e:
        return {"success": False, "error": str(e), "code": "selector_requires_resource"}
    except FanOutLimitExceeded as e:
        return {"success": False, "error": str(e), "code": "fanout_limit_exceeded"}
    except CapabilityNotFound as e:
        return {"success": False, "error": str(e), "code": "capability_not_found"}
    except Exception as e:
        logger.error(f"Error resolving targets: {e}")
        return {"success": False, "error": str(e)}


@mcp.tool()
@log_tool_call
async def execute(
    api_token: str,
    organization_slug: str,
    capability_name: str,
    parameters: dict,
    labels: str = None,
    target_set_id: str = None,
    timeout_seconds: int = None,
    session_id: str = None,
    thread_id: str = None,
) -> dict:
    """Execute a capability on a Marvin agent.

    Args:
        organization_slug: The slug of the organization
        capability_name: Name of the capability to execute
        parameters: Parameters for the capability as JSON
        labels: Label selector to filter Marvins.
        timeout_seconds: Timeout for the execution in seconds
        session_id: Optional session ID for context
        thread_id: Optional thread ID for context

    Returns:
        Result of the capability execution
    """
    try:
        org = validate_mcp_token(api_token, organization_slug)
        capability = Capability.objects.get(
            organization=org,
            name=capability_name,
            enabled=True,
        )

        if target_set_id:
            try:
                target_set = _validate_target_set(org, target_set_id)
            except ResolvedTargetSet.DoesNotExist:
                return {
                    "success": False,
                    "error": "Target set not found, outside organization, or expired",
                }
            if target_set.capability_id != capability.id:
                return {"success": False, "error": "Target set capability does not match request"}
        else:
            try:
                target_set = DiscoveryEngine().resolve_targets(
                    org,
                    capability,
                    _selector_from_labels(labels),
                )
            except SelectorRequiresResource as e:
                return {"success": False, "error": str(e), "code": "selector_requires_resource"}
            except FanOutLimitExceeded as e:
                return {"success": False, "error": str(e), "code": "fanout_limit_exceeded"}

        marvin_ids = [str(marvin_id) for marvin_id in cast(list[Any], target_set.snapshot)]
        if not marvin_ids:
            return {"success": False, "error": "No online Marvin found for this capability"}

        primary_marvin = Marvin.objects.filter(organization=org, id=marvin_ids[0]).first()

        # Create or use existing session/thread
        if not session_id:
            session = TSession.objects.create(
                organization=org,
                title=f"MCP Session - {capability_name}",
            )
            session_id = str(session.id)

        if not thread_id:
            thread = Thread.objects.create(
                tsession_id=session_id,
                title=f"MCP Thread - {capability_name}",
            )
            thread_id = str(thread.id)

        thread = Thread.objects.get(id=thread_id, tsession_id=session_id)
        policy = ExecutionPolicy.from_capability(capability)
        tool_call = ToolCall.objects.create(
            thread=thread,
            capability=capability,
            parameters=parameters or {},
            labels=target_set.selector,
            marvin=primary_marvin,
            timeout_seconds=timeout_seconds,
            target_set=target_set,
            execution_mode="fanout" if len(marvin_ids) > 1 else "single",
            policy_snapshot=policy.model_dump(),
            status=ToolCall.Status.IN_PROGRESS,
        )

        # Start the capability execution workflow via Temporal
        temporal_client = await get_temporal_client()

        try:
            result = await asyncio.wait_for(
                temporal_client.execute_workflow(
                    CapabilityExecutionWorkflow.run,
                    args=[
                        session_id,
                        thread_id,
                        capability_name,
                        parameters,
                        marvin_ids,
                    ],
                    id=f"capability-{capability_name}-{tool_call.id}",
                    task_queue=os.environ.get("TEMPORAL_TASK_QUEUE", "vogon"),
                ),
                timeout=timeout_seconds or int(os.environ.get("MCP_TOOL_TIMEOUT_SECONDS", "60")),
            )
        except TimeoutError:
            tool_call.status = ToolCall.Status.FAILED
            tool_call.result = {"error": "Tool execution timed out"}
            tool_call.save(update_fields=["status", "result"])
            return {
                "success": False,
                "error": "Tool execution timed out",
                "execution_id": str(tool_call.id),
            }

        tool_call.result = result
        tool_call.status = ToolCall.Status.COMPLETED
        tool_call.completed_at = timezone.now()
        tool_call.save(update_fields=["result", "status", "completed_at"])

        return {
            "success": True,
            "execution_id": str(tool_call.id),
            "status": tool_call.status,
            "result": result,
            "session_id": session_id,
            "thread_id": thread_id,
            "target_set_id": str(target_set.id),
            "target_count": len(marvin_ids),
        }

    except LabelSelectorError as e:
        return {"success": False, "error": str(e), "code": "invalid_label_selector"}
    except MCPAuthError as e:
        return _unauthorized_error(e)
    except Capability.DoesNotExist:
        return {"success": False, "error": "Capability not found or disabled"}
    except Exception as e:
        logger.error(f"Error executing capability: {e}")
        return {"success": False, "error": str(e)}


@mcp.tool()
@log_tool_call
async def execute_tool(
    api_token: str,
    organization_slug: str,
    capability_name: str,
    parameters: dict,
    labels: str = None,
    target_set_id: str = None,
    timeout_seconds: int = None,
    session_id: str = None,
    thread_id: str = None,
) -> dict:
    try:
        validate_mcp_token(api_token, organization_slug)
    except MCPAuthError as e:
        return _unauthorized_error(e)

    return await execute(
        api_token=api_token,
        organization_slug=organization_slug,
        capability_name=capability_name,
        parameters=parameters,
        labels=labels,
        target_set_id=target_set_id,
        timeout_seconds=timeout_seconds,
        session_id=session_id,
        thread_id=thread_id,
    )


@mcp.tool()
@log_tool_call
async def get_execution(api_token: str, execution_id: str, organization_slug: str) -> dict:
    try:
        org = validate_mcp_token(api_token, organization_slug)
        tool_call = ToolCall.objects.select_related(
            "thread__tsession__organization",
            "capability",
            "target_set",
        ).get(id=execution_id, thread__tsession__organization=org)
        result = tool_call.result or {}
        errors = result.get("errors", []) if isinstance(result, dict) else []
        summary = result.get("summary", {}) if isinstance(result, dict) else {}
        return {
            "success": True,
            "execution_id": str(tool_call.id),
            "status": tool_call.status,
            "capability": tool_call.capability.name if tool_call.capability else None,
            "session_id": str(tool_call.thread.tsession_id),
            "thread_id": str(tool_call.thread_id),
            "target_set_id": str(tool_call.target_set_id) if tool_call.target_set_id else None,
            "execution_mode": tool_call.execution_mode,
            "results": result,
            "errors": errors,
            "summary": summary,
            "created_at": tool_call.created_at.isoformat(),
            "completed_at": tool_call.completed_at.isoformat() if tool_call.completed_at else None,
        }
    except MCPAuthError as e:
        return _unauthorized_error(e)
    except ToolCall.DoesNotExist:
        return {"success": False, "error": "Execution not found for organization"}
    except Exception as e:
        logger.error(f"Error getting execution: {e}")
        return {"success": False, "error": str(e)}


@mcp.tool()
@log_tool_call
async def get_session_status(api_token: str, organization_slug: str, session_id: str) -> dict:
    """Get the status of a troubleshooting session.

    Args:
        session_id: The ID of the session

    Returns:
        Session status and details
    """
    try:
        org = validate_mcp_token(api_token, organization_slug)
        session = TSession.objects.get(id=session_id, organization=org)
        threads = session.threads.all()

        return {
            "id": str(session.id),
            "title": session.title,
            "status": session.status,
            "created_at": session.created_at.isoformat(),
            "thread_count": threads.count(),
            "threads": [
                {
                    "id": str(t.id),
                    "status": t.status,
                    "visibility": t.visibility,
                }
                for t in threads
            ],
        }
    except MCPAuthError as e:
        return _unauthorized_error(e)
    except TSession.DoesNotExist:
        return {"error": "Session not found"}


@mcp.tool()
@log_tool_call
async def list_sessions(api_token: str, organization_slug: str, status: str = None) -> list | dict:
    """List troubleshooting sessions for an organization.

    Args:
        organization_slug: The slug of the organization
        status: Optional filter by session status

    Returns:
        List of sessions
    """
    try:
        org = validate_mcp_token(api_token, organization_slug)
        sessions = TSession.objects.filter(organization=org)

        if status:
            sessions = sessions.filter(status=status)

        return [
            {
                "id": str(s.id),
                "title": s.title,
                "status": s.status,
                "created_at": s.created_at.isoformat(),
            }
            for s in sessions
        ]
    except MCPAuthError as e:
        return _unauthorized_error(e)


@mcp.tool()
@log_tool_call
async def request_architecture_design(
    api_token: str,
    organization_slug: str,
    session_id: str,
    thread_id: str,
    description: str,
) -> dict:
    """Request a human to upload an architecture diagram or markdown.

    Args:
        session_id: The ID of the session
        thread_id: The ID of the thread
        description: What design info is needed

    Returns:
        Request confirmation with URL
    """
    try:
        org = validate_mcp_token(api_token, organization_slug)
        thread = Thread.objects.get(
            id=thread_id,
            tsession_id=session_id,
            tsession__organization=org,
        )
        from services.standard_tools.architecture import (
            request_architecture_design as _request_arch,
        )

        return _request_arch(thread, description)
    except MCPAuthError as e:
        return _unauthorized_error(e)
    except Thread.DoesNotExist:
        return {"error": "Thread not found"}


if settings.LLM_PROMETHEUS_TOOLS_ENABLED:

    @mcp.tool()
    @log_tool_call
    async def get_prometheus_alerts(api_token: str, organization_slug: str) -> dict:
        """Fetch currently firing Prometheus alerts."""
        try:
            validate_mcp_token(api_token, organization_slug)
        except MCPAuthError as e:
            return _unauthorized_error(e)

        from services.standard_tools.prometheus import get_prometheus_alerts as _get_alerts

        return await _get_alerts()

    @mcp.tool()
    @log_tool_call
    async def query_prometheus(api_token: str, organization_slug: str, query: str) -> dict:
        """Run a PromQL query against Prometheus.

        Args:
            query: PromQL expression

        Returns:
            Query results
        """
        try:
            validate_mcp_token(api_token, organization_slug)
        except MCPAuthError as e:
            return _unauthorized_error(e)

        from services.standard_tools.prometheus import query_prometheus as _query

        return await _query(query)


@mcp.tool()
@log_tool_call
async def search_infrastructure_designs(
    api_token: str,
    organization_slug: str,
    query: str = None,
    environment: str = None,
    limit: int = 5,
) -> dict:
    """Search infrastructure designs by description or topology intent.

    Args:
        organization_slug: The slug of the organization
        query: Natural-language description of the infrastructure or problem.
        environment: Optional filter by environment (production, staging, development, testing)
        limit: Maximum number of results to return (default 5, max 20)

    Returns:
        List of matching infrastructure designs with titles, environments,
        Marvin selectors, and match scores.
    """
    try:
        org = validate_mcp_token(api_token, organization_slug)
        limit = max(1, min(limit, 20))

        from asgiref.sync import sync_to_async
        from django.db.models import Q

        from apps.infradesigns.models import InfrastructureDesign
        from services.embeddings.registry import EmbeddingProviderFactory

        @sync_to_async
        def _search():
            qs = InfrastructureDesign.objects.filter(organization=org)
            if environment:
                qs = qs.filter(environment=environment)

            results = []
            vector_results = []
            lexical_results = []

            if query and settings.EMBEDDING_SEARCH_ENABLED:
                provider = EmbeddingProviderFactory.get_default_provider()
                try:
                    embedding_result = provider.embed(query)
                    vector = embedding_result.embedding
                    if vector:
                        vector_str = "[" + ",".join(str(v) for v in vector) + "]"
                        from django.db.models import FloatField
                        from django.db.models.expressions import RawSQL

                        vector_qs = (
                            qs.filter(
                                embedding__isnull=False,
                                needs_re_embedding=False,
                            )
                            .annotate(
                                distance=RawSQL(
                                    "embedding <=> %s::vector",
                                    [vector_str],
                                    output_field=FloatField(),
                                )
                            )
                            .order_by("distance")[: limit * 2]
                        )

                        for design in vector_qs:
                            vector_results.append(
                                {
                                    "id": str(design.id),
                                    "name": design.name,
                                    "environment": design.environment,
                                    "description": design.description,
                                    "marvin_selector": design.marvin_selector,
                                    "distance": design.distance,
                                    "search_method": "vector",
                                }
                            )
                except Exception as e:
                    logger.warning(
                        "Vector search failed for designs, falling back to lexical: %s", e
                    )

            if query:
                lexical_qs = qs.filter(
                    Q(name__icontains=query)
                    | Q(description__icontains=query)
                    | Q(search_document__icontains=query)
                )[: limit * 2]

                for design in lexical_qs:
                    lexical_results.append(
                        {
                            "id": str(design.id),
                            "name": design.name,
                            "environment": design.environment,
                            "description": design.description,
                            "marvin_selector": design.marvin_selector,
                            "search_method": "lexical",
                        }
                    )

            seen_ids = set()
            for r in vector_results + lexical_results:
                if r["id"] not in seen_ids:
                    seen_ids.add(r["id"])
                    if "distance" in r:
                        r["score"] = max(0.0, 1.0 - r["distance"])
                    else:
                        r["score"] = 0.5
                    results.append(r)

            if not query:
                for design in qs.order_by("-updated_at")[:limit]:
                    results.append(
                        {
                            "id": str(design.id),
                            "name": design.name,
                            "environment": design.environment,
                            "description": design.description,
                            "marvin_selector": design.marvin_selector,
                            "search_method": "recent",
                            "score": 0.0,
                        }
                    )

            return results[:limit]

        results = await _search()

        return {
            "success": True,
            "count": len(results),
            "results": results,
        }

    except MCPAuthError as e:
        return _unauthorized_error(e)
    except Exception as e:
        logger.error("Error searching infrastructure designs: %s", e)
        return {"success": False, "error": str(e)}


@mcp.tool()
@log_tool_call
async def get_infrastructure_design(
    api_token: str,
    organization_slug: str,
    design_id: str,
) -> dict:
    """Retrieve a single infrastructure design by ID.

    Args:
        organization_slug: The slug of the organization
        design_id: UUID of the infrastructure design

    Returns:
        Full design including name, description, environment, mermaid topology,
        and Marvin selector.
    """
    try:
        org = validate_mcp_token(api_token, organization_slug)
        from apps.infradesigns.models import InfrastructureDesign

        design = InfrastructureDesign.objects.get(
            id=design_id,
            organization=org,
        )

        return {
            "success": True,
            "design": {
                "id": str(design.id),
                "name": design.name,
                "environment": design.environment,
                "description": design.description,
                "mermaid_topology": design.mermaid_topology,
                "marvin_selector": design.marvin_selector,
                "created_at": design.created_at.isoformat(),
                "updated_at": design.updated_at.isoformat(),
            },
        }
    except MCPAuthError as e:
        return _unauthorized_error(e)
    except Exception as e:
        if "does not exist" in str(e).lower():
            return {"success": False, "error": "Design not found"}
        logger.error("Error getting infrastructure design: %s", e)
        return {"success": False, "error": str(e)}


if __name__ == "__main__":
    mcp.run()
