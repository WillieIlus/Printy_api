from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("jobs", "0014_managedjob_artwork_required_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="managedjob",
            name="artwork_reminder_sent",
            field=models.BooleanField(default=False),
        ),
    ]
