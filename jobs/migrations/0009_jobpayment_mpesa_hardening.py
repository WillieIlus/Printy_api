from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("jobs", "0008_managedjob_urgency_foundations"),
    ]

    operations = [
        migrations.AddField(
            model_name="jobpayment",
            name="account_reference",
            field=models.CharField(blank=True, db_index=True, default="", max_length=100),
        ),
        migrations.AddField(
            model_name="jobpayment",
            name="callback_payload",
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="jobpayment",
            name="checkout_request_id",
            field=models.CharField(blank=True, db_index=True, default="", max_length=100),
        ),
        migrations.AddField(
            model_name="jobpayment",
            name="expected_amount",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True),
        ),
        migrations.AddField(
            model_name="jobpayment",
            name="merchant_request_id",
            field=models.CharField(blank=True, db_index=True, default="", max_length=100),
        ),
        migrations.AddField(
            model_name="jobpayment",
            name="mpesa_receipt_number",
            field=models.CharField(blank=True, db_index=True, default="", max_length=50),
        ),
        migrations.AddField(
            model_name="jobpayment",
            name="payer_phone",
            field=models.CharField(blank=True, default="", max_length=20),
        ),
        migrations.AddField(
            model_name="jobpayment",
            name="payment_channel",
            field=models.CharField(
                choices=[
                    ("stk_push", "STK push"),
                    ("paybill_manual", "Paybill manual"),
                    ("qr", "QR"),
                    ("cash", "Cash"),
                    ("manual", "Manual"),
                ],
                default="stk_push",
                max_length=32,
                verbose_name="payment channel",
            ),
        ),
        migrations.AddField(
            model_name="jobpayment",
            name="query_payload",
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="jobpayment",
            name="received_amount",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True),
        ),
        migrations.AddField(
            model_name="jobpayment",
            name="reconciliation_status",
            field=models.CharField(
                choices=[
                    ("pending", "Pending"),
                    ("callback_received", "Callback received"),
                    ("confirmed", "Confirmed"),
                    ("amount_mismatch", "Amount mismatch"),
                    ("unknown_reference", "Unknown reference"),
                    ("duplicate_callback", "Duplicate callback"),
                    ("duplicate_receipt", "Duplicate receipt"),
                    ("failed", "Failed"),
                    ("manual_review", "Manual review"),
                ],
                default="pending",
                max_length=32,
                verbose_name="reconciliation status",
            ),
        ),
        migrations.AlterField(
            model_name="jobpayment",
            name="payment_status",
            field=models.CharField(
                choices=[
                    ("pending", "Pending"),
                    ("manual_payment_pending", "Manual payment pending"),
                    ("stk_push_sent", "STK push sent"),
                    ("confirmation_pending", "Confirmation pending"),
                    ("confirmed", "Confirmed"),
                    ("failed", "Failed"),
                    ("refunded", "Refunded"),
                ],
                default="pending",
                max_length=32,
                verbose_name="payment status",
            ),
        ),
        migrations.AddIndex(
            model_name="jobpayment",
            index=models.Index(fields=["account_reference"], name="job_payment_account_ref_idx"),
        ),
        migrations.AddIndex(
            model_name="jobpayment",
            index=models.Index(fields=["checkout_request_id"], name="job_payment_checkout_idx"),
        ),
        migrations.AddIndex(
            model_name="jobpayment",
            index=models.Index(fields=["merchant_request_id"], name="job_payment_merchant_idx"),
        ),
        migrations.AddIndex(
            model_name="jobpayment",
            index=models.Index(fields=["mpesa_receipt_number"], name="job_payment_receipt_idx"),
        ),
    ]
