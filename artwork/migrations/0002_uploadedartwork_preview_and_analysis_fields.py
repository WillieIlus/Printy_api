from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('artwork', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='uploadedartwork',
            name='analysis_error',
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='uploadedartwork',
            name='analysis_status',
            field=models.CharField(default='pending', max_length=20),
        ),
        migrations.AddField(
            model_name='uploadedartwork',
            name='analysis_warnings',
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name='uploadedartwork',
            name='preview_image',
            field=models.ImageField(blank=True, null=True, upload_to='artwork/previews/'),
        ),
    ]
