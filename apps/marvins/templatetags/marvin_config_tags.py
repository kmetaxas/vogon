import json

from django import template

from apps.marvins.models import Capability

register = template.Library()


def _default_for_type(field_type: str):
    if field_type == "boolean":
        return False
    if field_type == "integer":
        return 0
    if field_type == "number":
        return 0.0
    return ""


def _ensure_dict(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return {}
    return {}


@register.simple_tag
def get_capability_form(capability: Capability, capability_overrides: dict | None) -> dict:
    """
    Return a dict with fields and metadata for rendering a capability override form.
    Usage: {% get_capability_form cap marvin.online_config.capability_overrides as cap_form %}
    """
    capability_overrides = _ensure_dict(capability_overrides)
    cap_override = capability_overrides.get(capability.name, {})
    schema = _ensure_dict(capability.json_schema)
    properties = schema.get("properties", {})

    fields = []
    for field_name, prop in properties.items():
        field_type = prop.get("type", "string")
        title = prop.get("title", field_name)
        description = prop.get("description", "")
        default = prop.get("default")
        if default is None:
            default = _default_for_type(field_type)

        if field_name in cap_override:
            current_value = cap_override[field_name]
        else:
            current_value = default

        field_info = {
            "name": field_name,
            "type": field_type,
            "title": title,
            "description": description,
            "default": default,
            "current_value": current_value,
        }

        if "enum" in prop:
            field_info["type"] = "enum"
            field_info["choices"] = prop["enum"]

        fields.append(field_info)

    return {
        "fields": fields,
        "has_override": bool(cap_override),
    }


@register.simple_tag
def get_overridden_capabilities(capabilities, capability_overrides: dict | None) -> list[str]:
    """
    Return a list of capability names that have overrides set.
    Usage:
    {% get_overridden_capabilities capabilities overrides as overridden_caps %}
    """
    capability_overrides = _ensure_dict(capability_overrides)
    overridden = []
    for cap in capabilities:
        if capability_overrides.get(cap.name):
            overridden.append(cap.name)
    return overridden
