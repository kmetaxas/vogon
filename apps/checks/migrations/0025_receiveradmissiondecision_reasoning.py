from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("checks", "0024_alter_check_schedule_expression_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="receiveradmissiondecision",
            name="reasoning",
            field=models.TextField(
                blank=True,
                help_text="Model reasoning chain from JEV/reasoning models",
            ),
        ),
    ]
