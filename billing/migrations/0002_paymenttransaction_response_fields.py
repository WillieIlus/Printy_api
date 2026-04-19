from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="paymenttransaction",
            name="customer_message",
            field=models.CharField(blank=True, default="", max_length=255, verbose_name="customer message"),
        ),
        migrations.AddField(
            model_name="paymenttransaction",
            name="raw_response",
            field=models.JSONField(blank=True, null=True, verbose_name="raw STK response"),
        ),
        migrations.AddField(
            model_name="paymenttransaction",
            name="response_code",
            field=models.CharField(blank=True, default="", max_length=10, verbose_name="response code"),
        ),
        migrations.AddField(
            model_name="paymenttransaction",
            name="response_description",
            field=models.CharField(blank=True, default="", max_length=255, verbose_name="response description"),
        ),
    ]
