""" Add operator_action to jobevent event_type. """

from django.db import migrations, models

class Migration(migrations.Migration):
    dependencies = [
        ("scheduler_app", "0004_rename_schedulerheartbeat_recent_claim_count"),
    ]

    operations = [
        migrations.AlterField(
            model_name="jobevent",
            name="event_type",
            field=models.CharField(
                choices=[
                    ("due_detected", "Due detected"),
                    ("occurrence_created", "Occurrence created"),
                    ("occurrence_exists", "Occurrence already exists"),
                    ("claim", "Claim"),
                    ("dispatch", "Dispatch"),
                    ("worker_start", "Worker start"),
                    ("worker_finish", "Worker finish"),
                    ("failure", "Failure"),
                    ("retry_scheduled", "Retry scheduled"),
                    ("timeout", "Timeout"),
                    ("misfire", "Misfire"),
                    ("lease_recovery", "Lease recovery"),
                    ("stale_claim_rejected", "Stale claim rejected"),
                    ("stale_result_discarded", "Stale result discarded"),
                    ("manual_retry", "Manual retry"),
                    ("dead_letter", "Dead letter"),
                    ("alert", "Alert"),
                    ("cache_invalidation", "Cache invalidation"),
                    ("cancelled", "Cancelled"),
                    ("operator_action", "Operator action"),
                ],
                max_length=32,
            ),
        ),
    ]
