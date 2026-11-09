"""Выдача задач, захват с fencing и выполнение одной попытки."""

import hashlib
import logging
import math
import os
from datetime import timedelta

from django.db import transaction
from django.db.models import Case, F, Q, When, Window
from django.db.models.functions import RowNumber
from django.utils import timezone

from ..models import Attempt, Evidence, PageSnapshot, Record, Run, Task
from .constants import (
    CLAIMABLE_RUN_STATUSES,
    LEASE_SECONDS,
    MAX_ATTEMPTS,
    REPUBLISH_AFTER_SECONDS,
    RUN_CANCELLING,
    RUN_CANCELLED,
    RUN_COMPLETED,
    RUN_COMPLETED_WITH_ERRORS,
    RUN_FAILED,
    RUN_RUNNING,
    TASK_CANCELLED,
    TASK_CRAWL,
    TASK_EXTRACT,
    TASK_FAILED,
    TASK_FETCH,
    TASK_LEASED,
    TASK_PENDING,
    TASK_RETRY_WAIT,
    TASK_SITEMAP,
    TASK_SUCCEEDED,
)
from .errors import FetchError, ScoutError
from .html_extract import extract_html
from .identity_records import store_identity_rows
from .product_identity import identity_extract_options
from .page_types import ambiguous_type_error, effective_fields, matching_page_types
from .public_http import fetch_public_html, fetch_public_xml, fetch_public_text, public_url_allowed
from .proxy_profiles import PROXY_RETRY_DELAYS
from .fetch_failures import STOP_FETCH_FAILURES
from .journal import append_event, describe_extract
from .runs import advance_run, _finish_if_idle
from .snapshot_store import read_snapshot_bytes, store_snapshot_bytes

logger = logging.getLogger('celery.module.scout.tasks')
DISPATCH_INTERLEAVE_ROUNDS = 1
EXTRACT_WRITE_BATCH_SIZE = 500


def inline_runner() -> bool:
    return os.environ.get('SCOUT_TASK_RUNNER', 'celery') == 'inline'


def schedule_publish(run_public_id) -> None:
    run_id = str(run_public_id)

    def _go():
        publish_ready(run_id)

    connection_in_atomic = transaction.get_connection().in_atomic_block
    if connection_in_atomic:
        transaction.on_commit(_go)
    else:
        _go()


def publish_ready(run_public_id=None) -> None:
    from .scheduled_runs import progress_scheduled_runs

    recover_expired_leases()
    _settle_idle_runs(run_public_id)
    progress_scheduled_runs(run_public_id)
    if inline_runner():
        for _ in range(40):
            progress_scheduled_runs(run_public_id)
            batch = list(_ready(run_public_id)[:20])
            if not batch:
                return
            for task in batch:
                token = task.fencing_token
                Task.objects.filter(pk=task.pk).update(published_at=timezone.now())
                run_one(str(task.public_id), token)
        return

    from ..tasks import execute_scout_task

    now = timezone.now()
    for task in list(_ready(run_public_id)[:20]):
        token = task.fencing_token
        try:
            execute_scout_task.delay(str(task.public_id), token)
        except Exception:
            logger.exception('SCOUT не опубликовал задачу %s', task.public_id)
            continue
        Task.objects.filter(pk=task.pk, fencing_token=token, status__in=(TASK_PENDING, TASK_RETRY_WAIT)).update(
            published_at=now,
        )


def _settle_idle_runs(run_public_id=None) -> None:
    queryset = Run.objects.filter(status=RUN_RUNNING)
    if run_public_id:
        queryset = queryset.filter(public_id=run_public_id)
    for run_id in list(queryset.values_list('pk', flat=True)):
        with transaction.atomic():
            locked = Run.objects.select_for_update().get(pk=run_id)
            if locked.status == RUN_RUNNING:
                from .crawl_budget import reconcile
                reconcile(locked)
                _finish_if_idle(locked)


def recover_expired_leases() -> int:
    now = timezone.now()
    expired = list(
        Task.objects.filter(status=TASK_LEASED, lease_until__lt=now).values_list('pk', 'run_id')
    )
    recovered = 0
    for task_id, run_id in expired:
        with transaction.atomic():
            run = Run.objects.select_for_update().get(pk=run_id)
            locked = Task.objects.select_for_update().get(pk=task_id)
            if locked.status != TASK_LEASED or not locked.lease_until or locked.lease_until >= now:
                continue
            previous = locked.fencing_token
            locked.fencing_token = previous + 1
            locked.published_at = None
            locked.lease_until = None
            if run.status in (
                RUN_CANCELLING,
                RUN_CANCELLED,
                RUN_COMPLETED,
                RUN_COMPLETED_WITH_ERRORS,
                RUN_FAILED,
            ):
                locked.status = TASK_CANCELLED
            else:
                locked.status = TASK_PENDING
            locked.save(update_fields=['status', 'fencing_token', 'published_at', 'lease_until'])
            Attempt.objects.filter(
                task=locked,
                fencing_token=previous,
                finished_at__isnull=True,
            ).update(
                status=TASK_FAILED,
                error_code='lease_lost',
                error_message='Исполнитель не подтвердил попытку',
                finished_at=now,
            )
            advance_run(run)
            recovered += 1
    return recovered


def run_one(task_public_id: str, fencing_token: int) -> bool:
    claimed = _claim(task_public_id, fencing_token)
    if claimed is None:
        return False
    task, token = claimed
    try:
        if task.kind == TASK_FETCH:
            _fetch(task, token)
        elif task.kind == TASK_CRAWL:
            _crawl(task, token)
        elif task.kind == TASK_EXTRACT:
            _extract(task, token)
        elif task.kind == TASK_SITEMAP:
            _load_sitemap(task, token)
        else:
            _fail(task, token, 'unknown_kind', 'Неизвестный вид задачи', retryable=False)
    except FetchError as exc:
        _fail(task, token, exc.code, str(exc), retryable=exc.retryable)
    except ScoutError as exc:
        _fail(task, token, exc.code, str(exc), retryable=False)
    except Exception:
        logger.exception('SCOUT: сбой задачи %s', task_public_id)
        _fail(task, token, 'unexpected', 'Задача завершилась с ошибкой', retryable=True)
    return True


def _ready(run_public_id):
    now = timezone.now()
    stale = now - timedelta(seconds=REPUBLISH_AFTER_SECONDS)
    queryset = Task.objects.filter(
        status__in=(TASK_PENDING, TASK_RETRY_WAIT),
        run__status__in=CLAIMABLE_RUN_STATUSES,
    ).filter(
        Q(next_retry_at__isnull=True) | Q(next_retry_at__lte=now),
    ).filter(
        Q(published_at__isnull=True) | Q(published_at__lt=stale),
    )
    if run_public_id:
        queryset = queryset.filter(run__public_id=run_public_id)
    # Начало пачки чередует загрузку и извлечение, остаток идёт по времени создания.
    return queryset.annotate(
        position_in_kind=Window(
            expression=RowNumber(),
            partition_by=F('kind'),
            order_by=(F('created_at').asc(), F('pk').asc()),
        ),
    ).order_by(
        Case(
            When(
                kind__in=(TASK_FETCH, TASK_EXTRACT),
                position_in_kind__lte=DISPATCH_INTERLEAVE_ROUNDS,
                then=F('position_in_kind'),
            ),
            default=DISPATCH_INTERLEAVE_ROUNDS + 1,
        ),
        'created_at',
        'pk',
    )


def _claim(task_public_id: str, fencing_token: int):
    with transaction.atomic():
        run_id = (
            Task.objects.filter(public_id=task_public_id)
            .values_list('run_id', flat=True)
            .first()
        )
        if run_id is None:
            return None
        run = Run.objects.select_for_update().get(pk=run_id)
        try:
            task = Task.objects.select_for_update().get(public_id=task_public_id)
        except Task.DoesNotExist:
            return None
        if task.fencing_token != fencing_token:
            return None
        if task.status not in (TASK_PENDING, TASK_RETRY_WAIT):
            return None
        if task.next_retry_at and task.next_retry_at > timezone.now():
            return None
        if run.status not in CLAIMABLE_RUN_STATUSES:
            return None
        if task.kind == TASK_FETCH:
            proxy = run.job_revision.proxy_profile_revision_public_id
            limit = 1 if proxy else int((run.job_revision.crawl or {}).get('parallel_fetch') or 2)
            busy = Task.objects.filter(
                run_id=run.pk,
                kind=TASK_FETCH,
                status__in=(TASK_LEASED, TASK_RETRY_WAIT) if proxy else (TASK_LEASED,),
            ).exclude(pk=task.pk).count()
            if busy >= limit:
                task.published_at = None
                task.save(update_fields=['published_at'])
                return None
            from .crawl_budget import admit_fetch
            if not admit_fetch(run, task):
                advance_run(run)
                return None
        elif task.kind == TASK_SITEMAP:
            from .crawl_budget import enforce_deadline

            if enforce_deadline(run):
                advance_run(run)
                return None
            if Task.objects.filter(run=run, kind=TASK_SITEMAP, status=TASK_LEASED).exclude(pk=task.pk).exists():
                task.published_at = None
                task.save(update_fields=['published_at'])
                return None
            from .sitemap_scan import reserve_xml
            reserve_xml(run, task)
        task.fencing_token += 1
        task.status = TASK_LEASED
        task.attempt_count += 1
        task.lease_until = timezone.now() + timedelta(seconds=LEASE_SECONDS)
        task.save(update_fields=['fencing_token', 'status', 'attempt_count', 'lease_until'])
        Attempt.objects.create(task=task, fencing_token=task.fencing_token, status=TASK_LEASED)
        return task, task.fencing_token


def _fetch(task: Task, token: int) -> None:
    limits = task.run.job_revision.limits
    crawl = task.run.job_revision.crawl or {}
    url_allowed = None
    timeout = int(limits['timeout_seconds'])
    if crawl.get('mode') == 'site' and task.run.status != 'exploring' and task.run.started_at:
        remaining = crawl['max_seconds'] - (timezone.now() - task.run.started_at).total_seconds()
        timeout = max(1, min(timeout, math.ceil(remaining)))
    if crawl.get('mode') == 'site':
        from .url_norm import host_allowed, resolve_url, rules_match

        def url_allowed(url):
            normalized, _reason = resolve_url(url, url, crawl.get('drop_query'))
            return bool(normalized and host_allowed(normalized, crawl.get('allowed_hosts'))
                        and (task.run.status == 'exploring' or rules_match(normalized, crawl.get('visit'))))

    fetch = fetch_public_html
    proxy_params = {}
    if revision_id := task.run.job_revision.proxy_profile_revision_public_id:
        from .proxy_http import fetch_proxy_html

        fetch = fetch_proxy_html
        proxy_params = {'revision_id': revision_id, 'owner': task.run.user_public_id}
    result = fetch(
        task.payload['url'],
        timeout_seconds=timeout,
        max_bytes=int(task.payload.get('byte_allowance') or limits['max_bytes']),
        url_allowed=url_allowed,
        **proxy_params,
    )
    digest = hashlib.sha256(result['body']).hexdigest()
    charset = result.get('charset') or ''
    links_present = None
    if crawl.get('mode') == 'site':
        from .page_links import has_crawl_links

        links_present = has_crawl_links(result['body'], result['content_type'], charset, result['final_url'])

    def write():
        # Регистрация ссылки и запись байтов сериализованы с очисткой общего хеша.
        from .retention_locks import resource_lock
        with resource_lock('blob:' + digest):
            from .retention import recover_pending_blob
            recover_pending_blob(digest)
            _digest, relative = store_snapshot_bytes(result['body'])
            write_snapshot(relative)

    def write_snapshot(relative):
        snapshot, _created = PageSnapshot.objects.get_or_create(
            run=task.run,
            source_url=task.payload['url'],
            content_hash=digest,
            defaults={
                'final_url': result['final_url'],
                'http_status': result['status'],
                'content_path': relative,
                'byte_size': len(result['body']),
                'content_type': result['content_type'],
                'charset': charset,
                'has_crawl_links': links_present,
            },
        )
        append_event(
            task.run_id,
            f'Страница скачана: HTTP {result["status"]}, {len(result["body"])} байт.',
        )
        from .frontier import note_fetch
        fresh = Run.objects.get(pk=task.run_id)
        note_fetch(fresh, snapshot)

    _finish(task, token, write)


def _extract(task: Task, token: int) -> None:
    snapshot = PageSnapshot.objects.get(
        public_id=task.payload['snapshot_public_id'],
        run=task.run,
    )
    revision = task.run.job_revision
    crawl = revision.crawl or {}
    fields = revision.fields
    has_types = bool(crawl.get('page_types'))
    if has_types:
        matches = matching_page_types(snapshot, crawl)
        if len(matches) > 1:
            raise ambiguous_type_error(snapshot, matches)
        page_type, = matches
        fields = effective_fields(fields, page_type)
    body = read_snapshot_bytes(snapshot.content_path, snapshot=snapshot)
    rows = extract_html(
        body,
        fields,
        record=revision.record or None,
        content_type=snapshot.content_type,
        charset=snapshot.charset,
        require_match=has_types,
        **identity_extract_options(snapshot, revision),
    )
    schema = revision.schema_revision
    rules = revision.rules_revision

    def write():
        if revision.identity:
            store_identity_rows(snapshot, revision, schema, rules, rows)
        else:
            _store_extracted_rows(snapshot, schema, rules, rows)
        if has_types and not rows:
            append_event(task.run_id, 'Страница подходит под тип извлечения, но ни одна запись не найдена.')
        for line in describe_extract(
            body,
            fields,
            len(rows),
            status=task.run.status,
            approved=True,
            content_type=snapshot.content_type,
            charset=snapshot.charset,
        ):
            append_event(task.run_id, line)

    _finish(task, token, write)


def _store_extracted_rows(snapshot, schema, rules, rows) -> None:
    records = Record.objects.filter(snapshot=snapshot, schema_revision=schema, rules_revision=rules)
    for offset in range(0, len(rows), EXTRACT_WRITE_BATCH_SIZE):
        by_key = {}
        for row in rows[offset:offset + EXTRACT_WRITE_BATCH_SIZE]:
            by_key.setdefault(row['identity_key'], row)
        existing_keys = records.filter(identity_key__in=by_key).values_list('identity_key', flat=True)
        for key in existing_keys:
            del by_key[key]
        if not by_key:
            continue

        Record.objects.bulk_create([
            Record(
                run_id=snapshot.run_id,
                snapshot=snapshot,
                schema_revision=schema,
                rules_revision=rules,
                identity_key=key,
                values=row['values'],
            )
            for key, row in by_key.items()
        ])
        record_ids = dict(records.filter(identity_key__in=by_key).values_list('identity_key', 'pk'))
        Evidence.objects.bulk_create([
            Evidence(
                record_id=record_ids[key],
                snapshot=snapshot,
                source_url=snapshot.source_url,
                field_path=item['field_path'],
                locator=item['locator'],
                raw_value=item['raw_value'],
                normalized_value=item['normalized_value'],
                extractor_id=item['extractor_id'],
                extractor_version=item['extractor_version'],
            )
            for key, row in by_key.items()
            for item in row['evidence']
        ], batch_size=EXTRACT_WRITE_BATCH_SIZE)


def _crawl(task: Task, token: int) -> None:
    snapshot = PageSnapshot.objects.get(
        public_id=task.payload['snapshot_public_id'],
        run=task.run,
    )
    body = read_snapshot_bytes(snapshot.content_path, snapshot=snapshot)

    def write():
        from .frontier import discover
        fresh = Run.objects.get(pk=task.run_id)
        discover(fresh, snapshot, body)

    _finish(task, token, write)


def _load_sitemap(task: Task, token: int) -> None:
    from .sitemap import sitemap_document
    from .sitemap_scan import finish_document, map_allowed
    from .robots import robots_sitemaps

    run = task.run
    is_robots = task.payload.get('source') == 'robots'
    revision = run.job_revision
    crawl = revision.crawl
    remaining = crawl['max_seconds'] - (timezone.now() - run.started_at).total_seconds()
    timeout = max(1, min(int(revision.limits['timeout_seconds']), math.ceil(remaining)))
    byte_limit = task.payload.get('xml_allowance', 0)
    if byte_limit <= 0:
        raise FetchError('sitemap_budget', 'Исчерпан общий бюджет XML-карт', retryable=False)
    fetch = fetch_public_text if is_robots else fetch_public_xml
    target_allowed = public_url_allowed
    proxy_params = {}
    if revision_id := revision.proxy_profile_revision_public_id:
        from .proxy_http import fetch_proxy_xml, fetch_proxy_text, proxy_target_allowed

        fetch = fetch_proxy_text if is_robots else fetch_proxy_xml
        target_allowed = proxy_target_allowed
        proxy_params = {'revision_id': revision_id, 'owner': run.user_public_id}
    try:
        result = fetch(
            task.payload['url'], timeout_seconds=timeout, max_bytes=byte_limit,
            url_allowed=lambda url: map_allowed(run, url), **proxy_params,
        )
    except FetchError as exc:
        label = 'robots.txt' if is_robots else 'карту сайта'
        raise FetchError(exc.code, f'Не удалось загрузить {label}. {exc}', retryable=exc.retryable) from exc
    if is_robots:
        kind, urls = 'index', robots_sitemaps(result['body'])
    else:
        kind, urls = sitemap_document(result['body'])

    def write():
        fresh = Run.objects.get(pk=task.run_id)
        finish_document(fresh, task, result, kind, urls, target_allowed)
        append_event(task.run_id, 'robots.txt прочитан. Карты поставлены в очередь.' if is_robots
                     else 'Карта сайта прочитана. Адреса проверены по правилам обхода.')

    _finish(task, token, write)


def _finish(task: Task, token: int, write) -> None:
    with transaction.atomic():
        run = Run.objects.select_for_update().get(pk=task.run_id)
        locked = Task.objects.select_for_update().get(pk=task.pk)
        if locked.fencing_token != token or locked.status != TASK_LEASED:
            return
        if run.status in (RUN_CANCELLING, RUN_CANCELLED):
            locked.status = TASK_CANCELLED
            locked.lease_until = None
            locked.save(update_fields=['status', 'lease_until'])
            Attempt.objects.filter(task=locked, fencing_token=token).update(
                status=TASK_CANCELLED, finished_at=timezone.now(),
            )
            advance_run(run)
            return
        write()
        locked.status = TASK_SUCCEEDED
        locked.lease_until = None
        locked.error_code = ''
        locked.error_message = ''
        locked.save(update_fields=['status', 'lease_until', 'error_code', 'error_message'])
        Attempt.objects.filter(task=locked, fencing_token=token).update(
            status=TASK_SUCCEEDED,
            finished_at=timezone.now(),
        )
        advance_run(locked.run)


def _fail(task: Task, token: int, code: str, message: str, *, retryable: bool) -> None:
    text = (message or '')[:400]
    with transaction.atomic():
        run = Run.objects.select_for_update().get(pk=task.run_id)
        locked = Task.objects.select_for_update().get(pk=task.pk)
        if locked.fencing_token != token or locked.status != TASK_LEASED:
            return
        locked.error_code = code[:64]
        locked.error_message = text
        if run.status in (RUN_CANCELLING, RUN_CANCELLED):
            locked.status = TASK_CANCELLED
            locked.lease_until = None
        elif code == 'too_large' and locked.payload.get('byte_allowance', limits_max_bytes(run)) < limits_max_bytes(run):
            from .crawl_budget import stop_fetches
            stop_fetches(run, 'bytes')
            locked.status = TASK_CANCELLED
            locked.lease_until = None
            from ..models import CrawlURL
            CrawlURL.objects.filter(run=run, public_id=locked.payload.get('crawl_public_id')).update(
                state='skipped', skip_reason='bytes',
            )
        elif retryable and locked.attempt_count < MAX_ATTEMPTS:
            locked.status = TASK_RETRY_WAIT
            delay = PROXY_RETRY_DELAYS[locked.attempt_count - 1] if code == 'proxy_unavailable' else 2 * locked.attempt_count
            locked.next_retry_at = timezone.now() if inline_runner() else timezone.now() + timedelta(seconds=delay)
            locked.published_at = None
            locked.lease_until = None
            if not inline_runner():
                _schedule_retry(run.public_id, delay)
        else:
            locked.status = TASK_FAILED
            locked.lease_until = None
            if locked.kind in (TASK_FETCH, TASK_SITEMAP) and code in STOP_FETCH_FAILURES:
                from .crawl_budget import stop_fetches

                run.stop_reason = code
                run.save(update_fields=['stop_reason'])
                stop_fetches(run, code)
        locked.save()
        Attempt.objects.filter(task=locked, fencing_token=token).update(
            status=locked.status,
            error_code=locked.error_code,
            error_message=text,
            finished_at=timezone.now(),
        )
        if locked.status == TASK_RETRY_WAIT:
            append_event(locked.run_id, 'Страница пока не записана, повторяем.')
        elif locked.status == TASK_FAILED:
            if locked.kind == TASK_SITEMAP:
                from .sitemap_scan import cancel_pending_maps
                cancel_pending_maps(run)
            append_event(locked.run_id, f'Шаг {locked.kind} не выполнен: {text}')
            if locked.kind == TASK_FETCH:
                from .frontier import mark_failed
                mark_failed(run, (locked.payload or {}).get('url') or '')
        advance_run(locked.run)


def _schedule_retry(run_public_id, delay: int) -> None:
    run_id = str(run_public_id)

    def _go():
        from ..tasks import dispatch_scout_pending

        dispatch_scout_pending.apply_async(args=[run_id], countdown=max(delay, 1))

    transaction.on_commit(_go)


def limits_max_bytes(run) -> int:
    return int(run.job_revision.limits['max_bytes'])
