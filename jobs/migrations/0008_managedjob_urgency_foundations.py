from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("jobs", "0007_alter_jobassignment_created_at_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="managedjob",
            name="after_hours_fee",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True),
        ),
        migrations.AddField(
            model_name="managedjob",
            name="operational_priority_level",
            field=models.PositiveSmallIntegerField(default=1),
        ),
        migrations.AddField(
            model_name="managedjob",
            name="requested_deadline",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="managedjob",
            name="requested_delivery_time",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="managedjob",
            name="urgency_fee",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True),
        ),
        migrations.AddField(
            model_name="managedjob",
            name="urgency_multiplier",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=6, null=True),
        ),
        migrations.AddField(
            model_name="managedjob",
            name="urgency_type",
            field=models.CharField(
                choices=[
                    ("standard", "Standard"),
                    ("same_day", "Same-day"),
                    ("express", "Express"),
                    ("after_hours", "After-hours"),
                    ("emergency", "Emergency"),
                ],
                default="standard",
                max_length=32,
                verbose_name="urgency type",
            ),
        ),
        migrations.AddField(
            model_name="jobassignment",
            name="operational_priority_level",
            field=models.PositiveSmallIntegerField(default=1),
        ),
        migrations.AddField(
            model_name="jobassignment",
            name="requested_deadline",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="jobassignment",
            name="urgency_type",
            field=models.CharField(
                choices=[
                    ("standard", "Standard"),
                    ("same_day", "Same-day"),
                    ("express", "Express"),
                    ("after_hours", "After-hours"),
                    ("emergency", "Emergency"),
                ],
                default="standard",
                max_length=32,
                verbose_name="urgency type",
            ),
        ),
        migrations.AddIndex(
            model_name="managedjob",
            index=models.Index(fields=["operational_priority_level", "status"], name="managed_job_priority_idx"),
        ),
        migrations.AddIndex(
            model_name="jobassignment",
            index=models.Index(fields=["assigned_shop", "operational_priority_level"], name="job_assignment_priority_idx"),
        ),
    ]

