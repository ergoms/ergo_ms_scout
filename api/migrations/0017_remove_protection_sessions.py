from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ('scout', '0016_protection_sessions'),
    ]

    operations = [
        migrations.DeleteModel(name='ProtectionSession'),
    ]
