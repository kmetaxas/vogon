#!/usr/bin/env python3
"""Test find_tools manually without triggering an agent run.

Usage:
    python scripts/test_find_tools.py [ORG_NAME_OR_ID]

If no org is provided, lists all organizations and exits.
"""

import os
import sys

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "vogon.settings")
import django

django.setup()

from apps.core.models import Organization
from apps.marvins.models import Capability, Marvin
from services.temporal_workers.activities import _find_tools


def list_orgs():
    print("Available organizations:")
    for org in Organization.objects.all().order_by("name"):
        print(f"  - id={org.id}, name='{org.name}', slug={org.slug}")


def show_marvin_state(org: Organization):
    print(f"\n--- Marvin state for org '{org.name}' ({org.id}) ---")

    marvins = Marvin.objects.filter(organization=org)
    print(f"Total Marvins: {marvins.count()}")
    for m in marvins.order_by("name"):
        status_icon = "🟢" if m.status == Marvin.Status.ONLINE else "🔴"
        print(f"  {status_icon} {m.name} ({m.client_id}) status={m.status}")
        print(f"      labels: {m.labels}")
        print(f"      capabilities: {list(m.capabilities.values_list('name', flat=True))}")
        print(f"      last_seen: {m.last_seen}")

    capabilities = Capability.objects.filter(organization=org)
    print(f"\nTotal Capabilities: {capabilities.count()}")
    for cap in capabilities.order_by("name"):
        enabled_icon = "✅" if cap.enabled else "❌"
        online_marvins = cap.marvins.filter(status=Marvin.Status.ONLINE).count()
        print(
            f"  {enabled_icon} {cap.name} (enabled={cap.enabled}, online_marvins={online_marvins})"
        )
        print(f"      description: {cap.description}")
        print(f"      keywords: {cap.keywords}")


def test_find_tools(org: Organization):
    print(f"\n--- Testing _find_tools for org '{org.name}' ({org.id}) ---")

    org_id = str(org.id)

    # Test 1: No filters
    print("\n1. No filters:")
    result = _find_tools(org_id)
    print(f"   Results: {result}")

    # Test 2: With query
    print("\n2. Query='restart':")
    result = _find_tools(org_id, query="restart")
    print(f"   Results: {result}")

    # Test 3: With labels
    print("\n3. Labels='env:production':")
    result = _find_tools(org_id, labels="env:production")
    print(f"   Results: {result}")

    # Test 4: Exact capability name
    print("\n4. capability_name='restart':")
    result = _find_tools(org_id, capability_name="restart")
    print(f"   Results: {result}")

    # Test 5: Limit
    print("\n5. limit=1 (no other filters):")
    result = _find_tools(org_id, limit=1)
    print(f"   Results: {result}")


def main():
    if len(sys.argv) < 2:
        list_orgs()
        print("\nUsage: python scripts/test_find_tools.py <org_name_or_id>")
        sys.exit(0)

    org_arg = sys.argv[1]

    try:
        org = Organization.objects.get(id=org_arg)
    except Organization.DoesNotExist:
        try:
            org = Organization.objects.get(name=org_arg)
        except Organization.DoesNotExist:
            print(f"Organization not found: '{org_arg}'")
            list_orgs()
            sys.exit(1)

    show_marvin_state(org)
    test_find_tools(org)


if __name__ == "__main__":
    main()
