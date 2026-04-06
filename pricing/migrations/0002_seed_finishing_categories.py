"""
Data migration: seed standard FinishingCategory rows.

These five categories are the set the frontend form exposes.
The slugs are used by the frontend's resolveCategoryId() to look
up the FK when creating/updating finishing rates.
"""

from django.db import migrations


CATEGORIES = [
    {"name": "Lamination", "slug": "lamination", "description": "Glossy, matte, or soft-touch lamination applied per sheet."},
    {"name": "Binding", "slug": "binding", "description": "Spiral, comb, wire-o, or perfect binding."},
    {"name": "Cutting", "slug": "cutting", "description": "Die-cutting, laser cutting, or straight-cutting services."},
    {"name": "Folding", "slug": "folding", "description": "Bi-fold, tri-fold, z-fold, and similar folding services."},
    {"name": "Other", "slug": "other", "description": "Any other post-press finishing service."},
]


def seed_categories(apps, schema_editor):
    FinishingCategory = apps.get_model("pricing", "FinishingCategory")
    for cat in CATEGORIES:
        FinishingCategory.objects.get_or_create(
            slug=cat["slug"],
            defaults={"name": cat["name"], "description": cat["description"]},
        )


def remove_seeded_categories(apps, schema_editor):
    FinishingCategory = apps.get_model("pricing", "FinishingCategory")
    slugs = [c["slug"] for c in CATEGORIES]
    FinishingCategory.objects.filter(slug__in=slugs).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("pricing", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed_categories, reverse_code=remove_seeded_categories),
    ]
