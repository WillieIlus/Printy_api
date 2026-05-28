from decimal import Decimal

from django.db import migrations


COMMON_STOCKS = [
    ("Art Paper 90gsm", 90, "MATTE", "matt"),
    ("Art Paper 115gsm", 115, "MATTE", "matt"),
    ("Art Paper 128gsm", 128, "MATTE", "matt"),
    ("Art Paper 150gsm", 150, "MATTE", "matt"),
    ("Art Paper 170gsm", 170, "MATTE", "matt"),
    ("Art Paper 200gsm", 200, "MATTE", "matt"),
    ("Art Paper 250gsm", 250, "MATTE", "artcard"),
    ("Art Paper 300gsm", 300, "MATTE", "artcard"),
    ("Bond 80gsm", 80, "UNCOATED", "bond"),
    ("Bond 90gsm", 90, "UNCOATED", "bond"),
    ("Sticker Paper Matte", 150, "MATTE", "tictac"),
    ("Sticker Paper Glossy", 150, "GLOSS", "tictac"),
    ("Synthetic / PP", 200, "OTHER", "special"),
]


def seed_common_paper_stocks(apps, schema_editor):
    Shop = apps.get_model("shops", "Shop")
    Paper = apps.get_model("inventory", "Paper")

    for shop in Shop.objects.all().iterator():
        for name, gsm, paper_type, category in COMMON_STOCKS:
            defaults = {
                "name": name,
                "display_name": name,
                "category": category,
                "buying_price": Decimal("0.00"),
                "selling_price": Decimal("0.00"),
                "quantity_in_stock": 0,
                "reorder_level": 0,
                "is_active": True,
                "is_sticker_stock": category == "tictac",
                "is_specialty": category == "special",
                "is_cover_stock": gsm >= 170,
                "is_insert_stock": category != "tictac",
            }
            paper, created = Paper.objects.get_or_create(
                shop=shop,
                sheet_size="SRA3",
                gsm=gsm,
                paper_type=paper_type,
                defaults=defaults,
            )
            if not created and not paper.display_name:
                paper.display_name = name
                paper.save(update_fields=["display_name", "updated_at"])


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0002_paper_category_paper_display_name_and_more"),
        ("shops", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed_common_paper_stocks, migrations.RunPython.noop),
    ]
