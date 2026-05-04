from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('leads', '0002_demoaction'),
    ]

    operations = [
        migrations.CreateModel(
            name='EarlyAccessCampaign',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True, help_text='Timestamp when the record was created.', verbose_name='created at')),
                ('updated_at', models.DateTimeField(auto_now=True, help_text='Timestamp when the record was last updated.', verbose_name='updated at')),
                ('city', models.CharField(max_length=100, unique=True)),
                ('total_spots', models.PositiveIntegerField(default=20)),
                ('manual_reserved_spots', models.PositiveIntegerField(default=0)),
                ('active', models.BooleanField(default=True)),
            ],
            options={
                'verbose_name': 'Early Access Campaign',
                'verbose_name_plural': 'Early Access Campaigns',
            },
        ),
    ]
