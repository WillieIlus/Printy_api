from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("quotes", "0001_initial"),
    ]

    operations = [
        migrations.AlterField(
            model_name="quoterequest",
            name="status",
            field=models.CharField(
                choices=[
                    ("draft", "Draft"),
                    ("submitted", "Submitted"),
                    ("awaiting_shop_action", "Awaiting shop action"),
                    ("accepted", "Accepted by shop"),
                    ("awaiting_client_reply", "Awaiting client reply"),
                    ("viewed", "Viewed"),
                    ("quoted", "Quoted"),
                    ("rejected", "Rejected"),
                    ("expired", "Expired"),
                    ("closed", "Closed"),
                    ("cancelled", "Cancelled"),
                ],
                default="draft",
                help_text="Customer request lifecycle: draft â†’ submitted â†’ quoted â†’ accepted/closed.",
                max_length=32,
                verbose_name="status",
            ),
        ),
    ]
