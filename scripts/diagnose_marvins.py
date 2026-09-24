#!/usr/bin/env python3
"""Diagnose Marvin and Capability state in the database.

Usage:
    python scripts/diagnose_marvins.py [ORG_NAME_OR_ID]

Shows:
  - Organizations and their Marvins
  - Which Marvins are online/offline
  - Capabilities and their online Marvin counts
  - Why find_tools might return empty results
"""

import os
import sys

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "vogon.settings")
import django

django.setup()

from apps.core.models import Organization
from apps.marvins.models import Capability, Marvin


def diagnose_org(org: Organization):
    print(f"\n{'=' * 60}")
    print(f"Organization: {org.name} (id={org.id}, slug={org.slug})")
    print(f"{'=' * 60}")

    # Marvins
    marvins = Marvin.objects.filter(organization=org).order_by("name")
    print(f"\n📊 Marvins: {marvins.count()}")
    if marvins.count() == 0:
        print("   ⚠️  No Marvins registered for this org!")
    else:
        for m in marvins:
            status_icon = (
                "🟢"
                if m.status == Marvin.Status.ONLINE
                else "🔴"
                if m.status == Marvin.Status.OFFLINE
                else "🟡"
            )
            caps = list(m.capabilities.values_list("name", flat=True))
            print(f"   {status_icon} {m.name}")
            print(f"      client_id: {m.client_id}")
            print(f"      status: {m.status}")
            print(f"      labels: {m.labels}")
            print(f"      capabilities: {caps}")
            print(f"      last_seen: {m.last_seen}")
            print(f"      hostname: {m.hostname}")
            print(f"      agent_version: {m.agent_version}")

    # Capabilities
    capabilities = Capability.objects.filter(organization=org).order_by("name")
    print(f"\n📊 Capabilities: {capabilities.count()}")
    if capabilities.count() == 0:
        print("   ⚠️  No capabilities registered for this org!")
    else:
        for cap in capabilities:
            enabled_icon = "✅" if cap.enabled else "❌"
            online_count = cap.marvins.filter(status=Marvin.Status.ONLINE).count()
            total_count = cap.marvins.count()
            print(f"   {enabled_icon} {cap.name}")
            print(f"      enabled: {cap.enabled}")
            print(f"      online_marvins: {online_count} / {total_count}")
            print(f"      description: {cap.description}")
            print(f"      keywords: {cap.keywords}")

    # Analysis
    print(f"\n🔍 Analysis:")

    online_marvins = marvins.filter(status=Marvin.Status.ONLINE)
    if online_marvins.count() == 0:
        print("   ❌ No Marvins are ONLINE. find_tools will always return empty.")
        print("      Fix: Ensure a Marvin agent is connected via gRPC.")
        return

    enabled_caps = capabilities.filter(enabled=True)
    if enabled_caps.count() == 0:
        print("   ❌ No capabilities are ENABLED. find_tools will always return empty.")
        print("      Fix: Register capabilities through a Marvin agent.")
        return

    # Check capabilities with online marvins
    caps_with_online = enabled_caps.filter(marvins__status=Marvin.Status.ONLINE).distinct()

    if caps_with_online.count() == 0:
        print("   ❌ Capabilities exist but NONE are linked to ONLINE Marvins.")
        print("      This usually means:")
        print("      1. Marvin registered but then disconnected (status=offline)")
        print("      2. Capability was created manually but not via Marvin registration")
        print("      3. Marvin registration succeeded but capability sync failed")
        return

    print(
        f"   ✅ {caps_with_online.count()} capability(s) available via {online_marvins.count()} online Marvin(s)"
    )
    print("   ✅ find_tools should return results for this org")

    for cap in caps_with_online:
        online_marvin_names = list(
            cap.marvins.filter(status=Marvin.Status.ONLINE).values_list("name", flat=True)
        )
        print(f"      - '{cap.name}' via: {', '.join(online_marvin_names)}")


def main():
    if len(sys.argv) < 2:
        print("Diagnosing all organizations...\n")
        orgs = Organization.objects.all().order_by("name")
        if orgs.count() == 0:
            print("❌ No organizations found in the database.")
            print("   Create an org first: python manage.py shell")
            print("   >>> from apps.core.models import Organization")
            print("   >>> Organization.objects.create(name='My Org', slug='my-org')")
            sys.exit(1)
        for org in orgs:
            diagnose_org(org)
    else:
        org_arg = sys.argv[1]
        try:
            org = Organization.objects.get(id=org_arg)
        except Organization.DoesNotExist:
            try:
                org = Organization.objects.get(name=org_arg)
            except Organization.DoesNotExist:
                print(f"Organization not found: '{org_arg}'")
                print("\nAvailable organizations:")
                for o in Organization.objects.all().order_by("name"):
                    print(f"  - id={o.id}, name='{o.name}', slug={o.slug}")
                sys.exit(1)
        diagnose_org(org)

    print(f"\n{'=' * 60}")
    print("Done.")


if __name__ == "__main__":
    main()
