"""Переходы запуска. Подтверждение привязано к хешу плана."""

import hashlib
import json

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from ..models import (
    ExtractionRulesRevision,
    ExtractionSchemaRevision,
    Job,
    JobRevision,
    Run,
    Task,
)
from .constants import (
    OCCUPYING_STATUSES,
    OPEN_TASK_STATUSES,
    RUN_AWAITING,
    RUN_CANCELLING,
    RUN_CANCELLED,
    RUN_COMPLETED,
    RUN_COMPLETED_WITH_ERRORS,
    RUN_EXPLORING,
    RUN_FAILED,
    RUN_PAUSED,
    RUN_PAUSING,
    RUN_RUNNING,
    TASK_CANCELLED,
    TASK_EXTRACT,
    TASK_FAILED,
    TASK_FETCH,
    TASK_LEASED,
    TASK_PENDING,
    TASK_RETRY_WAIT,
)
from .errors import ScoutError
from .plan import normalize_plan, plan_hash


def create_job(*, user_public_id, title: str, seed_urls, fields) -> Job:
    plan = normalize_plan(seed_urls, fields)
    clean_title = (title or '').strip() or _('Сбор')
    if len(clean_title) > 200:
        raise ScoutError('invalid', _('Название слишком длинное'))
    with transaction.atomic():
        job = Job.objects.create(user_public_id=user_public_id, title=clean_title)
        _create_revision(job, 1, plan)
    return job


def revise_job(*, job: Job, seed_urls, fields) -> JobRevision:
    plan = normalize_plan(seed_urls, fields)
    with transaction.atomic():
        if job.runs.filter(status__in=OCCUPYING_STATUSES).exists():
            raise ScoutError('slot_busy', _('Нельзя менять план, пока сбор не завершён'))
        current = _latest_revision(job)
        number = 1 if current is None else current.revision_number + 1
        return _create_revision(job, number, plan)


def explore_job(*, job: Job, user_public_id) -> Run:
    with transaction.atomic():
        _lock_single_slot()
        busy = Run.objects.select_for_update().filter(status__in=OCCUPYING_STATUSES)
        if busy.exists():
            raise ScoutError('slot_busy', _('Уже выполняется другой сбор'))
        revision = _latest_revision(job)
        if revision is None:
            raise ScoutError('invalid', _('У задания нет плана'))
        run = Run.objects.create(
            job_revision=revision,
            user_public_id=user_public_id,
            status=RUN_EXPLORING,
            started_at=timezone.now(),
        )
        for url in revision.seed_urls:
            Task.objects.create(
                run=run,
                kind=TASK_FETCH,
                status=TASK_PENDING,
                idempotency_key=f'fetch:{url}',
                payload={'url': url},
                fencing_token=1,
            )
    return run


def approve_run(*, run: Run, user_public_id) -> Run:
    with transaction.atomic():
        locked = Run.objects.select_for_update().get(pk=run.pk)
        if locked.status != RUN_AWAITING:
            raise ScoutError('invalid_state', _('Подтвердить можно план, который ждёт проверки'))
        revision = JobRevision.objects.select_for_update().get(pk=locked.job_revision_id)
        if revision.approved_plan_hash and revision.approved_plan_hash != revision.plan_hash:
            raise ScoutError('plan_changed', _('План изменился, нужно новое подтверждение'))
        revision.approved_plan_hash = revision.plan_hash
        revision.approved_at = timezone.now()
        revision.approved_by_public_id = user_public_id
        revision.save(update_fields=['approved_plan_hash', 'approved_at', 'approved_by_public_id'])
    return Run.objects.get(pk=run.pk)


def start_run(*, run: Run) -> Run:
    with transaction.atomic():
        locked = Run.objects.select_for_update().select_related('job_revision').get(pk=run.pk)
        if locked.status != RUN_AWAITING:
            raise ScoutError('invalid_state', _('Запуск доступен после подтверждения'))
        revision = locked.job_revision
        if not revision.approved_plan_hash or revision.approved_plan_hash != revision.plan_hash:
            raise ScoutError('approval_required', _('Сбор без подтверждения этого плана запрещён'))
        locked.status = RUN_RUNNING
        locked.save(update_fields=['status'])
        _ensure_extract_tasks(locked)
        _finish_if_idle(locked)
    return Run.objects.get(pk=run.pk)


def pause_run(*, run: Run) -> Run:
    with transaction.atomic():
        locked = Run.objects.select_for_update().get(pk=run.pk)
        if locked.status != RUN_RUNNING:
            raise ScoutError('invalid_state', _('Пауза доступна во время сбора'))
        locked.status = RUN_PAUSING
        locked.save(update_fields=['status'])
        advance_run(locked)
    return Run.objects.get(pk=run.pk)


def cancel_run(*, run: Run) -> Run:
    with transaction.atomic():
        locked = Run.objects.select_for_update().get(pk=run.pk)
        if locked.status not in (
            RUN_EXPLORING,
            RUN_AWAITING,
            RUN_RUNNING,
            RUN_PAUSING,
            RUN_PAUSED,
        ):
            raise ScoutError('invalid_state', _('Этот сбор уже нельзя отменить'))
        locked.status = RUN_CANCELLING
        locked.save(update_fields=['status'])
        locked.tasks.filter(status__in=(TASK_PENDING, TASK_RETRY_WAIT)).update(status=TASK_CANCELLED)
        advance_run(locked)
    return Run.objects.get(pk=run.pk)


def advance_run(run: Run) -> None:
    locked = Run.objects.select_for_update().get(pk=run.pk)
    open_tasks = locked.tasks.filter(status__in=OPEN_TASK_STATUSES)
    if locked.status == RUN_EXPLORING:
        if open_tasks.filter(kind=TASK_FETCH).exists():
            return
        if locked.snapshots.exists():
            locked.status = RUN_AWAITING
        else:
            locked.status = RUN_FAILED
            locked.finished_at = timezone.now()
            locked.error_summary = _summary(locked)
        locked.save()
        return
    if locked.status == RUN_RUNNING:
        _finish_if_idle(locked)
        return
    if locked.status == RUN_PAUSING:
        if locked.tasks.filter(status=TASK_LEASED).exists():
            return
        locked.status = RUN_PAUSED
        locked.save(update_fields=['status'])
        return
    if locked.status == RUN_CANCELLING:
        if locked.tasks.filter(status=TASK_LEASED).exists():
            return
        locked.status = RUN_CANCELLED
        locked.finished_at = timezone.now()
        locked.save(update_fields=['status', 'finished_at'])


def _finish_if_idle(locked: Run) -> None:
    if locked.tasks.filter(status__in=OPEN_TASK_STATUSES).exists():
        return
    failed = locked.tasks.filter(status=TASK_FAILED).exists()
    locked.status = RUN_COMPLETED_WITH_ERRORS if failed else RUN_COMPLETED
    locked.finished_at = timezone.now()
    locked.error_summary = _summary(locked)
    locked.save(update_fields=['status', 'finished_at', 'error_summary'])


def _ensure_extract_tasks(run: Run) -> None:
    revision = run.job_revision
    schema = revision.schema_revision
    rules = revision.rules_revision
    for snapshot in run.snapshots.all():
        key = f'extract:{snapshot.public_id}:{schema.public_id}:{rules.public_id}'
        task, created = Task.objects.get_or_create(
            run=run,
            idempotency_key=key,
            defaults={
                'kind': TASK_EXTRACT,
                'status': TASK_PENDING,
                'payload': {'snapshot_public_id': str(snapshot.public_id)},
                'fencing_token': 1,
            },
        )
        if created:
            continue
        if task.status in (TASK_FAILED, TASK_CANCELLED):
            task.status = TASK_PENDING
            task.published_at = None
            task.error_code = ''
            task.error_message = ''
            task.fencing_token += 1
            task.save()


def _create_revision(job: Job, number: int, plan: dict) -> JobRevision:
    digest = plan_hash(plan)
    revision = JobRevision.objects.create(
        job=job,
        revision_number=number,
        seed_urls=plan['seed_urls'],
        fields=plan['fields'],
        limits=plan['limits'],
        plan_hash=digest,
    )
    schema_fields = [{'name': item['name']} for item in plan['fields']]
    ExtractionSchemaRevision.objects.create(
        job_revision=revision,
        fields=schema_fields,
        content_hash=_sha(schema_fields),
    )
    ExtractionRulesRevision.objects.create(
        job_revision=revision,
        rules=plan['fields'],
        content_hash=_sha(plan['fields']),
    )
    return revision


def _lock_single_slot() -> None:
    from django.db import connection

    if connection.vendor != 'postgresql':
        return
    with connection.cursor() as cursor:
        cursor.execute('SELECT pg_advisory_xact_lock(%s)', [814201])


def _latest_revision(job: Job):
    return job.revisions.order_by('-revision_number').first()


def _summary(run: Run) -> str:
    parts = [
        item
        for item in run.tasks.filter(status=TASK_FAILED).values_list('error_message', flat=True)
        if item
    ]
    return '; '.join(parts)[:1000]


def _sha(payload) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()
