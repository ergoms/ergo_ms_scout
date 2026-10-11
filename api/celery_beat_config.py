"""Периодическая сверка очереди SCOUT: повтор и восстановление аренды."""

from datetime import timedelta

from src.core.utils.celery_beat import CeleryBeatModuleConfig


class ScoutBeatConfig(CeleryBeatModuleConfig):
    def get_beat_schedule(self):
        return {
            'scout-dispatch-pending': {
                'task': 'modules.scout.api.tasks.dispatch_scout_pending',
                'schedule': timedelta(seconds=30),
                'options': {'queue': 'scout'},
            },
        }
