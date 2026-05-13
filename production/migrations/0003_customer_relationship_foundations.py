from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("shops", "0004_shop_mvp_rate_card"),
        ("production", "0002_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="customer",
            name="acquisition_source",
            field=models.CharField(
                choices=[
                    ("unknown", "Unknown"),
                    ("legacy_quote", "Legacy quote flow"),
                    ("direct", "Direct"),
                    ("partner", "Partner"),
                    ("shop", "Shop"),
                    ("ops", "Ops"),
                ],
                default="unknown",
                help_text="Migration-safe origin label for how this customer entered the system.",
                max_length=20,
                verbose_name="acquisition source",
            ),
        ),
        migrations.AddField(
            model_name="customer",
            name="relationship_owner_shop",
            field=models.ForeignKey(
                blank=True,
                help_text="Shop that owns the relationship when the owner type is shop.",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="owned_customer_relationships",
                to="shops.shop",
                verbose_name="relationship owner shop",
            ),
        ),
        migrations.AddField(
            model_name="customer",
            name="relationship_owner_type",
            field=models.CharField(
                choices=[
                    ("unknown", "Unknown"),
                    ("printy", "Printy"),
                    ("user", "User"),
                    ("shop", "Shop"),
                ],
                default="unknown",
                help_text="Who currently owns the client relationship for attribution and payout routing.",
                max_length=20,
                verbose_name="relationship owner type",
            ),
        ),
        migrations.AddField(
            model_name="customer",
            name="relationship_owner_user",
            field=models.ForeignKey(
                blank=True,
                help_text="User that owns the relationship when the owner type is user.",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="owned_customer_relationships",
                to=settings.AUTH_USER_MODEL,
                verbose_name="relationship owner user",
            ),
        ),
    ]
