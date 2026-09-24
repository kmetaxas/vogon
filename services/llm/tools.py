from services.llm.base import ToolSpec
from django.conf import settings

_TOOL_SPECS = [
    ToolSpec(
        name="find_tools",
        description=(
            "Discover troubleshooting capabilities (tools) available on Marvin agents. "
            "Supports natural-language and semantic queries (e.g., 'why is this Kafka consumer falling behind?') "
            "as well as keyword searches. Use free text, filter by label selector, or look up an exact name. "
            "Call this BEFORE execute_tool to find the right capability and its parameters. "
            "Only returns capabilities backed by at least one online Marvin."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Natural-language or keyword search over capability name, description, and keywords. "
                        "You may describe the problem in plain English (e.g., 'high CPU on a pod', 'disk full warnings') "
                        "or use exact technical terms (e.g., 'kubernetes.pod.logs')."
                    ),
                },
                "labels": {
                    "type": "string",
                    "description": (
                        "Label selector to filter which Marvin agents can execute this tool. "
                        "Labels are key:value pairs separated by commas. "
                        "Use a colon (:) not equals (=). "
                        "Examples: 'env:development' or 'env:production,team:platform'. "
                        "You can only match actual labels set on Marvins (e.g. env, team, cluster). "
                        "You CANNOT use metadata fields like client_id, hostname, provider, or region as labels. "
                        "Omit to target all online Marvins with this capability."
                    ),
                },
                "capability_name": {
                    "type": "string",
                    "description": "Exact capability name for direct lookup",
                },
                "filters": {
                    "type": "object",
                    "description": (
                        "Structured filters, e.g. {'providers': ['kafka'], 'scopes': ['cluster']}"
                    ),
                },
                "limit": {"type": "integer", "default": 10},
            },
        },
    ),
    ToolSpec(
        name="execute_tool",
        description=(
            "Execute a capability on a Marvin agent. Route to the correct Dave(s) using a "
            "label selector. Use find_tools first to discover the capability name and schema."
        ),
        parameters={
            "type": "object",
            "properties": {
                "capability_name": {"type": "string"},
                "parameters": {"type": "object"},
                "labels": {
                    "type": "string",
                    "description": (
                        "Label selector to filter Marvin agents. "
                        "Labels are key:value pairs separated by commas. "
                        "Use a colon (:) not equals (=). "
                        "Examples: 'env:development' or 'env:production,team:platform'. "
                        "You can only match actual labels set on Marvins (e.g. env, team, cluster). "
                        "You CANNOT use metadata fields like client_id, hostname, provider, or region as labels. "
                        "Omit to search across all online Marvins."
                    ),
                },
                "timeout_seconds": {"type": "integer"},
            },
            "required": ["capability_name", "parameters"],
        },
    ),
    ToolSpec(
        name="request_architecture_design",
        description=(
            "Request a human to upload an architecture diagram or markdown. "
            "Use when the investigation needs system design context."
        ),
        parameters={
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "What design info is needed",
                }
            },
            "required": ["description"],
        },
    ),
]

_PROMETHEUS_TOOL_SPECS = [
    ToolSpec(
        name="get_prometheus_alerts",
        description="Fetch currently firing Prometheus alerts.",
        parameters={"type": "object", "properties": {}},
    ),
    ToolSpec(
        name="query_prometheus",
        description="Run a PromQL query against Prometheus.",
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "PromQL expression",
                }
            },
            "required": ["query"],
        },
    ),
]

STANDARD_TOOLS = _TOOL_SPECS + (
    _PROMETHEUS_TOOL_SPECS if settings.LLM_PROMETHEUS_TOOLS_ENABLED else []
)
