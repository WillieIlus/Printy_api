from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="PartnerClient",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True, help_text="Timestamp when the record was created.", verbose_name="created at")),
                ("updated_at", models.DateTimeField(auto_now=True, help_text="Timestamp when the record was last updated.", verbose_name="updated at")),
                ("name", models.CharField(max_length=255)),
                ("phone", models.CharField(blank=True, default="", max_length=50)),
                ("email", models.EmailField(blank=True, default="", max_length=254)),
                ("company", models.CharField(blank=True, default="", max_length=255)),
                ("client_user", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="managed_by_partners", to=settings.AUTH_USER_MODEL)),
                ("partner", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="partner_clients", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "ordering": ["name", "id"],
            },
        ),
        migrations.AddConstraint(
            model_name="partnerclient",
            constraint=models.UniqueConstraint(condition=models.Q(("client_user__isnull", False)), fields=("partner", "client_user"), name="api_partnerclient_unique_partner_client_user"),
        ),
        migrations.AddConstraint(
            model_name="partnerclient",
            constraint=models.UniqueConstraint(condition=models.Q(("phone", ""), _negated=True), fields=("partner", "phone"), name="api_partnerclient_unique_partner_phone"),
        ),
    ]
