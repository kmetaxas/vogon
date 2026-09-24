# Generated manually

from django.db import migrations


def backfill_provider_and_search_document(apps, schema_editor):
    Capability = apps.get_model("marvins", "Capability")
    for cap in Capability.objects.all().iterator():
        name_str = str(cap.name) if cap.name else ""
        if name_str and not cap.provider:
            cap.provider = name_str.split(".")[0] if "." in name_str else ""
        use_cases = list(getattr(cap, "use_cases", None) or [])
        aliases = list(getattr(cap, "aliases", None) or [])
        tags = list(getattr(cap, "tags", None) or [])
        parts = [
            f"Capability: {cap.name}",
            f"Provider: {cap.provider}",
            f"Description: {cap.description}",
        ]
        if use_cases:
            parts.append("\nUseful for:")
            for uc in use_cases:
                parts.append(f"- {uc}")
        if cap.json_schema and isinstance(cap.json_schema, dict):
            props = cap.json_schema.get("properties", {})
            if props:
                parts.append("\nParameters:")
                for pname, pdef in props.items():
                    desc = pdef.get("description", "")
                    if desc:
                        parts.append(f"- {pname}: {desc}")
        if tags:
            parts.append(f"\nTags: {', '.join(tags)}")
        if aliases:
            parts.append(f"Aliases: {', '.join(aliases)}")
        new_doc = "\n".join(parts)
        if cap.search_document != new_doc:
            cap.search_document = new_doc
            cap.search_document_version += 1
        # Use .update() to avoid triggering custom save() logic that may reference
        # fields not yet present at this point in migration history.
        Capability.objects.filter(id=cap.id).update(
            provider=cap.provider,
            search_document=cap.search_document,
            search_document_version=cap.search_document_version,
        )


def reverse_backfill(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("marvins", "0009_capability_embedding_capability_embedding_model_and_more"),
    ]

    operations = [
        migrations.RunPython(backfill_provider_and_search_document, reverse_backfill),
    ]
