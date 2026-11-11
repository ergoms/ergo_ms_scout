"""Общие лимиты обхода; вызывается только под блокировкой Run."""

from datetime import timedelta

from django.db.models import Sum
from django.utils import timezone

from ..models import Attempt, CrawlURL, PageSnapshot, Task
from .constants import TASK_FETCH, TASK_LEASED, TASK_PENDING, TASK_RETRY_WAIT, TASK_CANCELLED, TASK_SITEMAP


def snapshot_bytes(run) -> int:
    used = CrawlURL.objects.filter(run=run, state='done', snapshot__isnull=False).values('snapshot_id')
    return PageSnapshot.objects.filter(run=run, pk__in=used).aggregate(total=Sum('byte_size'))['total'] or 0


def stop_fetches(run, reason) -> None:
    if not run.stop_reason:
        run.stop_reason = reason
        run.save(update_fields=['stop_reason'])
    pending = Task.objects.filter(run=run, kind__in=(TASK_FETCH, TASK_SITEMAP), status__in=(TASK_PENDING, TASK_RETRY_WAIT))
    keys = [key.removeprefix('fetch:') for key in pending.filter(kind=TASK_FETCH).values_list('idempotency_key', flat=True)]
    pending.update(status=TASK_CANCELLED, lease_until=None, published_at=None)
    CrawlURL.objects.filter(run=run, url_key__in=keys, state='active').update(
        state='skipped', skip_reason=reason,
    )
    CrawlURL.objects.filter(run=run, state='queued').update(state='skipped', skip_reason=reason)


def enforce_deadline(run) -> bool:
    config = run.job_revision.crawl or {}
    if config.get('mode') != 'site' or run.status != 'running':
        return False
    if run.started_at and timezone.now() >= run.started_at + timedelta(seconds=config['max_seconds']):
        stop_fetches(run, 'time')
        return True
    return False


def admit_fetch(run, task) -> bool:
    config = run.job_revision.crawl or {}
    if config.get('mode') != 'site' or run.status != 'running':
        return True
    if enforce_deadline(run):
        return False
    now = timezone.now()
    delay = timedelta(milliseconds=config.get('fetch_delay_ms') or 0)
    previous = Attempt.objects.filter(task__run=run, task__kind=TASK_FETCH).order_by('-started_at').first()
    if previous and previous.started_at + delay > now:
        task.next_retry_at = previous.started_at + delay
        task.published_at = None
        task.save(update_fields=['next_retry_at', 'published_at'])
        return False
    available = config['max_total_bytes'] - snapshot_bytes(run)
    if available <= 0:
        stop_fetches(run, 'bytes')
        return False
    page_limit = run.job_revision.limits['max_bytes']
    allowance = min(available, page_limit)
    # Временные резервы не уменьшают допустимый размер нового ответа.
    reserved = sum(int(payload.get('byte_allowance') or page_limit)
                   for payload in Task.objects.filter(run=run, kind=TASK_FETCH, status=TASK_LEASED)
                   .values_list('payload', flat=True))
    if available - reserved < allowance:
        task.published_at = None
        task.save(update_fields=['published_at'])
        return False
    task.payload = dict(task.payload, byte_allowance=allowance)
    task.save(update_fields=['payload'])
    return True


def reconcile(run) -> None:
    """Восстанавливает frontier из сохранённого результата, а не из брокера."""
    if (run.job_revision.crawl or {}).get('mode') != 'site':
        return
    from .frontier import note_fetch, release_queued

    active = {f'fetch:{row.url_key}': row for row in CrawlURL.objects.filter(run=run, state='active')}
    tasks = {
        task.idempotency_key: task
        for task in Task.objects.filter(run=run, idempotency_key__in=active).only(
            'idempotency_key', 'status', 'payload', 'error_code',
        )
    }
    for key, row in active.items():
        task = tasks.get(key)
        if task is None:
            row.state = 'queued'
            row.save(update_fields=['state'])
        elif task.status == 'succeeded':
            snapshot = PageSnapshot.objects.filter(run=run, source_url=task.payload.get('url')).order_by('-fetched_at', '-pk').first()
            if snapshot:
                note_fetch(run, snapshot)
        elif task.status in ('failed', 'cancelled'):
            row.state = 'failed' if task.status == 'failed' else 'skipped'
            row.skip_reason = task.error_code or task.status
            row.save(update_fields=['state', 'skip_reason'])
    if not enforce_deadline(run):
        release_queued(run)
