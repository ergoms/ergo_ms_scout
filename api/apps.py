from django.apps import AppConfig


class ScoutConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'modules.scout.api'
    label = 'scout'
    verbose_name = 'SCOUT'

    def ready(self):
        from . import integrations  # noqa: F401
