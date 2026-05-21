from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def normalize_role(value: str | None) -> str | None:
    mapping = {
        "super_admin": "super_admin",
        "admin": "super_admin",
        "superuser": "super_admin",
        "staff": "super_admin",
        "client": "client",
        "customer": "client",
        "buyer": "client",
        "partner": "partner",
        "broker": "partner",
        "production": "production",
        "shop_owner": "production",
        "printer": "production",
        "production_shop": "production",
    }
    if not value:
        return None
    return mapping.get(str(value).strip().lower())


def backfill_user_roles(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    UserRole = apps.get_model("accounts", "UserRole")
    Shop = apps.get_model("shops", "Shop")
    ShopMembership = apps.get_model("shops", "ShopMembership")

    owner_ids = set(Shop.objects.values_list("owner_id", flat=True))
    membership_ids = set(
        ShopMembership.objects.filter(is_active=True).values_list("user_id", flat=True)
    )

    role_rows = []
    seen: set[tuple[int, str]] = set()
    for user in User.objects.all().order_by("id"):
        candidate_roles: list[str] = []
        legacy_role = normalize_role(getattr(user, "role", None))
        if legacy_role:
            candidate_roles.append(legacy_role)
        if getattr(user, "partner_profile_enabled", False) and "partner" not in candidate_roles:
            candidate_roles.append("partner")
        if (user.id in owner_ids or user.id in membership_ids) and "production" not in candidate_roles:
            candidate_roles.append("production")
        if (getattr(user, "is_superuser", False) or getattr(user, "is_staff", False)) and "super_admin" not in candidate_roles:
            candidate_roles.append("super_admin")
        if not candidate_roles:
            candidate_roles.append("client")

        for role in candidate_roles:
            key = (user.id, role)
            if key in seen:
                continue
            seen.add(key)
            role_rows.append(
                UserRole(
                    user_id=user.id,
                    role=role,
                    is_active=True,
                    source="migration_backfill",
                )
            )

    if role_rows:
        UserRole.objects.bulk_create(role_rows, ignore_conflicts=True)


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0005_super_admin_role_choices"),
        ("shops", "0004_shop_mvp_rate_card"),
    ]

    operations = [
        migrations.CreateModel(
            name="UserRole",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True, help_text="Timestamp when the record was created.", verbose_name="created at")),
                ("updated_at", models.DateTimeField(auto_now=True, help_text="Timestamp when the record was last updated.", verbose_name="updated at")),
                ("role", models.CharField(choices=[("client", "Client"), ("partner", "Partner"), ("production", "Production"), ("super_admin", "Super Admin")], max_length=20)),
                ("is_active", models.BooleanField(default=True)),
                ("source", models.CharField(blank=True, default="", max_length=64)),
                ("assigned_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="assigned_user_roles", to=settings.AUTH_USER_MODEL)),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="user_roles", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "verbose_name": "User role",
                "verbose_name_plural": "User roles",
                "ordering": ["user_id", "role", "id"],
            },
        ),
        migrations.AddConstraint(
            model_name="userrole",
            constraint=models.UniqueConstraint(fields=("user", "role"), name="accounts_userrole_unique_user_role"),
        ),
        migrations.RunPython(backfill_user_roles, migrations.RunPython.noop),
    ]
