"""Команды сбора. Тот же план, что у веб-страницы. Работа уходит в очередь."""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from ...services.errors import ScoutError
from ...services.constants import CRAWL_LIMITS
from ...services.runs import create_job, explore_job
from ...services.tasks_exec import schedule_publish


class Command(BaseCommand):
    help = 'Создать задание SCOUT по адресу и поставить предварительную загрузку в очередь.'

    def add_arguments(self, parser):
        parser.add_argument('--url', action='append', required=True)
        parser.add_argument('--field', action='append', required=True)
        parser.add_argument('--record', default='')
        parser.add_argument('--crawl', default='pages', choices=('pages', 'site'))
        parser.add_argument('--allow-host', action='append', default=[])
        parser.add_argument('--visit-prefix', default='')
        parser.add_argument('--extract-prefix', default='')
        parser.add_argument('--depth', type=int, default=2)
        parser.add_argument('--max-pages', type=int, default=50)
        parser.add_argument('--preview-url', action='append', default=[])
        parser.add_argument('--preview-pages', type=int, default=CRAWL_LIMITS['preview_pages'])
        parser.add_argument('--max-seconds', type=int, default=CRAWL_LIMITS['max_seconds'])
        parser.add_argument('--max-total-bytes', type=int, default=CRAWL_LIMITS['max_total_bytes'])
        parser.add_argument('--frontier-cap', type=int, default=CRAWL_LIMITS['frontier_cap'])
        parser.add_argument('--fetch-delay-ms', type=int, default=CRAWL_LIMITS['fetch_delay_ms'])
        parser.add_argument('--parallel-fetch', type=int, default=CRAWL_LIMITS['parallel_fetch'])
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
                record=_parse_record(options['record']),
                crawl=_crawl(options),
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


def _crawl(options) -> dict:
    visit = options['visit_prefix']
    extract_on = options['extract_prefix']
    return {
        'mode': options['crawl'],
        'allowed_hosts': options['allow_host'],
        'visit': [{'kind': 'prefix', 'pattern': visit}] if visit else [],
        'extract_on': [{'kind': 'prefix', 'pattern': extract_on}] if extract_on else [],
        'max_depth': options['depth'],
        'max_pages': options['max_pages'],
        'preview_urls': options['preview_url'],
        'preview_pages': options['preview_pages'],
        'max_seconds': options['max_seconds'],
        'max_total_bytes': options['max_total_bytes'],
        'frontier_cap': options['frontier_cap'],
        'fetch_delay_ms': options['fetch_delay_ms'],
        'parallel_fetch': options['parallel_fetch'],
    }


def _parse_record(raw: str):
    text = (raw or '').strip()
    if not text:
        return None
    kind, colon, selector = text.partition(':')
    if not colon or kind not in ('css', 'xpath') or not selector:
        raise CommandError('Карточка задаётся как css:селектор или xpath:селектор.')
    return {'kind': kind, 'selector': selector}


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
