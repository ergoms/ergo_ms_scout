"""Очередь адресов обхода. Загрузчик и извлекатель сюда не входят."""

from datetime import timedelta

from django.db import transaction
from django.db.models import Case, Count, Q, When
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
    TASK_SUCCEEDED,
)
from .page_links import iter_hrefs
from .page_types import matching_page_types
from .fetch_failures import STOP_FETCH_FAILURES
from .url_norm import host_allowed, resolve_url, rules_match, url_key

_COUNTING = (CRAWL_QUEUED, CRAWL_ACTIVE, CRAWL_DONE, CRAWL_FAILED)
_DISCOVERY_BATCH_SIZE = 250


def blocks_completion(run: Run) -> bool:
    return CrawlURL.objects.filter(run=run, state__in=(CRAWL_QUEUED, CRAWL_ACTIVE)).exists()


def open_site(run: Run, sitemap_urls: tuple[str, ...] | list[str] = ()) -> None:
    """Начальные страницы и адреса карты открывают обход на глубине 0."""
    crawl = _config(run)
    if crawl.get('mode') != 'site':
        return
    for url in run.job_revision.seed_urls:
        remember(run, url, base=url, depth=0, parent=None)
    for offset in range(0, len(sitemap_urls), _DISCOVERY_BATCH_SIZE):
        if run.stop_reason == 'frontier':
            break
        batch = sitemap_urls[offset:offset + _DISCOVERY_BATCH_SIZE]
        _register_urls(run, crawl, batch, base='', depth=0, parent=None)


def note_fetch(run: Run, snapshot: PageSnapshot) -> None:
    crawl = _config(run)
    if crawl.get('mode') != 'site' or run.status not in (RUN_RUNNING, RUN_PAUSING):
        return
    _remember_done(run, snapshot.source_url, snapshot, depth=0)
    _alias_redirect(run, snapshot)
    if snapshot.has_crawl_links is not False:
        _schedule_crawl(run, snapshot)
    _schedule_extract(run, snapshot, crawl)


def discover(run: Run, snapshot: PageSnapshot, body: bytes) -> None:
    crawl = _config(run)
    if crawl.get('mode') != 'site':
        return
    if run.status not in (RUN_RUNNING, RUN_PAUSING):
        return
    # Алиас получает глубину редиректа, пока до него не найден более короткий путь.
    parent = CrawlURL.objects.filter(run=run, snapshot=snapshot).order_by(
        'depth', Case(When(skip_reason='redirect', then=1), default=0), 'pk',
    ).first()
    depth = 0 if parent is None else parent.depth + 1
    page = snapshot.final_url or snapshot.source_url
    hrefs, base = iter_hrefs(body, snapshot.content_type, snapshot.charset, page)
    for offset in range(0, len(hrefs), _DISCOVERY_BATCH_SIZE):
        _register_urls(run, crawl, hrefs[offset:offset + _DISCOVERY_BATCH_SIZE], base=base, depth=depth, parent=parent)


def _register_urls(run, crawl, urls, *, base, depth, parent):
    if len(urls) > 1 and _register_batch(run, crawl, urls, base=base, depth=depth, parent=parent):
        return
    for url in urls:
        remember(run, url, base=base, depth=depth, parent=parent)


def _register_batch(run: Run, crawl: dict, hrefs: list[str], *, base: str, depth: int, parent) -> bool:
    # Под блокировкой Run новые адреса и их задачи записываются в одной транзакции.
    links = {}
    for href in hrefs:
        normalized, reason = resolve_url(href, base, crawl.get('drop_query'))
        key = url_key(normalized or href or reason)
        links.setdefault(key, (href, normalized, reason))

    frontier = CrawlURL.objects.filter(run=run)
    existing = dict(frontier.filter(url_key__in=links).values_list('url_key', 'depth'))
    # Короткий путь может открыть дочерние ссылки, а повторное использование снимка — изменить бюджет.
    if any(previous_depth > depth for previous_depth in existing.values()):
        return False
    new_links = {key: link for key, link in links.items() if key not in existing}
    if not new_links:
        return True
    urls = [normalized for _, normalized, _ in new_links.values() if normalized is not None]
    if PageSnapshot.objects.filter(run=run, source_url__in=urls).exists():
        return False

    from .crawl_budget import snapshot_bytes

    counts = frontier.aggregate(total=Count('pk'), used=Count('pk', filter=Q(state__in=_COUNTING)))
    frontier_size = counts['total']
    used = counts['used']
    total_bytes = snapshot_bytes(run)
    capacity = int(crawl.get('frontier_cap') or 1)
    pending_state = CRAWL_ACTIVE if run.status == RUN_RUNNING else CRAWL_QUEUED
    rows = []
    for key, (href, normalized, reason) in new_links.items():
        if frontier_size >= capacity:
            _stop(run, 'frontier')
            break
        if normalized is not None:
            reason = _link_rejection(crawl, normalized, depth)
            if not reason and _budget_closed(run, crawl, usage=(used, frontier_size, total_bytes)):
                reason = run.stop_reason or 'limit'
        if not reason:
            used += 1
        frontier_size += 1
        url = normalized or href or reason
        rows.append(CrawlURL(
            run=run, url_key=key, depth=depth, parent=parent,
            original_url=(url if reason else href or url)[:2000],
            normalized_url=url[:2000] if reason else url,
            state=CRAWL_SKIPPED if reason else pending_state, skip_reason=reason or '',
        ))

    CrawlURL.objects.bulk_create(rows)
    tasks = {
        f'fetch:{row.url_key}': _fetch_defaults(row) for row in rows if row.state == CRAWL_ACTIVE
    }
    if tasks:
        existing_tasks = set(Task.objects.filter(run=run, idempotency_key__in=tasks).values_list('idempotency_key', flat=True))
        Task.objects.bulk_create([
            Task(run=run, idempotency_key=key, **defaults)
            for key, defaults in tasks.items() if key not in existing_tasks
        ])
    return True


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
    reason = _admit(run, crawl, normalized, depth)
    if reason is not None:
        return _skip(run, normalized, reason, depth, parent, key)
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
    reason = _link_rejection(crawl, normalized, depth)
    if reason is not None:
        return reason
    if _budget_closed(run, crawl):
        return run.stop_reason or 'limit'
    return None


def _link_rejection(crawl: dict, normalized: str, depth: int) -> str | None:
    if depth > int(crawl.get('max_depth') or 0):
        return 'depth'
    if not host_allowed(normalized, crawl.get('allowed_hosts')):
        return 'host'
    if not rules_match(normalized, crawl.get('visit')):
        return 'visit'
    return None


def _attach_or_fetch(run: Run, row: CrawlURL) -> None:
    crawl = _config(run)
    snapshot = PageSnapshot.objects.filter(run=run, source_url=row.normalized_url).first()
    if snapshot is not None and crawl.get('visit') and snapshot.final_url not in ('', snapshot.source_url):
        # Предпросмотр не применяет visit, а снимок хранит только последний адрес редиректа.
        # Основная загрузка заново проверит все переходы, сохранив попытки предпросмотра.
        key = f'fetch:{row.url_key}'
        Task.objects.filter(run=run, idempotency_key=key, status=TASK_SUCCEEDED).exclude(
            payload__has_key='crawl_public_id',
        ).update(idempotency_key=f'preview:{key}')
        snapshot = None
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
    if snapshot is None or snapshot.has_crawl_links is False or not snapshot.content_path:
        return
    from .snapshot_store import read_snapshot_bytes
    try:
        body = read_snapshot_bytes(snapshot.content_path, snapshot=snapshot)
    except (OSError, ValueError):
        return
    discover(run, snapshot, body)


def _budget_closed(run: Run, crawl: dict, *, usage: tuple[int, int, int] | None = None) -> bool:
    if run.stop_reason in ('time', 'bytes', 'frontier') or run.stop_reason in STOP_FETCH_FAILURES:
        return True
    used, frontier_size, total_bytes = usage if usage is not None else (None, None, None)
    if used is None:
        used = CrawlURL.objects.filter(run=run, state__in=_COUNTING).count()
    if used >= int(crawl.get('max_pages') or 1):
        _stop(run, 'max_pages')
        return True
    if frontier_size is None:
        frontier_size = CrawlURL.objects.filter(run=run).count()
    if frontier_size >= int(crawl.get('frontier_cap') or 1):
        _stop(run, 'frontier')
        return True
    started = run.started_at or timezone.now()
    if timezone.now() > started + timedelta(seconds=int(crawl.get('max_seconds') or 1)):
        _stop(run, 'time')
        return True
    if total_bytes is None:
        from .crawl_budget import snapshot_bytes
        total_bytes = snapshot_bytes(run)
    if total_bytes >= int(crawl.get('max_total_bytes') or 1):
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
        defaults=_fetch_defaults(row),
    )
    row.state = CRAWL_ACTIVE
    row.save(update_fields=['state'])


def _fetch_defaults(row: CrawlURL) -> dict:
    return {
        'kind': TASK_FETCH,
        'status': TASK_PENDING,
        'payload': {'url': row.normalized_url, 'crawl_public_id': str(row.public_id)},
        'fencing_token': 1,
    }


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
    if crawl.get('page_types'):
        matches = matching_page_types(snapshot, crawl)
        if not matches or (len(matches) == 1 and matches[0]['action'] == 'discover'):
            return
    elif not rules_match(snapshot.final_url or snapshot.source_url, crawl.get('extract_on')):
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
