from django import template

from services.notifications.registry import registry

register = template.Library()


@register.simple_tag
def get_channel_form(provider_type: str, config: dict | None, credentials: dict | None) -> dict:
    """Build field metadata for a notification channel form from its provider schema.

    Returns dict with:
        - fields: list of field metadata dicts
        - title: provider display title

    Each field dict has:
        - name: field key (e.g. "webhook_url")
        - type: JSON Schema type ("string", "integer", "boolean", "array", "enum")
        - title: human-readable label
        - description: help text
        - required: bool
        - secret: bool (writeOnly fields)
        - default: default value from schema or empty string/list
        - current_value: actual value from config/credentials
        - choices: list of enum values (only for enum type)
        - items: dict describing array items (only for array type)
    """
    config = config or {}
    credentials = credentials or {}

    try:
        provider_cls = registry.get(provider_type)
        schema = provider_cls.config_schema
    except KeyError:
        schema = {"type": "object", "properties": {}}

    fields = []
    for name, prop in schema.get("properties", {}).items():
        field_type = prop.get("type", "string")
        is_secret = bool(prop.get("writeOnly"))
        source = credentials if is_secret else config
        default = prop.get("default")
        if default is None:
            default = [] if field_type == "array" else ""

        field = {
            "name": name,
            "type": field_type,
            "title": prop.get("title", name.replace("_", " ").title()),
            "description": prop.get("description", ""),
            "required": name in schema.get("required", []),
            "secret": is_secret,
            "default": default,
            "current_value": source.get(name, default),
        }

        if "enum" in prop:
            field["type"] = "enum"
            field["choices"] = prop["enum"]

        if field_type == "array":
            field["items"] = prop.get("items", {})

        fields.append(field)

    return {
        "fields": fields,
        "title": schema.get("title", provider_type.title()),
    }
