from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("jobs", "0005_job_payment_settlement_foundations"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ManagedJobEvent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("event_type", models.CharField(max_length=64, verbose_name="event type")),
                ("summary", models.CharField(blank=True, default="", max_length=255, verbose_name="summary")),
                ("metadata", models.JSONField(blank=True, default=dict, verbose_name="metadata")),
                ("actor", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="managed_job_events", to=settings.AUTH_USER_MODEL, verbose_name="actor")),
                ("assignment", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="events", to="jobs.jobassignment", verbose_name="assignment")),
                ("job_file", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="events", to="jobs.jobfile", verbose_name="job file")),
                ("managed_job", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="events", to="jobs.managedjob", verbose_name="managed job")),
                ("payment", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="events", to="jobs.jobpayment", verbose_name="payment")),
                ("settlement", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="events", to="jobs.jobsettlementsplit", verbose_name="settlement")),
            ],
            options={
                "verbose_name": "managed job event",
                "verbose_name_plural": "managed job events",
                "ordering": ["-created_at", "-id"],
            },
        ),
        migrations.AddIndex(
            model_name="managedjobevent",
            index=models.Index(fields=["managed_job", "event_type"], name="managed_job_event_type_idx"),
        ),
        migrations.AddIndex(
            model_name="managedjobevent",
            index=models.Index(fields=["managed_job", "-created_at"], name="managed_job_event_created_idx"),
        ),
    ]
