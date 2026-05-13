from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("jobs", "0002_managedjob_foundations"),
        ("production", "0003_customer_relationship_foundations"),
        ("quotes", "0005_quoterequestmessage_conversation_type_and_more"),
        ("shops", "0004_shop_mvp_rate_card"),
    ]

    operations = [
        migrations.CreateModel(
            name="JobAssignment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("status", models.CharField(choices=[("pending", "Pending"), ("accepted", "Accepted"), ("rejected", "Rejected"), ("in_production", "In production"), ("ready", "Ready"), ("completed", "Completed"), ("cancelled", "Cancelled"), ("reassigned", "Reassigned")], default="pending", max_length=32, verbose_name="status")),
                ("production_amount", models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True)),
                ("due_at", models.DateTimeField(blank=True, null=True)),
                ("accepted_at", models.DateTimeField(blank=True, null=True)),
                ("rejected_at", models.DateTimeField(blank=True, null=True)),
                ("assignment_notes", models.TextField(blank=True, default="")),
                ("operational_snapshot", models.JSONField(blank=True, default=dict)),
                ("assigned_shop", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="job_assignments", to="shops.shop", verbose_name="assigned shop")),
                ("managed_job", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="assignments", to="jobs.managedjob", verbose_name="managed job")),
                ("production_order", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="job_assignments", to="production.productionorder", verbose_name="production order")),
                ("reassigned_from", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="reassignments", to="jobs.jobassignment", verbose_name="reassigned from")),
                ("source_shop_quote", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="job_assignments", to="quotes.shopquote", verbose_name="source shop quote")),
            ],
            options={
                "verbose_name": "job assignment",
                "verbose_name_plural": "job assignments",
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddConstraint(
            model_name="jobassignment",
            constraint=models.UniqueConstraint(condition=models.Q(("reassigned_from__isnull", True)), fields=("managed_job",), name="unique_active_assignment_per_managed_job"),
        ),
        migrations.AddIndex(
            model_name="jobassignment",
            index=models.Index(fields=["assigned_shop", "status"], name="job_assignment_shop_status_idx"),
        ),
    ]
