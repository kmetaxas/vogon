from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("troubleshooting_sessions", "0014_rename_context_tokens_cumulative_context_tokens"),
    ]

    operations = [
        migrations.AddField(
            model_name="tsession",
            name="is_autonomous",
            field=models.BooleanField(
                default=False,
                help_text="Autonomous sessions are hidden from default session views.",
            ),
        ),
    ]
