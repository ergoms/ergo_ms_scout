"""Очередь адресов обхода. Загрузчик и извлекатель сюда не входят."""

from datetime import timedelta

from django.db import transaction
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
    TASK_RETRY_WAIT,
)
from .page_links import iter_hrefs
from .url_norm import host_allowed, resolve_url, rules_match, url_key

_COUNTING = (CRAWL_QUEUED, CRAWL_ACTIVE, CRAWL_DONE, CRAWL_FAILED)


def blocks_completion(run: Run) -> bool:
    return CrawlURL.objects.filter(run=run, state__in=(CRAWL_QUEUED, CRAWL_ACTIVE)).exists()


def open_site(run: Run) -> None:
    """После подтверждения семена не качаются повторно, с них начинается обход."""
    crawl = _config(run)
    if crawl.get('mode') != 'site':
        return
    for url in run.job_revision.seed_urls:
        remember(run, url, base=url, depth=0, parent=None)


def note_fetch(run: Run, snapshot: PageSnapshot) -> None:
    crawl = _config(run)
    if crawl.get('mode') != 'site' or run.status not in (RUN_RUNNING, RUN_PAUSING):
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
    linked = CrawlURL.objects.filter(run=run, snapshot=snapshot)
    # Алиас редиректа — тот же документ. Его глубина не должна заменять глубину адреса, с которого пришли.
    parent = linked.exclude(skip_reason='redirect').order_by('depth', 'pk').first()
    if parent is None:
        parent = linked.order_by('depth', 'pk').first()
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
        return _revisit(run, existing, depth, parent, crawl, normalized)
    if CrawlURL.objects.filter(run=run).count() >= int(crawl.get('frontier_cap') or 1):
        _stop(run, 'frontier')
        return None
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
    _attach_or_fetch(run, row)
    return CrawlURL.objects.get(pk=row.pk)


def _revisit(run: Run, existing: CrawlURL, depth: int, parent, crawl: dict, normalized: str) -> CrawlURL:
    """Короткий путь уменьшает глубину. Пропуск из-за глубины можно вернуть, остальные пропуски — нет."""
    enqueue = False
    spread = False
    with transaction.atomic():
        row = CrawlURL.objects.select_for_update().get(pk=existing.pk)
        if depth >= row.depth:
            return row
        if row.state == CRAWL_FAILED or (row.state == CRAWL_SKIPPED and row.skip_reason != 'depth'):
            return row
        row.depth = depth
        row.parent = parent
        fields = ['depth', 'parent']
        if row.state == CRAWL_SKIPPED:
            reason = _admit(run, crawl, normalized, depth)
            if reason is not None:
                row.skip_reason = (reason or '')[:80]
                row.save(update_fields=fields + ['skip_reason'])
                return row
            row.state = CRAWL_QUEUED
            row.skip_reason = ''
            row.save(update_fields=fields + ['state', 'skip_reason'])
            enqueue = True
        else:
            row.save(update_fields=fields)
            spread = row.state == CRAWL_DONE and bool(row.snapshot_id)
    if enqueue:
        _attach_or_fetch(run, row)
        row.refresh_from_db()
    elif spread:
        _spread(run, row)
    return row


def _admit(run: Run, crawl: dict, normalized: str, depth: int) -> str | None:
    if depth > int(crawl.get('max_depth') or 0):
        return 'depth'
    if not host_allowed(normalized, crawl.get('allowed_hosts')):
        return 'host'
    if not rules_match(normalized, crawl.get('visit')):
        return 'visit'
    if _budget_closed(run, crawl):
        return run.stop_reason or 'limit'
    return None


def _attach_or_fetch(run: Run, row: CrawlURL) -> None:
    crawl = _config(run)
    snapshot = PageSnapshot.objects.filter(run=run, source_url=row.normalized_url).first()
    if snapshot is not None:
        from .crawl_budget import snapshot_bytes
        if snapshot_bytes(run) + snapshot.byte_size > int(crawl.get('max_total_bytes') or 1):
            _stop(run, 'bytes')
            row.state = CRAWL_SKIPPED
            row.skip_reason = 'bytes'
            row.save(update_fields=['state', 'skip_reason'])
            return
        note_fetch(run, snapshot)
        return
    if run.status == RUN_RUNNING:
        _schedule_fetch(run, row)


def _spread(run: Run, row: CrawlURL) -> None:
    snapshot = row.snapshot
    if snapshot is None or not snapshot.content_path:
        return
    from .snapshot_store import read_snapshot_bytes
    try:
        body = read_snapshot_bytes(snapshot.content_path)
    except (OSError, ValueError):
        return
    discover(run, snapshot, body)


def _budget_closed(run: Run, crawl: dict) -> bool:
    if run.stop_reason in ('time', 'bytes', 'frontier'):
        return True
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
    from .crawl_budget import snapshot_bytes
    total = snapshot_bytes(run)
    if total >= int(crawl.get('max_total_bytes') or 1):
        _stop(run, 'bytes')
        return True
    return False


def _stop(run: Run, reason: str) -> None:
    if run.stop_reason:
        return
    run.stop_reason = reason
    run.save(update_fields=['stop_reason'])


def _skip(run, url: str, reason: str, depth: int, parent, key: str = '') -> CrawlURL | None:
    digest = key or url_key(url or reason)
    existing = CrawlURL.objects.filter(run=run, url_key=digest).first()
    if existing is not None:
        return existing
    if CrawlURL.objects.filter(run=run).count() >= int(_config(run).get('frontier_cap') or 1):
        _stop(run, 'frontier')
        return None
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
    source_row = CrawlURL.objects.filter(run=run, url_key=url_key(source)).first()
    # Редирект не начинает обход заново: конечный адрес получает глубину найденной страницы.
    source_depth = 0 if source_row is None else source_row.depth
    key = url_key(final)
    row = CrawlURL.objects.filter(run=run, url_key=key).first()
    if row is None:
        if CrawlURL.objects.filter(run=run).count() >= int(_config(run).get('frontier_cap') or 1):
            _stop(run, 'frontier')
            return
        CrawlURL.objects.create(
            run=run,
            original_url=final[:2000],
            normalized_url=final,
            url_key=key,
            depth=source_depth,
            state=CRAWL_DONE,
            skip_reason='redirect',
            snapshot=snapshot,
        )
        return
    depth = min(row.depth, source_depth)
    lowered = depth < row.depth
    if row.state in (CRAWL_QUEUED, CRAWL_ACTIVE):
        task = Task.objects.filter(run=run, idempotency_key=f'fetch:{key}').first()
        if task is None or task.status in (TASK_PENDING, TASK_RETRY_WAIT):
            row.depth = depth
            row.state = CRAWL_DONE
            row.snapshot = snapshot
            row.skip_reason = 'redirect'
            row.save(update_fields=['depth', 'state', 'snapshot', 'skip_reason'])
            Task.objects.filter(
                run=run,
                idempotency_key=f'fetch:{key}',
                status__in=(TASK_PENDING, TASK_RETRY_WAIT),
            ).update(status=TASK_CANCELLED)
            return
    max_depth = int(_config(run).get('max_depth') or 0)
    if row.state == CRAWL_SKIPPED and row.skip_reason == 'depth' and depth <= max_depth:
        row.depth = depth
        row.state = CRAWL_DONE
        row.snapshot = snapshot
        row.skip_reason = 'redirect'
        row.save(update_fields=['depth', 'state', 'snapshot', 'skip_reason'])
        return
    if not lowered:
        return
    row.depth = depth
    row.save(update_fields=['depth'])
    if row.state == CRAWL_DONE and row.snapshot_id:
        _spread(run, row)


def _schedule_fetch(run: Run, row: CrawlURL) -> None:
    Task.objects.get_or_create(
        run=run,
        idempotency_key=f'fetch:{row.url_key}',
        defaults={
            'kind': TASK_FETCH,
            'status': TASK_PENDING,
            'payload': {'url': row.normalized_url, 'crawl_public_id': str(row.public_id)},
            'fencing_token': 1,
        },
    )
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
    if not rules_match(target, crawl.get('extract_on')):
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
