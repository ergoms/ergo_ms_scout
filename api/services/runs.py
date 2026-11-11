"""Переходы запуска. Подтверждение привязано к хешу плана."""

import hashlib
import json

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from ..models import (
    ExtractionRulesRevision,
    ExtractionSchemaRevision,
    CrawlURL,
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
    TASK_SITEMAP,
)
from .errors import ScoutError
from .plan import IDENTITY_UNSET, normalize_plan, plan_hash
from .preview import page_type_preview, preview_urls
from .proxy_profiles import validate_revision
from .fetch_failures import STOP_FETCH_FAILURES


def create_job(*, user_public_id, title: str, seed_urls, fields, record=None, crawl=None,
               proxy_profile_revision_public_id=None, identity=IDENTITY_UNSET) -> Job:
    plan = normalize_plan(seed_urls, fields, record=record, crawl=crawl, identity=identity)
    clean_title = _clean_title(title)
    with transaction.atomic():
        _bind_proxy(plan, proxy_profile_revision_public_id, user_public_id)
        job = Job.objects.create(user_public_id=user_public_id, title=clean_title)
        _create_revision(job, 1, plan)
    return job


_KEEP_PROXY = object()


def revise_job(*, job: Job, seed_urls, fields, record=None, title=None, crawl=None,
               proxy_profile_revision_public_id=_KEEP_PROXY, identity=IDENTITY_UNSET) -> JobRevision:
    with transaction.atomic():
        locked_job = Job.objects.select_for_update().get(pk=job.pk)
        if Run.objects.filter(job_revision__job=locked_job, status__in=OCCUPYING_STATUSES).exists():
            raise ScoutError('slot_busy', _('Нельзя менять план, пока сбор не завершён'))
        current = _latest_revision(locked_job)
        plan = normalize_plan(
            seed_urls, fields, limits=current.limits if current else None,
            record=record, crawl=crawl, identity=identity,
        )
        if title is not None:
            locked_job.title = _clean_title(title)
            locked_job.save(update_fields=['title', 'updated_at'])
        if proxy_profile_revision_public_id is _KEEP_PROXY:
            proxy_profile_revision_public_id = current.proxy_profile_revision_public_id if current else None
        _bind_proxy(plan, proxy_profile_revision_public_id, locked_job.user_public_id)
        number = 1 if current is None else current.revision_number + 1
        return _create_revision(locked_job, number, plan)


def explore_job(*, job: Job, user_public_id) -> Run:
    revision = _latest_revision(job)
    if revision is None:
        raise ScoutError('invalid', _('У задания нет плана'))
    return explore_revision(revision=revision, user_public_id=user_public_id)


def explore_revision(*, revision: JobRevision, user_public_id) -> Run:
    with transaction.atomic():
        _lock_single_slot()
        Job.objects.select_for_update().get(pk=revision.job_id)
        if Run.objects.filter(status__in=OCCUPYING_STATUSES).exists():
            raise ScoutError('slot_busy', _('Уже выполняется другой сбор'))
        validate_revision(revision.proxy_profile_revision_public_id, user_public_id, lock=True)
        run = Run.objects.create(
            job_revision=revision,
            user_public_id=user_public_id,
            status=RUN_EXPLORING,
            started_at=timezone.now(),
        )
        for url in preview_urls(revision):
            Task.objects.create(
                run=run,
                kind=TASK_FETCH,
                status=TASK_PENDING,
                idempotency_key=_fetch_key(url),
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
        _require_verified_page_types(locked)
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
        _require_verified_page_types(locked)
        locked.status = RUN_RUNNING
        locked.started_at = timezone.now()
        locked.save(update_fields=['status', 'started_at'])
        crawl = locked.job_revision.crawl or {}
        if sitemap := crawl.get('sitemap_url'):
            Task.objects.create(
                run=locked, kind=TASK_SITEMAP, status=TASK_PENDING,
                idempotency_key='sitemap', payload={'url': sitemap}, fencing_token=1,
            )
        elif crawl.get('sitemap_from_robots'):
            from .robots import robots_url
            Task.objects.create(
                run=locked, kind=TASK_SITEMAP, status=TASK_PENDING,
                idempotency_key='sitemap',
                payload={'url': robots_url(revision.seed_urls[0]), 'source': 'robots', 'depth': -1}, fencing_token=1,
            )
        elif crawl.get('mode') == 'site':
            from .frontier import open_site
            open_site(locked)
        else:
            _ensure_extract_tasks(locked)
        _finish_if_idle(locked)
    return Run.objects.get(pk=run.pk)


def _require_verified_page_types(run: Run) -> None:
    if (run.job_revision.crawl or {}).get('page_types'):
        _rows, diagnostics = page_type_preview(run)
        if not diagnostics['can_approve']:
            raise ScoutError('page_type_preview_blocked', _('Исправьте ошибки и проверьте все извлекающие типы страниц'))


def pause_run(*, run: Run) -> Run:
    with transaction.atomic():
        locked = Run.objects.select_for_update().get(pk=run.pk)
        if locked.status != RUN_RUNNING:
            raise ScoutError('invalid_state', _('Пауза доступна во время сбора'))
        locked.status = RUN_PAUSING
        locked.save(update_fields=['status'])
        advance_run(locked)
    return Run.objects.get(pk=run.pk)


def resume_run(*, run: Run) -> Run:
    with transaction.atomic():
        locked = Run.objects.select_for_update().select_related('job_revision').get(pk=run.pk)
        if locked.status != RUN_PAUSED:
            raise ScoutError('invalid_state', _('Продолжить можно сбор на паузе'))
        revision = locked.job_revision
        if not revision.approved_plan_hash or revision.approved_plan_hash != revision.plan_hash:
            raise ScoutError('approval_required', _('Сбор без подтверждения этого плана запрещён'))
        locked.status = RUN_RUNNING
        locked.save(update_fields=['status'])
        from .crawl_budget import reconcile
        reconcile(locked)
        _finish_if_idle(locked)
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
        CrawlURL.objects.filter(run=locked, state__in=('queued', 'active')).update(
            state='skipped', skip_reason='cancelled',
        )
        advance_run(locked)
    return Run.objects.get(pk=run.pk)


def advance_run(run: Run) -> None:
    locked = Run.objects.select_for_update().get(pk=run.pk)
    open_tasks = locked.tasks.filter(status__in=OPEN_TASK_STATUSES)
    if locked.status == RUN_EXPLORING:
        if open_tasks.filter(kind=TASK_FETCH).exists():
            return
        if locked.stop_reason not in STOP_FETCH_FAILURES and locked.snapshots.exists():
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
    from .frontier import blocks_completion
    if locked.tasks.filter(status__in=OPEN_TASK_STATUSES).exists():
        return
    if blocks_completion(locked):
        return
    failed_tasks = locked.tasks.filter(status=TASK_FAILED)
    failed = failed_tasks.exists()
    if failed and failed_tasks.filter(kind=TASK_SITEMAP).exists():
        locked.status = RUN_FAILED
    elif locked.stop_reason in STOP_FETCH_FAILURES and not (locked.records.exists() or locked.crawl_urls.filter(state='done').exists()):
        locked.status = RUN_FAILED
    else:
        locked.status = RUN_COMPLETED_WITH_ERRORS if failed or locked.stop_reason in STOP_FETCH_FAILURES else RUN_COMPLETED
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


def _bind_proxy(plan, revision_id, owner):
    revision_id = validate_revision(revision_id, owner, lock=True)
    if revision_id is not None:
        from .proxy_http import proxy_target_allowed

        crawl = plan['crawl']
        for url in [*plan['seed_urls'], *crawl.get('preview_urls', [])]:
            proxy_target_allowed(url)
        if sitemap := crawl.get('sitemap_url'):
            proxy_target_allowed(sitemap)
        if crawl.get('sitemap_from_robots'):
            from .robots import robots_url
            proxy_target_allowed(robots_url(plan['seed_urls'][0]))
        plan['proxy_profile_revision_public_id'] = str(revision_id)


def _create_revision(job: Job, number: int, plan: dict) -> JobRevision:
    digest = plan_hash(plan)
    revision = JobRevision.objects.create(
        job=job,
        revision_number=number,
        seed_urls=plan['seed_urls'],
        fields=plan['fields'],
        record=plan['record'] or {},
        limits=plan['limits'],
        crawl=plan['crawl'],
        identity=plan.get('identity'),
        proxy_profile_revision_public_id=plan.get('proxy_profile_revision_public_id'),
        plan_hash=digest,
    )
    schema_fields = [{'name': item['name']} for item in plan['fields']]
    for schema_field, field in zip(schema_fields, plan['fields']):
        if 'conversion' in field:
            schema_field['type'] = field['conversion']['type']
    ExtractionSchemaRevision.objects.create(
        job_revision=revision,
        fields=schema_fields,
        content_hash=_sha(schema_fields),
    )
    rules = plan['fields']
    if page_types := plan['crawl'].get('page_types'):
        rules = {'fields': rules, 'record': plan['record'], 'page_types': page_types}
    ExtractionRulesRevision.objects.create(
        job_revision=revision,
        rules=rules,
        content_hash=_sha(rules),
    )
    return revision


def _clean_title(title: str) -> str:
    clean = (title or '').strip() or _('Сбор')
    if len(clean) > 200:
        raise ScoutError('invalid', _('Название слишком длинное'))
    return clean


def _fetch_key(url: str) -> str:
    digest = hashlib.sha256(url.encode('utf-8')).hexdigest()
    return f'fetch:{digest}'


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
