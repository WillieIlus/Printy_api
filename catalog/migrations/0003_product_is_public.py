from django.db import migrations, models


def backfill_product_is_public(apps, schema_editor):
    Product = apps.get_model("catalog", "Product")
    Product.objects.filter(status="UNAVAILABLE").update(is_public=False)
    Product.objects.exclude(status="UNAVAILABLE").update(is_public=True)


class Migration(migrations.Migration):

    dependencies = [
        ("catalog", "0002_booklet_product_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="product",
            name="is_public",
            field=models.BooleanField(
                default=True,
                help_text="Whether this product is visible on public pages and public shop catalogs.",
                verbose_name="is public",
            ),
        ),
        migrations.RunPython(backfill_product_is_public, migrations.RunPython.noop),
    ]
