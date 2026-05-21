from decimal import Decimal

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0006_userrole_foundations"),
    ]

    operations = [
        migrations.AddField(
            model_name="userprofile",
            name="default_markup_rate",
            field=models.DecimalField(decimal_places=2, default=Decimal("0.30"), max_digits=5),
        ),
    ]
