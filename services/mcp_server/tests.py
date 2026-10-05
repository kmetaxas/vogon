import asyncio
import os

import pytest

# find_tools() performs synchronous ORM calls (validate_mcp_token) inside an
# async function. Allow that in tests without modifying the source.
os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "true")

from apps.core.models import Organization
from apps.marvins.models import Capability, Marvin
from services.mcp_server.auth import MCPAuthError, validate_mcp_token
from services.mcp_server.server import find_tools


@pytest.mark.django_db(transaction=True)
def test_auth_validate_mcp_token_success():
    org = Organization.objects.create(name="Acme", slug="acme")
    token = org.generate_mcp_token()

    result = validate_mcp_token(token, "acme")

    assert result == org


@pytest.mark.django_db(transaction=True)
def test_auth_validate_mcp_token_wrong_slug():
    org = Organization.objects.create(name="Acme", slug="acme")
    token = org.generate_mcp_token()

    with pytest.raises(MCPAuthError):
        validate_mcp_token(token, "other")


@pytest.mark.django_db(transaction=True)
def test_auth_validate_mcp_token_bad_format():
    Organization.objects.create(name="Acme", slug="acme")

    with pytest.raises(MCPAuthError):
        validate_mcp_token("not-a-vogon-token", "acme")


@pytest.mark.django_db(transaction=True)
def test_auth_validate_mcp_token_missing_hash():
    Organization.objects.create(name="Acme", slug="acme")
    token = "vogon_acme_" + "a" * 32

    with pytest.raises(MCPAuthError):
        validate_mcp_token(token, "acme")


@pytest.mark.django_db(transaction=True)
def test_auth_validate_mcp_token_wrong_value():
    org = Organization.objects.create(name="Acme", slug="acme")
    org.generate_mcp_token()
    wrong_token = "vogon_acme_" + "b" * 32

    with pytest.raises(MCPAuthError):
        validate_mcp_token(wrong_token, "acme")


def _make_org_with_capability(slug: str = "acme", name: str = "Acme"):
    """Create an org with a token, an enabled capability, and an online Marvin."""
    org = Organization.objects.create(name=name, slug=slug)
    token = org.generate_mcp_token()
    capability = Capability.objects.create(
        organization=org,
        name="network.icmp.echo_request",
        description="Ping a host",
        enabled=True,
    )
    marvin = Marvin.objects.create(
        organization=org,
        name="server1.us-east.example.com",
        client_id=f"agent-{slug}",
        status=Marvin.Status.ONLINE,
    )
    marvin.capabilities.add(capability)
    return org, token, capability


@pytest.mark.django_db(transaction=True)
def test_find_tools_happy_path_returns_capabilities():
    org, token, capability = _make_org_with_capability()

    result = asyncio.run(find_tools(api_token=token, organization_slug=org.slug))

    assert isinstance(result, dict)
    assert result["total_found"] == 1
    assert result["results"][0]["name"] == capability.name
    assert result["results"][0]["online_count"] == 1


@pytest.mark.django_db(transaction=True)
def test_find_tools_invalid_labels_returns_error_dict():
    org, token, _ = _make_org_with_capability()

    result = asyncio.run(
        find_tools(
            api_token=token,
            organization_slug=org.slug,
            labels="invalid=selector",
        )
    )

    assert isinstance(result, dict)
    assert result["success"] is False
    assert result["code"] == "invalid_label_selector"


@pytest.mark.django_db(transaction=True)
def test_find_tools_invalid_auth_returns_unauthorized():
    org, _token, _ = _make_org_with_capability()
    wrong_token = f"vogon_{org.slug}_" + "b" * 32

    result = asyncio.run(find_tools(api_token=wrong_token, organization_slug=org.slug))

    assert isinstance(result, dict)
    assert result["error"] == "Unauthorized"
    assert "detail" in result


@pytest.mark.django_db(transaction=True)
def test_find_tools_empty_query_returns_capabilities():
    org, token, capability = _make_org_with_capability()

    result = asyncio.run(find_tools(api_token=token, organization_slug=org.slug, query=None))

    assert isinstance(result, dict)
    assert result["total_found"] == 1
    assert result["results"][0]["name"] == capability.name
