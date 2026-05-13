from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="capability_overrides",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text=(
                    "Optional additive capability overrides. Keys mirror capability names such as "
                    "can_manage_clients or can_source_jobs."
                ),
            ),
        ),
        migrations.AddField(
            model_name="user",
            name="partner_profile_enabled",
            field=models.BooleanField(
                default=False,
                help_text="Enables future partner/broker capabilities without changing the primary dashboard role yet.",
            ),
        ),
    ]
