from django.db import migrations


def populate_menu(apps, schema_editor):
    from src.core.cms.adp.menu.migration_utils import MenuMigrationHelper

    helper = MenuMigrationHelper(apps, 'modules/scout')
    helper.clear_module_items()
    helper.create_route('SCOUT', 'ScoutHome', icon='Radar')


def reverse_menu(apps, schema_editor):
    from src.core.cms.adp.menu.migration_utils import MenuMigrationHelper

    MenuMigrationHelper(apps, 'modules/scout').clear_module_items()


class Migration(migrations.Migration):
    dependencies = [
        ('cms_adp', '0065_retarget_auth_user_foreign_keys'),
        ('scout', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(populate_menu, reverse_menu),
    ]
