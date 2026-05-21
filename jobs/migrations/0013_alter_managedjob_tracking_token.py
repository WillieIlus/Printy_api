import uuid

from django.db import migrations, models
from django.utils.translation import gettext_lazy as _


class Migration(migrations.Migration):

    dependencies = [
        ("jobs", "0012_managedjob_tracking_token"),
    ]

    operations = [
        migrations.AlterField(
            model_name="managedjob",
            name="tracking_token",
            field=models.UUIDField(
                default=uuid.uuid4,
                editable=False,
                help_text=_("Public tracking token for managed job status links."),
                unique=True,
                verbose_name=_("tracking token"),
            ),
        ),
    ]
