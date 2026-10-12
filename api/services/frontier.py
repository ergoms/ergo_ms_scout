"""Очередь адресов обхода. Загрузчик и извлекатель сюда не входят."""

from datetime import timedelta

from django.utils import timezone

from ..models import CrawlURL, PageSnapshot, Run, Task
from .constants import (
    CRAWL_ACTIVE,
    CRAWL_DONE,
    CRAWL_FAILED,
    CRAWL_QUEUED,
    CRAWL_SKIPPED,
    RUN_PAUSING,
    RUN_RUNNING,
    TASK_CANCELLED,
    TASK_CRAWL,
    TASK_EXTRACT,
    TASK_FETCH,
    TASK_PENDING,
)
from .page_links import iter_hrefs
from .url_norm import host_allowed, resolve_url, rules_match, url_key

_COUNTING = (CRAWL_QUEUED, CRAWL_ACTIVE, CRAWL_DONE)


def blocks_completion(run: Run) -> bool:
    return CrawlURL.objects.filter(run=run, state__in=(CRAWL_QUEUED, CRAWL_ACTIVE)).exists()


def open_site(run: Run) -> None:
    """После подтверждения семена не качаются повторно, с них начинается обход."""
    crawl = _config(run)
    if crawl.get('mode') != 'site':
        return
    for url in run.job_revision.seed_urls:
        snapshot = PageSnapshot.objects.filter(run=run, source_url=url).first()
        if snapshot is not None:
            _remember_done(run, url, snapshot, depth=0)
            _alias_redirect(run, snapshot)
            _schedule_crawl(run, snapshot)
            _schedule_extract(run, snapshot, crawl)
            continue
        remember(run, url, base=url, depth=0, parent=None)


def note_fetch(run: Run, snapshot: PageSnapshot) -> None:
    crawl = _config(run)
    if crawl.get('mode') != 'site' or run.status != RUN_RUNNING:
        return
    _remember_done(run, snapshot.source_url, snapshot, depth=0)
    _alias_redirect(run, snapshot)
    _schedule_crawl(run, snapshot)
    _schedule_extract(run, snapshot, crawl)


def discover(run: Run, snapshot: PageSnapshot, body: bytes) -> None:
    crawl = _config(run)
    if crawl.get('mode') != 'site':
        return
    if run.status not in (RUN_RUNNING, RUN_PAUSING):
        return
    parent = CrawlURL.objects.filter(run=run, snapshot=snapshot).order_by('depth').first()
    depth = 0 if parent is None else parent.depth + 1
    page = snapshot.final_url or snapshot.source_url
    hrefs, base = iter_hrefs(body, snapshot.content_type, snapshot.charset, page)
    for href in hrefs:
        remember(run, href, base=base, depth=depth, parent=parent)


def release_queued(run: Run) -> None:
    if run.status != RUN_RUNNING:
        return
    for row in list(CrawlURL.objects.filter(run=run, state=CRAWL_QUEUED)):
        _schedule_fetch(run, row)


def remember(run: Run, raw_url: str, *, base: str, depth: int, parent) -> CrawlURL | None:
    crawl = _config(run)
    if run.status not in (RUN_RUNNING, RUN_PAUSING):
        return None
    normalized, reason = resolve_url(raw_url, base, crawl.get('drop_query'))
    if normalized is None:
        return _skip(run, raw_url or reason, reason, depth, parent)
    key = url_key(normalized)
    existing = CrawlURL.objects.filter(run=run, url_key=key).first()
    if existing is not None:
        return existing
    if depth > int(crawl.get('max_depth') or 0):
        return _skip(run, normalized, 'depth', depth, parent, key)
    if not host_allowed(normalized, crawl.get('allowed_hosts')):
        return _skip(run, normalized, 'host', depth, parent, key)
    if not rules_match(normalized, crawl.get('visit')):
        return _skip(run, normalized, 'visit', depth, parent, key)
    if _budget_closed(run, crawl):
        return _skip(run, normalized, run.stop_reason or 'limit', depth, parent, key)
    row = CrawlURL.objects.create(
        run=run,
        original_url=(raw_url or normalized)[:2000],
        normalized_url=normalized,
        url_key=key,
        depth=depth,
        parent=parent,
        state=CRAWL_QUEUED,
    )
    if run.status == RUN_RUNNING:
        _schedule_fetch(run, row)
    return row


def _budget_closed(run: Run, crawl: dict) -> bool:
    used = CrawlURL.objects.filter(run=run, state__in=_COUNTING).count()
    if used >= int(crawl.get('max_pages') or 1):
        _stop(run, 'max_pages')
        return True
    if CrawlURL.objects.filter(run=run).count() >= int(crawl.get('frontier_cap') or 1):
        _stop(run, 'frontier')
        return True
    started = run.started_at or timezone.now()
    if timezone.now() > started + timedelta(seconds=int(crawl.get('max_seconds') or 1)):
        _stop(run, 'time')
        return True
    total = sum(PageSnapshot.objects.filter(run=run).values_list('byte_size', flat=True))
    if total >= int(crawl.get('max_total_bytes') or 1):
        _stop(run, 'bytes')
        return True
    return False


def _stop(run: Run, reason: str) -> None:
    if run.stop_reason:
        return
    run.stop_reason = reason
    run.save(update_fields=['stop_reason'])


def _skip(run, url: str, reason: str, depth: int, parent, key: str = '') -> CrawlURL:
    digest = key or url_key(url or reason)
    row, _created = CrawlURL.objects.get_or_create(
        run=run,
        url_key=digest,
        defaults={
            'original_url': (url or '')[:2000],
            'normalized_url': (url or '')[:2000],
            'depth': depth,
            'parent': parent,
            'state': CRAWL_SKIPPED,
            'skip_reason': (reason or '')[:80],
        },
    )
    return row


def _remember_done(run: Run, url: str, snapshot: PageSnapshot, depth: int) -> None:
    normalized, _reason = resolve_url(url, url, _config(run).get('drop_query'))
    if normalized is None:
        return
    key = url_key(normalized)
    row, created = CrawlURL.objects.get_or_create(
        run=run,
        url_key=key,
        defaults={
            'original_url': url[:2000],
            'normalized_url': normalized,
            'depth': depth,
            'state': CRAWL_DONE,
            'snapshot': snapshot,
        },
    )
    if created:
        return
    row.state = CRAWL_DONE
    row.snapshot = snapshot
    row.save(update_fields=['state', 'snapshot'])


def _alias_redirect(run: Run, snapshot: PageSnapshot) -> None:
    drop = _config(run).get('drop_query')
    final, _reason = resolve_url(snapshot.final_url or '', snapshot.final_url or snapshot.source_url, drop)
    source, _ignored = resolve_url(snapshot.source_url, snapshot.source_url, drop)
    if not final or not source or url_key(final) == url_key(source):
        return
    key = url_key(final)
    row = CrawlURL.objects.filter(run=run, url_key=key).first()
    if row is None:
        CrawlURL.objects.create(
            run=run,
            original_url=final[:2000],
            normalized_url=final,
            url_key=key,
            depth=0,
            state=CRAWL_DONE,
            skip_reason='redirect',
            snapshot=snapshot,
        )
        return
    if row.state == CRAWL_QUEUED:
        row.state = CRAWL_DONE
        row.snapshot = snapshot
        row.skip_reason = 'redirect'
        row.save(update_fields=['state', 'snapshot', 'skip_reason'])
        Task.objects.filter(
            run=run,
            idempotency_key=f'fetch:{key}',
            status=TASK_PENDING,
        ).update(status=TASK_CANCELLED)


def _schedule_fetch(run: Run, row: CrawlURL) -> None:
    delay_ms = int(_config(run).get('fetch_delay_ms') or 0)
    task, created = Task.objects.get_or_create(
        run=run,
        idempotency_key=f'fetch:{row.url_key}',
        defaults={
            'kind': TASK_FETCH,
            'status': TASK_PENDING,
            'payload': {'url': row.normalized_url, 'crawl_public_id': str(row.public_id)},
            'fencing_token': 1,
        },
    )
    if created and delay_ms and Task.objects.filter(run=run, kind=TASK_FETCH).exclude(pk=task.pk).exists():
        task.next_retry_at = timezone.now() + timedelta(milliseconds=delay_ms)
        task.save(update_fields=['next_retry_at'])
    row.state = CRAWL_ACTIVE
    row.save(update_fields=['state'])


def _schedule_crawl(run: Run, snapshot: PageSnapshot) -> None:
    Task.objects.get_or_create(
        run=run,
        idempotency_key=f'crawl:{snapshot.public_id}',
        defaults={
            'kind': TASK_CRAWL,
            'status': TASK_PENDING,
            'payload': {'snapshot_public_id': str(snapshot.public_id)},
            'fencing_token': 1,
        },
    )


def _schedule_extract(run: Run, snapshot: PageSnapshot, crawl: dict) -> None:
    target = snapshot.final_url or snapshot.source_url
    if not rules_match(target, crawl.get('extract_on')) and not rules_match(snapshot.source_url, crawl.get('extract_on')):
        return
    revision = run.job_revision
    key = f'extract:{snapshot.public_id}:{revision.schema_revision.public_id}:{revision.rules_revision.public_id}'
    Task.objects.get_or_create(
        run=run,
        idempotency_key=key,
        defaults={
            'kind': TASK_EXTRACT,
            'status': TASK_PENDING,
            'payload': {'snapshot_public_id': str(snapshot.public_id)},
            'fencing_token': 1,
        },
    )


def _config(run: Run) -> dict:
    return run.job_revision.crawl or {}


def mark_failed(run: Run, url: str) -> None:
    normalized, _reason = resolve_url(url, url, _config(run).get('drop_query'))
    if normalized is None:
        return
    CrawlURL.objects.filter(run=run, url_key=url_key(normalized), state=CRAWL_ACTIVE).update(
        state=CRAWL_FAILED,
    )
