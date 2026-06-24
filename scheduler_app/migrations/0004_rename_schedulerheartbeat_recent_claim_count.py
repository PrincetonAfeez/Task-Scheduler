""" Rename schedulerheartbeat recent_claim_count to recent_occurrences_created. """

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("scheduler_app", "0003_alter_job_alert_mode"),
    ]

    operations = [
        migrations.RenameField(
            model_name="schedulerheartbeat",
            old_name="recent_claim_count",
            new_name="recent_occurrences_created",
        ),
        migrations.AlterField(
            model_name="schedulerheartbeat",
            name="recent_occurrences_created",
            field=models.PositiveIntegerField(
                default=0,
                help_text="Occurrences created on the most recent scheduler tick (not worker claims).",
            ),
        ),
    ]
