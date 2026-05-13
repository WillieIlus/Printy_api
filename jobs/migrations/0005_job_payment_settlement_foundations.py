from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("jobs", "0004_jobfile_foundation"),
        ("shops", "0004_shop_mvp_rate_card"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="JobPayment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("amount", models.DecimalField(decimal_places=2, max_digits=12)),
                ("payment_method", models.CharField(choices=[("mpesa", "M-Pesa"), ("card", "Card"), ("cash", "Cash"), ("manual", "Manual")], default="mpesa", max_length=16, verbose_name="payment method")),
                ("payment_status", models.CharField(choices=[("pending", "Pending"), ("manual_payment_pending", "Manual payment pending"), ("confirmed", "Confirmed"), ("failed", "Failed"), ("refunded", "Refunded")], default="pending", max_length=32, verbose_name="payment status")),
                ("external_reference", models.CharField(blank=True, default="", max_length=100)),
                ("confirmed_at", models.DateTimeField(blank=True, null=True)),
                ("raw_gateway_payload", models.JSONField(blank=True, null=True)),
                ("managed_job", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="payments", to="jobs.managedjob", verbose_name="managed job")),
                ("payer", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="job_payments", to=settings.AUTH_USER_MODEL, verbose_name="payer")),
            ],
            options={
                "verbose_name": "job payment",
                "verbose_name_plural": "job payments",
                "ordering": ["-created_at"],
            },
        ),
        migrations.CreateModel(
            name="JobSettlementSplit",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("production_amount", models.DecimalField(decimal_places=2, default=0, max_digits=12)),
                ("platform_fee", models.DecimalField(decimal_places=2, default=0, max_digits=12)),
                ("partner_commission", models.DecimalField(decimal_places=2, default=0, max_digits=12)),
                ("delivery_amount", models.DecimalField(decimal_places=2, default=0, max_digits=12)),
                ("client_total", models.DecimalField(decimal_places=2, default=0, max_digits=12)),
                ("relationship_owner_type", models.CharField(blank=True, default="", max_length=20)),
                ("relationship_owner_reference", models.CharField(blank=True, default="", max_length=50)),
                ("commission_recipient_type", models.CharField(choices=[("printy", "Printy"), ("user", "User"), ("shop", "Shop")], default="printy", max_length=20, verbose_name="commission recipient type")),
                ("status", models.CharField(choices=[("pending", "Pending"), ("held", "Held"), ("release_ready", "Release ready"), ("released", "Released"), ("cancelled", "Cancelled"), ("refunded", "Refunded")], default="pending", max_length=20, verbose_name="status")),
                ("payment_method", models.CharField(choices=[("mpesa", "M-Pesa"), ("card", "Card"), ("cash", "Cash"), ("manual", "Manual")], default="mpesa", max_length=16, verbose_name="payment method")),
                ("release_ready_at", models.DateTimeField(blank=True, null=True)),
                ("released_at", models.DateTimeField(blank=True, null=True)),
                ("managed_job", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="settlement_split", to="jobs.managedjob", verbose_name="managed job")),
                ("relationship_owner_shop", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="job_settlement_splits", to="shops.shop", verbose_name="relationship owner shop")),
                ("relationship_owner_user", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="job_settlement_splits", to=settings.AUTH_USER_MODEL, verbose_name="relationship owner user")),
            ],
            options={
                "verbose_name": "job settlement split",
                "verbose_name_plural": "job settlement splits",
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddIndex(
            model_name="jobpayment",
            index=models.Index(fields=["managed_job", "payment_status"], name="job_payment_status_idx"),
        ),
    ]
