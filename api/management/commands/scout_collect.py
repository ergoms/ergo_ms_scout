"""Команды сбора. Тот же план, что у веб-страницы. Работа уходит в очередь."""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from ...services.errors import ScoutError
from ...services.runs import create_job, explore_job
from ...services.tasks_exec import schedule_publish


class Command(BaseCommand):
    help = 'Создать задание SCOUT по адресу и поставить предварительную загрузку в очередь.'

    def add_arguments(self, parser):
        parser.add_argument('--url', action='append', required=True)
        parser.add_argument('--field', action='append', required=True)
        parser.add_argument('--user-public-id', required=True)
        parser.add_argument('--title', default='Сбор')

    def handle(self, *args, **options):
        user = _user(options['user_public_id'])
        fields = [_parse_field(raw) for raw in options['field']]
        try:
            job = create_job(
                user_public_id=user.public_id,
                title=options['title'],
                seed_urls=options['url'],
                fields=fields,
            )
            run = explore_job(job=job, user_public_id=user.public_id)
        except ScoutError as exc:
            raise CommandError(str(exc)) from exc
        schedule_publish(run.public_id)
        run.refresh_from_db()
        self.stdout.write(f'job {job.public_id}')
        self.stdout.write(f'run {run.public_id} status {run.status}')
        self.stdout.write(
            'Дальше очередь scout. Когда статус станет awaiting_confirmation: '
            'ergoms scout:approve-run --run <public_id> --user-public-id <public_id>'
        )


def _user(public_id: str):
    user = get_user_model().objects.filter(public_id=public_id).first()
    if user is None:
        raise CommandError('Пользователь с таким public_id не найден.')
    return user


def _parse_field(raw: str) -> dict:
    name, separator, rest = raw.partition('=')
    kind, colon, selector = rest.partition(':')
    if not separator or not colon or kind not in ('css', 'xpath'):
        raise CommandError('Поле задаётся как имя=css:селектор или имя=xpath:селектор.')
    many = name.endswith('*')
    return {
        'name': name[:-1] if many else name,
        'kind': kind,
        'selector': selector,
        'many': many,
    }
