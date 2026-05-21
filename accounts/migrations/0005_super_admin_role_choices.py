from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0004_expand_user_role_choices"),
    ]

    operations = [
        migrations.AlterField(
            model_name="user",
            name="role",
            field=models.CharField(
                choices=[
                    ("super_admin", "Super Admin"),
                    ("admin", "Admin"),
                    ("client", "Client"),
                    ("partner", "Partner"),
                    ("production", "Production"),
                    ("broker", "Broker"),
                    ("shop_owner", "Shop Owner"),
                    ("printer", "Printer"),
                    ("staff", "Staff"),
                ],
                default="client",
                help_text="Primary account role used by the dashboard UI.",
                max_length=20,
            ),
        ),
    ]
