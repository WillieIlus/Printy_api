from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("jobs", "0012_managedjob_tracking_token"),
        ("quotes", "0010_quotedraft_artwork_filename_quotedraft_artwork_token_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="quotedraft",
            name="source_job",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=models.deletion.SET_NULL,
                related_name="reorder_quote_drafts",
                to="jobs.managedjob",
            ),
        ),
    ]
