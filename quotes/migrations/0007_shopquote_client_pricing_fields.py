from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import migrations, models


def _decimal(value):
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def backfill_shopquote_client_pricing(apps, schema_editor):
    ShopQuote = apps.get_model("quotes", "ShopQuote")
    for shop_quote in ShopQuote.objects.all():
        snapshot = shop_quote.response_snapshot or {}
        customer_pricing = snapshot.get("customer_pricing") or {}
        changed = False

        mappings = {
            "production_base_price": _decimal(customer_pricing.get("production_base_price")),
            "broker_margin_value": _decimal(customer_pricing.get("broker_margin_value") or customer_pricing.get("broker_margin_percent")),
            "broker_margin_amount": _decimal(customer_pricing.get("broker_margin_amount")),
            "platform_service_percent": _decimal(customer_pricing.get("platform_service_percent")),
            "platform_service_amount": _decimal(customer_pricing.get("platform_service_amount")),
            "client_total": _decimal(customer_pricing.get("final_client_price")),
        }
        for field, value in mappings.items():
            if value is not None and getattr(shop_quote, field) is None:
                setattr(shop_quote, field, value)
                changed = True

        broker_margin_type = customer_pricing.get("broker_margin_type") or ""
        if broker_margin_type and not shop_quote.broker_margin_type:
            shop_quote.broker_margin_type = broker_margin_type
            changed = True

        if customer_pricing and shop_quote.sent_to_client_at is None and shop_quote.sent_at is not None:
            shop_quote.sent_to_client_at = shop_quote.sent_at
            changed = True

        if customer_pricing and not shop_quote.client_quote_status:
            shop_quote.client_quote_status = "sent"
            changed = True

        if changed:
            shop_quote.save()


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("quotes", "0006_quoterequest_on_behalf_of"),
    ]

    operations = [
        migrations.AddField(
            model_name="shopquote",
            name="broker_margin_amount",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True),
        ),
        migrations.AddField(
            model_name="shopquote",
            name="broker_margin_type",
            field=models.CharField(blank=True, default="", max_length=20),
        ),
        migrations.AddField(
            model_name="shopquote",
            name="broker_margin_value",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True),
        ),
        migrations.AddField(
            model_name="shopquote",
            name="client_quote_status",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
        migrations.AddField(
            model_name="shopquote",
            name="client_total",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True),
        ),
        migrations.AddField(
            model_name="shopquote",
            name="platform_service_amount",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True),
        ),
        migrations.AddField(
            model_name="shopquote",
            name="platform_service_percent",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=6, null=True),
        ),
        migrations.AddField(
            model_name="shopquote",
            name="production_base_price",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True),
        ),
        migrations.AddField(
            model_name="shopquote",
            name="sent_to_client_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="shopquote",
            name="sent_to_client_by",
            field=models.ForeignKey(blank=True, null=True, on_delete=models.deletion.SET_NULL, related_name="client_facing_shop_quotes", to=settings.AUTH_USER_MODEL),
        ),
        migrations.RunPython(backfill_shopquote_client_pricing, migrations.RunPython.noop),
    ]
