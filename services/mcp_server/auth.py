from apps.core.models import Organization


class MCPAuthError(Exception):
    pass


def validate_mcp_token(api_token: str, organization_slug: str) -> Organization:
    if not api_token:
        raise MCPAuthError("Missing API token")
    if not organization_slug:
        raise MCPAuthError("Missing organization slug")

    token_prefix = f"vogon_{organization_slug}_"
    if not api_token.startswith(token_prefix):
        raise MCPAuthError("Invalid token format")

    random_part = api_token[len(token_prefix) :]
    if len(random_part) != 32:
        raise MCPAuthError("Invalid token format")

    organization = Organization.objects.filter(slug=organization_slug).first()
    if organization is None:
        raise MCPAuthError("Invalid organization")

    if not organization.validate_mcp_token(api_token):
        raise MCPAuthError("Invalid API token")

    return organization
