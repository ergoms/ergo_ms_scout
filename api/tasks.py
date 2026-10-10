"""Тонкие задачи Celery. Состояние сбора живёт в базе, не в сообщении."""

import logging

from celery import shared_task

from .services.tasks_exec import inline_runner, publish_ready, run_one

logger = logging.getLogger('celery.module.scout.tasks')


@shared_task(name='modules.scout.api.tasks.execute_scout_task')
def execute_scout_task(task_public_id: str, fencing_token: int) -> dict:
    changed = run_one(str(task_public_id), int(fencing_token))
    if changed and not inline_runner():
        from .models import Task

        run_id = (
            Task.objects.filter(public_id=task_public_id)
            .values_list('run__public_id', flat=True)
            .first()
        )
        if run_id:
            dispatch_scout_pending.delay(str(run_id))
    return {'changed': bool(changed)}


@shared_task(name='modules.scout.api.tasks.dispatch_scout_pending')
def dispatch_scout_pending(run_public_id: str = '') -> dict:
    publish_ready(run_public_id or None)
    logger.info('SCOUT сверил очередь запуска %s', run_public_id or '*')
    return {'ok': True}
