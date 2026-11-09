"""Временные снимки открытых страниц для настройки правил до создания задания."""

import logging
from datetime import timedelta
from time import sleep

from django.db import transaction
from django.utils import timezone
from kombu.exceptions import OperationalError

from ..models import VisualSnapshot
from .constants import DEFAULT_LIMITS
from .errors import FetchError, ScoutError
from .public_http import fetch_public_html, public_url_allowed
from .proxy_http import fetch_proxy_html, proxy_target_allowed
from .proxy_profiles import PROXY_RETRY_DELAYS, validate_revision
from .tasks_exec import inline_runner
from .visual_rules import VisualDocument

logger = logging.getLogger('modules.scout')
SNAPSHOT_TTL = timedelta(hours=1)
_LOAD_DEADLINE = timedelta(minutes=2)
_PENDING = ('queued', 'fetching')


def create_snapshot(user_public_id, url, proxy_profile_revision_public_id=None):
    if not isinstance(url, str) or len(url) > 2000 or not url.startswith(('http://', 'https://')):
        raise ScoutError('invalid', 'Укажите HTTP(S)-адрес страницы длиной до 2000 символов')
    public_url_allowed(url)
    with transaction.atomic():
        revision_id = validate_revision(proxy_profile_revision_public_id, user_public_id, lock=True)
        if revision_id:
            proxy_target_allowed(url)
        return VisualSnapshot.objects.create(
            user_public_id=user_public_id, source_url=url, proxy_profile_revision_public_id=revision_id,
        )


def enqueue_snapshot(public_id):
    if inline_runner():
        load_snapshot(public_id)
        return
    from ..tasks import load_visual_snapshot

    try:
        load_visual_snapshot.apply_async(args=[str(public_id)], expires=int(_LOAD_DEADLINE.total_seconds()))
    except OperationalError:
        VisualSnapshot.objects.filter(public_id=public_id, status='queued').update(
            status='failed', error_code='queue_unavailable', error_detail='Очередь загрузки недоступна',
        )


def load_snapshot(public_id):
    pending = VisualSnapshot.objects.filter(public_id=public_id, created_at__gt=timezone.now() - _LOAD_DEADLINE)
    if not pending.filter(status='queued').update(status='fetching'):
        return
    snapshot = pending.get()
    try:
        page = _fetch_snapshot(snapshot)
        VisualDocument(page['body'], page['charset'])
    except ScoutError as exc:
        result = {'status': 'failed', 'error_code': exc.code, 'error_detail': str(exc)[:400]}
    except Exception:
        logger.exception('SCOUT не подготовил снимок %s', public_id)
        result = {'status': 'failed', 'error_code': 'snapshot_failed', 'error_detail': 'Не удалось подготовить снимок'}
    else:
        result = {'status': 'ready', 'body': page['body'], 'charset': page['charset'], 'final_url': page['final_url']}
    pending.filter(status='fetching', created_at__gt=timezone.now() - _LOAD_DEADLINE).update(**result)


def _fetch_snapshot(snapshot):
    if not snapshot.proxy_profile_revision_public_id:
        return fetch_public_html(
            snapshot.source_url, timeout_seconds=DEFAULT_LIMITS['timeout_seconds'], max_bytes=DEFAULT_LIMITS['max_bytes'],
        )
    for delay in (*PROXY_RETRY_DELAYS, None):
        try:
            return fetch_proxy_html(
                snapshot.source_url, revision_id=snapshot.proxy_profile_revision_public_id,
                owner=snapshot.user_public_id, timeout_seconds=10, max_bytes=DEFAULT_LIMITS['max_bytes'],
            )
        except FetchError as exc:
            if exc.code != 'proxy_unavailable' or delay is None:
                raise
        sleep(delay)


def snapshot_status(snapshot):
    if snapshot.status in _PENDING:
        if snapshot.created_at <= timezone.now() - _LOAD_DEADLINE:
            return 'failed', {'code': 'load_timeout', 'detail': 'Время ожидания снимка истекло'}
        return 'loading', None
    if snapshot.status == 'failed':
        return 'failed', {'code': snapshot.error_code, 'detail': snapshot.error_detail}
    return 'ready', None


def delete_expired_snapshots():
    from .retention_locks import resource_lock
    from ..models import RetentionLease

    for row in VisualSnapshot.objects.filter(created_at__lte=timezone.now() - SNAPSHOT_TTL).iterator():
        with resource_lock(f'visual:{row.pk}', wait=False) as acquired:
            if acquired and not RetentionLease.objects.filter(
                resource=f'visual:{row.pk}', expires_at__gt=timezone.now(),
            ).exists():
                row.delete()
