from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("jobs", "0001_initial"),
        ("production", "0003_customer_relationship_foundations"),
        ("quotes", "0005_quoterequestmessage_conversation_type_and_more"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("shops", "0004_shop_mvp_rate_card"),
    ]

    operations = [
        migrations.CreateModel(
            name="ManagedJob",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("managed_reference", models.CharField(blank=True, default="", help_text="Stable reference for the managed operational job.", max_length=50, unique=True, verbose_name="managed reference")),
                ("title", models.CharField(blank=True, default="", help_text="Operational label for the managed job.", max_length=255, verbose_name="title")),
                ("status", models.CharField(choices=[("draft", "Draft"), ("quoted", "Quoted"), ("awaiting_payment", "Awaiting payment"), ("payment_confirmed", "Payment confirmed"), ("assigned", "Assigned"), ("in_production", "In production"), ("finishing", "Finishing"), ("ready", "Ready"), ("delivered", "Delivered"), ("completed", "Completed"), ("disputed", "Disputed"), ("cancelled", "Cancelled")], default="draft", max_length=32, verbose_name="status")),
                ("payment_status", models.CharField(choices=[("pending", "Pending"), ("confirmation_pending", "Confirmation pending"), ("confirmed", "Confirmed"), ("release_ready", "Release ready"), ("payout_on_hold", "Payout on hold"), ("released", "Released"), ("refunded", "Refunded")], default="pending", max_length=32, verbose_name="payment status")),
                ("assignment_status", models.CharField(choices=[("unassigned", "Unassigned"), ("assignment_pending", "Assignment pending"), ("assigned", "Assigned"), ("reassignment_required", "Reassignment required"), ("overflow_review", "Overflow review")], default="unassigned", max_length=32, verbose_name="assignment status")),
                ("exception_status", models.CharField(choices=[("clear", "Clear"), ("production_issue", "Production issue"), ("delivery_issue", "Delivery issue"), ("dispute_open", "Dispute open"), ("ops_review", "Ops review")], default="clear", max_length=32, verbose_name="exception status")),
                ("fulfillment_mode", models.CharField(choices=[("printy_rider", "Printy rider"), ("own_rider", "Own rider"), ("pickup", "Pickup")], default="pickup", max_length=32, verbose_name="fulfillment mode")),
                ("topology_type", models.CharField(choices=[("client_partner", "Client to partner"), ("client_printy_support", "Client to Printy support"), ("partner_shop", "Partner to shop"), ("shop_ops", "Shop to ops"), ("ops_internal", "Ops internal")], default="client_printy_support", max_length=32, verbose_name="topology type")),
                ("client_total", models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True)),
                ("production_total", models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True)),
                ("platform_fee", models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True)),
                ("broker_commission", models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True)),
                ("payout_hold", models.BooleanField(default=False)),
                ("dispute_open", models.BooleanField(default=False)),
                ("production_issue_flag", models.BooleanField(default=False)),
                ("delivery_issue_flag", models.BooleanField(default=False)),
                ("ops_review_required", models.BooleanField(default=False)),
                ("commercial_snapshot", models.JSONField(blank=True, default=dict)),
                ("operational_snapshot", models.JSONField(blank=True, default=dict)),
                ("workflow_metadata", models.JSONField(blank=True, default=dict)),
                ("relationship_snapshot", models.JSONField(blank=True, default=dict)),
                ("accepted_at", models.DateTimeField(blank=True, null=True)),
                ("payment_confirmed_at", models.DateTimeField(blank=True, null=True)),
                ("assigned_at", models.DateTimeField(blank=True, null=True)),
                ("production_started_at", models.DateTimeField(blank=True, null=True)),
                ("ready_at", models.DateTimeField(blank=True, null=True)),
                ("delivered_at", models.DateTimeField(blank=True, null=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("disputed_at", models.DateTimeField(blank=True, null=True)),
                ("cancelled_at", models.DateTimeField(blank=True, null=True)),
                ("assigned_shop", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="managed_jobs", to="shops.shop", verbose_name="assigned shop")),
                ("broker", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="broker_managed_jobs", to=settings.AUTH_USER_MODEL, verbose_name="broker")),
                ("client", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="managed_jobs", to=settings.AUTH_USER_MODEL, verbose_name="client")),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="managed_jobs_created", to=settings.AUTH_USER_MODEL, verbose_name="created by")),
                ("customer", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="managed_jobs", to="production.customer", verbose_name="customer")),
                ("source_job_request", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="managed_derivatives", to="jobs.jobrequest", verbose_name="source overflow job request")),
                ("source_production_order", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="managed_jobs", to="production.productionorder", verbose_name="source production order")),
                ("source_quote_request", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="managed_jobs", to="quotes.quoterequest", verbose_name="source quote request")),
                ("source_shop_quote", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="managed_jobs", to="quotes.shopquote", verbose_name="source shop quote")),
            ],
            options={
                "verbose_name": "managed job",
                "verbose_name_plural": "managed jobs",
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddIndex(
            model_name="managedjob",
            index=models.Index(fields=["status", "payment_status"], name="managed_job_status_payment_idx"),
        ),
        migrations.AddIndex(
            model_name="managedjob",
            index=models.Index(fields=["assigned_shop", "assignment_status"], name="managed_job_assignment_idx"),
        ),
    ]
