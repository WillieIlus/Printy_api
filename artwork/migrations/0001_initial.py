from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name='UploadedArtwork',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('file', models.FileField(upload_to='artwork/')),
                ('file_type', models.CharField(blank=True, max_length=20)),
                ('detected_pages', models.IntegerField(blank=True, null=True)),
                ('detected_width_mm', models.FloatField(blank=True, null=True)),
                ('detected_height_mm', models.FloatField(blank=True, null=True)),
                ('analysis', models.JSONField(blank=True, default=dict)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
        ),
    ]
