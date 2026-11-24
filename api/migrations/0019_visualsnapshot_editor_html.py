from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('scout', '0018_runimage')]

    operations = [
        migrations.AddField(
            model_name='visualsnapshot',
            name='editor_html',
            field=models.TextField(blank=True, default=''),
        ),
        migrations.AddField(
            model_name='visualsnapshot',
            name='editor_tags',
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
