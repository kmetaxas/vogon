from django.db import migrations, models


def backfill_model_name(apps, schema_editor):
    Message = apps.get_model("troubleshooting_sessions", "Message")
    LLMProvider = apps.get_model("llm", "LLMProvider")

    messages = Message.objects.filter(
        models.Q(input_tokens__gt=0) | models.Q(output_tokens__gt=0)
    ).select_related("thread__tsession__llm_provider")

    for message in messages.iterator():
        tsession = message.thread.tsession
        provider = tsession.llm_provider
        if provider is None:
            provider = (
                LLMProvider.objects.filter(
                    organization_id=tsession.organization_id,
                    enabled=True,
                )
                .order_by("-is_default")
                .first()
            )
        if provider and provider.model:
            message.model_name = provider.model
            message.save(update_fields=["model_name"])


class Migration(migrations.Migration):
    dependencies = [
        ("troubleshooting_sessions", "0012_tsession_usage_counters"),
        ("llm", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="message",
            name="model_name",
            field=models.CharField(blank=True, max_length=100),
        ),
        migrations.RunPython(backfill_model_name, migrations.RunPython.noop),
    ]
