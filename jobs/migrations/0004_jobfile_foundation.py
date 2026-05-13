from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("artwork", "0002_uploadedartwork_preview_and_analysis_fields"),
        ("jobs", "0003_jobassignment_groundwork"),
        ("quotes", "0005_quoterequestmessage_conversation_type_and_more"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="JobFile",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("file", models.FileField(blank=True, null=True, upload_to="managed_jobs/%Y/%m/", verbose_name="file")),
                ("original_filename", models.CharField(blank=True, default="", max_length=255)),
                ("file_type", models.CharField(choices=[("customer_upload", "Customer upload"), ("broker_revision", "Broker revision"), ("proof", "Proof"), ("print_ready", "Print ready"), ("delivery_evidence", "Delivery evidence")], default="customer_upload", max_length=32, verbose_name="file type")),
                ("visibility", models.CharField(choices=[("client", "Client"), ("partner", "Partner"), ("shop", "Shop"), ("ops", "Ops"), ("internal", "Internal")], default="client", max_length=16, verbose_name="visibility")),
                ("status", models.CharField(choices=[("uploaded", "Uploaded"), ("under_review", "Under review"), ("approved", "Approved"), ("rejected", "Rejected"), ("replaced", "Replaced")], default="uploaded", max_length=32, verbose_name="status")),
                ("version", models.PositiveIntegerField(default=1)),
                ("notes", models.TextField(blank=True, default="")),
                ("assignment", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="job_files", to="jobs.jobassignment", verbose_name="assignment")),
                ("managed_job", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="job_files", to="jobs.managedjob", verbose_name="managed job")),
                ("replaces", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="revisions", to="jobs.jobfile", verbose_name="replaces")),
                ("source_quote_request_attachment", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="job_files", to="quotes.quoterequestattachment", verbose_name="source quote request attachment")),
                ("source_shop_quote_attachment", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="job_files", to="quotes.shopquoteattachment", verbose_name="source shop quote attachment")),
                ("source_uploaded_artwork", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="job_files", to="artwork.uploadedartwork", verbose_name="source uploaded artwork")),
                ("uploaded_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="uploaded_job_files", to=settings.AUTH_USER_MODEL, verbose_name="uploaded by")),
            ],
            options={
                "verbose_name": "job file",
                "verbose_name_plural": "job files",
                "ordering": ["created_at", "id"],
            },
        ),
        migrations.AddConstraint(
            model_name="jobfile",
            constraint=models.UniqueConstraint(condition=models.Q(("source_uploaded_artwork__isnull", False)), fields=("managed_job", "source_uploaded_artwork"), name="unique_job_file_source_uploaded_artwork"),
        ),
        migrations.AddConstraint(
            model_name="jobfile",
            constraint=models.UniqueConstraint(condition=models.Q(("source_quote_request_attachment__isnull", False)), fields=("managed_job", "source_quote_request_attachment"), name="unique_job_file_source_quote_attachment"),
        ),
        migrations.AddConstraint(
            model_name="jobfile",
            constraint=models.UniqueConstraint(condition=models.Q(("source_shop_quote_attachment__isnull", False)), fields=("managed_job", "source_shop_quote_attachment"), name="unique_job_file_source_shop_attachment"),
        ),
        migrations.AddIndex(
            model_name="jobfile",
            index=models.Index(fields=["managed_job", "file_type"], name="job_file_type_idx"),
        ),
        migrations.AddIndex(
            model_name="jobfile",
            index=models.Index(fields=["managed_job", "visibility"], name="job_file_visibility_idx"),
        ),
    ]
