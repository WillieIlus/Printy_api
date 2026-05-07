from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("shops", "0003_shop_pricing_ranges"),
    ]

    operations = [
        migrations.AddField(
            model_name="shop",
            name="mvp_rate_card",
            field=models.JSONField(
                blank=True,
                default=None,
                help_text=(
                    "Structured MVP onboarding rate card. Stores final inclusive digital press paper rows, "
                    "finishing rows, onboarding progress, and shop setup metadata for the simplified shop-owner flow."
                ),
                null=True,
                verbose_name="mvp rate card",
            ),
        ),
    ]
