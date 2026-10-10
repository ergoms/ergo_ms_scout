"""Очередь scout. Остальные роли пока только имена контрактов, без очередей."""

from src.core.utils.celery.base import CeleryModuleConfig


class ScoutCeleryConfig(CeleryModuleConfig):
    def get_task_routes(self):
        return {
            'modules.scout.api.tasks.*': {'queue': 'scout'},
        }

    def get_task_queues(self):
        return {
            'scout': {
                'exchange': 'scout',
                'routing_key': 'scout',
            },
        }

    def get_task_annotations(self):
        return {
            'modules.scout.api.tasks.execute_scout_task': {
                'time_limit': 90,
                'soft_time_limit': 80,
            },
            'modules.scout.api.tasks.dispatch_scout_pending': {
                'time_limit': 30,
                'soft_time_limit': 25,
            },
        }

    def get_max_concurrent_tasks(self) -> int:
        return 4
