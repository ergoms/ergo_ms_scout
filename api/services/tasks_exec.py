"""Выдача задач, захват с fencing и выполнение одной попытки."""

import logging
import os
from datetime import timedelta

from django.db import transaction
from django.db.models import Q
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
    TASK_CANCELLED,
    TASK_EXTRACT,
    TASK_FAILED,
    TASK_FETCH,
    TASK_LEASED,
    TASK_PENDING,
    TASK_RETRY_WAIT,
    TASK_SUCCEEDED,
)
from .errors import FetchError, ScoutError
from .html_extract import extract_html
from .http_fetch import fetch_html
from .journal import append_event, describe_extract
from .runs import advance_run
from .snapshot_store import read_snapshot_bytes, store_snapshot_bytes

logger = logging.getLogger('celery.module.scout.tasks')


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
    recover_expired_leases()
    if inline_runner():
        for _ in range(40):
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
        Task.objects.filter(pk=task.pk).update(published_at=now)


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
        elif task.kind == TASK_EXTRACT:
            _extract(task, token)
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
    return queryset.order_by('created_at')


def _claim(task_public_id: str, fencing_token: int):
    with transaction.atomic():
        try:
            task = Task.objects.select_for_update().select_related('run').get(public_id=task_public_id)
        except Task.DoesNotExist:
            return None
        if task.fencing_token != fencing_token:
            return None
        if task.status not in (TASK_PENDING, TASK_RETRY_WAIT):
            return None
        if task.status == TASK_RETRY_WAIT and task.next_retry_at and task.next_retry_at > timezone.now():
            return None
        if task.run.status not in CLAIMABLE_RUN_STATUSES:
            return None
        task.fencing_token += 1
        task.status = TASK_LEASED
        task.attempt_count += 1
        task.lease_until = timezone.now() + timedelta(seconds=LEASE_SECONDS)
        task.save(update_fields=['fencing_token', 'status', 'attempt_count', 'lease_until'])
        Attempt.objects.create(task=task, fencing_token=task.fencing_token, status=TASK_LEASED)
        return task, task.fencing_token


def _fetch(task: Task, token: int) -> None:
    limits = task.run.job_revision.limits
    result = fetch_html(
        task.payload['url'],
        timeout_seconds=int(limits['timeout_seconds']),
        max_bytes=int(limits['max_bytes']),
    )
    digest, relative = store_snapshot_bytes(result['body'])

    def write():
        PageSnapshot.objects.get_or_create(
            run=task.run,
            source_url=task.payload['url'],
            content_hash=digest,
            defaults={
                'final_url': result['final_url'],
                'http_status': result['status'],
                'content_path': relative,
                'byte_size': len(result['body']),
                'content_type': result['content_type'],
                'charset': result.get('charset') or '',
            },
        )
        append_event(
            task.run_id,
            f'Страница скачана: HTTP {result["status"]}, {len(result["body"])} байт.',
        )

    _finish(task, token, write)


def _extract(task: Task, token: int) -> None:
    snapshot = PageSnapshot.objects.get(
        public_id=task.payload['snapshot_public_id'],
        run=task.run,
    )
    body = read_snapshot_bytes(snapshot.content_path)
    revision = task.run.job_revision
    rows = extract_html(
        body,
        revision.fields,
        record=revision.record or None,
        content_type=snapshot.content_type,
        charset=snapshot.charset,
    )
    schema = revision.schema_revision
    rules = revision.rules_revision

    def write():
        for row in rows:
            record, created = Record.objects.get_or_create(
                snapshot=snapshot,
                schema_revision=schema,
                rules_revision=rules,
                identity_key=row['identity_key'],
                defaults={'run': task.run, 'values': row['values']},
            )
            if not created:
                continue
            Evidence.objects.bulk_create([
                Evidence(
                    record=record,
                    snapshot=snapshot,
                    source_url=snapshot.source_url,
                    field_path=item['field_path'],
                    locator=item['locator'],
                    raw_value=item['raw_value'],
                    normalized_value=item['normalized_value'],
                    extractor_id=item['extractor_id'],
                    extractor_version=item['extractor_version'],
                )
                for item in row['evidence']
            ])
        for line in describe_extract(
            body,
            revision.fields,
            len(rows),
            status=task.run.status,
            approved=True,
            content_type=snapshot.content_type,
            charset=snapshot.charset,
        ):
            append_event(task.run_id, line)

    _finish(task, token, write)


def _finish(task: Task, token: int, write) -> None:
    with transaction.atomic():
        locked = Task.objects.select_for_update().get(pk=task.pk)
        if locked.fencing_token != token or locked.status != TASK_LEASED:
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
        locked = Task.objects.select_for_update().get(pk=task.pk)
        if locked.fencing_token != token or locked.status != TASK_LEASED:
            return
        locked.error_code = code[:64]
        locked.error_message = text
        if retryable and locked.attempt_count < MAX_ATTEMPTS:
            locked.status = TASK_RETRY_WAIT
            delay = 2 * locked.attempt_count
            locked.next_retry_at = timezone.now() if inline_runner() else timezone.now() + timedelta(seconds=delay)
            locked.published_at = None
            locked.lease_until = None
            if not inline_runner():
                _schedule_retry(locked.run.public_id, delay)
        else:
            locked.status = TASK_FAILED
            locked.lease_until = None
        locked.save()
        Attempt.objects.filter(task=locked, fencing_token=token).update(
            status=locked.status,
            error_code=locked.error_code,
            error_message=text,
            finished_at=timezone.now(),
        )
        if locked.status == TASK_FAILED:
            append_event(locked.run_id, f'Шаг {locked.kind} не выполнен: {text}')
        advance_run(locked.run)


def _schedule_retry(run_public_id, delay: int) -> None:
    run_id = str(run_public_id)

    def _go():
        from ..tasks import dispatch_scout_pending

        dispatch_scout_pending.apply_async(args=[run_id], countdown=max(delay, 1))

    transaction.on_commit(_go)
