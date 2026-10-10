"""Подтверждение плана и запуск основного извлечения."""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from src.core.cms.adp.services.permissions import PermissionService

from ...models import Run
from ...services.errors import ScoutError
from ...services.runs import approve_run, start_run
from ...services.tasks_exec import schedule_publish


class Command(BaseCommand):
    help = 'Подтвердить план запуска SCOUT и поставить извлечение в очередь.'

    def add_arguments(self, parser):
        parser.add_argument('--run', required=True)
        parser.add_argument('--user-public-id', required=True)

    def handle(self, *args, **options):
        user = get_user_model().objects.filter(public_id=options['user_public_id']).first()
        if user is None:
            raise CommandError('Пользователь с таким public_id не найден.')
        run = Run.objects.filter(public_id=options['run']).first()
        if run is None:
            raise CommandError('Запуск не найден.')
        if run.user_public_id != user.public_id and not PermissionService.is_admin(user):
            raise CommandError('Этот запуск принадлежит другому пользователю.')
        try:
            run = approve_run(run=run, user_public_id=user.public_id)
            run = start_run(run=run)
        except ScoutError as exc:
            raise CommandError(str(exc)) from exc
        schedule_publish(run.public_id)
        run.refresh_from_db()
        self.stdout.write(f'run {run.public_id} status {run.status}')
