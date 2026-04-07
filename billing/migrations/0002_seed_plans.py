"""Data migration: seed the four canonical billing plans."""
from decimal import Decimal

from django.db import migrations


PLANS = [
    {
        "code": "FREE",
        "name": "Free",
        "price_monthly": Decimal("0.00"),
        "price_annual": Decimal("0.00"),
        "shops_limit": 1,
        "machines_limit": 1,
        "products_limit": 3,
        "quotes_per_month_limit": 15,
        "users_limit": 1,
        "all_papers_enabled": True,
        "branded_quotes_enabled": False,
        "customer_history_enabled": False,
        "analytics_level": "basic",
        "priority_support": False,
        "is_active": True,
        "sort_order": 0,
        "best_for": "Trying Printy with one shop",
        "public_tagline": "Get started for free",
        "benefits": [
            "Start with one shop",
            "Set up one machine",
            "Use all papers",
            "Create up to 3 products",
            "Send up to 15 quotes each month",
            "Best for testing Printy",
        ],
        "currency": "KES",
    },
    {
        "code": "BIASHARA",
        "name": "Biashara",
        "price_monthly": Decimal("1500.00"),
        "price_annual": Decimal("15000.00"),
        "shops_limit": 1,
        "machines_limit": 3,
        "products_limit": 15,
        "quotes_per_month_limit": 100,
        "users_limit": 2,
        "all_papers_enabled": True,
        "branded_quotes_enabled": True,
        "customer_history_enabled": True,
        "analytics_level": "standard",
        "priority_support": False,
        "is_active": True,
        "sort_order": 1,
        "best_for": "One active print shop",
        "public_tagline": "Run your print shop professionally",
        "benefits": [
            "Run one real print shop on Printy",
            "Add up to 3 machines",
            "Store up to 15 products",
            "Send up to 100 quotes per month",
            "Use branded customer-facing quotes",
            "Keep customer history",
        ],
        "currency": "KES",
    },
    {
        "code": "BIASHARA_PLUS",
        "name": "Biashara Plus",
        "price_monthly": Decimal("3500.00"),
        "price_annual": Decimal("35000.00"),
        "shops_limit": 2,
        "machines_limit": 10,
        "products_limit": 50,
        "quotes_per_month_limit": 400,
        "users_limit": 5,
        "all_papers_enabled": True,
        "branded_quotes_enabled": True,
        "customer_history_enabled": True,
        "analytics_level": "advanced",
        "priority_support": False,
        "is_active": True,
        "sort_order": 2,
        "best_for": "Growing print business with 2 shops",
        "public_tagline": "Scale across two locations",
        "benefits": [
            "Manage up to 2 shops",
            "Support more machines and products",
            "Handle up to 400 monthly quotes",
            "Give access to a small team",
            "Get advanced analytics",
            "Best for growing print businesses",
        ],
        "currency": "KES",
    },
    {
        "code": "BIASHARA_MAX",
        "name": "Biashara Max",
        "price_monthly": Decimal("6500.00"),
        "price_annual": Decimal("65000.00"),
        "shops_limit": 5,
        "machines_limit": None,
        "products_limit": None,
        "quotes_per_month_limit": None,
        "users_limit": 15,
        "all_papers_enabled": True,
        "branded_quotes_enabled": True,
        "customer_history_enabled": True,
        "analytics_level": "advanced",
        "priority_support": True,
        "is_active": True,
        "sort_order": 3,
        "best_for": "Larger print business or multi-branch operations",
        "public_tagline": "Full power for multi-branch print ops",
        "benefits": [
            "Manage up to 5 shops",
            "Support large teams",
            "Remove most growth bottlenecks",
            "Prioritize support",
            "Use advanced reporting and branded workflows",
            "Best for multi-branch print operations",
        ],
        "currency": "KES",
    },
]


def seed_plans(apps, schema_editor):
    Plan = apps.get_model("billing", "Plan")
    for data in PLANS:
        code = data.pop("code")
        Plan.objects.update_or_create(code=code, defaults=data)
        data["code"] = code  # restore for safety


def unseed_plans(apps, schema_editor):
    Plan = apps.get_model("billing", "Plan")
    Plan.objects.filter(code__in=["FREE", "BIASHARA", "BIASHARA_PLUS", "BIASHARA_MAX"]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0001_initial_billing"),
    ]

    operations = [
        migrations.RunPython(seed_plans, unseed_plans),
    ]
