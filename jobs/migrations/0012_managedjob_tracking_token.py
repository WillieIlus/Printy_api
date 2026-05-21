import uuid

from django.db import migrations, models


def populate_tracking_tokens(apps, schema_editor):
    ManagedJob = apps.get_model("jobs", "ManagedJob")
    for managed_job in ManagedJob.objects.filter(tracking_token__isnull=True):
        managed_job.tracking_token = uuid.uuid4()
        managed_job.save(update_fields=["tracking_token"])


class Migration(migrations.Migration):

    dependencies = [
        ("jobs", "0011_managedjob_dispatched_at_managedjob_dispatched_by"),
    ]

    operations = [
        migrations.AddField(
            model_name="managedjob",
            name="tracking_token",
            field=models.UUIDField(blank=True, editable=False, null=True, verbose_name="tracking token"),
        ),
        migrations.RunPython(populate_tracking_tokens, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="managedjob",
            name="tracking_token",
            field=models.UUIDField(default=uuid.uuid4, editable=False, unique=True, verbose_name="tracking token"),
        ),
    ]
