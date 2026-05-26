from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0007_userprofile_default_markup_rate"),
    ]

    operations = [
        migrations.AddField(
            model_name="userprofile",
            name="is_system_account",
            field=models.BooleanField(default=False),
        ),
    ]
