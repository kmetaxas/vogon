from django.db import migrations


def delete_non_autonomous_checks(apps, schema_editor):
    """Delete every Check that is not in autonomous execution mode.

    Deterministic and AI-assisted check flows are being removed, so their
    persisted rows must be purged before the ``execution_mode`` field is
    dropped from the schema.
    """
    Check = apps.get_model("checks", "Check")
    Check.objects.exclude(execution_mode="autonomous").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("checks", "0005_migrate_execution_budget"),
    ]

    operations = [
        migrations.RunPython(
            delete_non_autonomous_checks,
            migrations.RunPython.noop,
        ),
    ]
