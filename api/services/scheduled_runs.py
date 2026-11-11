"""Захват назначенного дня и продолжение подготовки подтверждённого плана."""

import logging
from datetime import timedelta
from zoneinfo import ZoneInfo

from django.db import DatabaseError, transaction
from django.utils import timezone

from ..models import DailySchedule, Run, ScheduleRevision, ScheduledOccurrence
from .constants import (
    OCCUPYING_STATUSES, OPEN_TASK_STATUSES, RUN_AWAITING, RUN_FAILED, TASK_CANCELLED, TASK_FAILED,
)
from .daily_schedules import disable_schedule, owner_can_schedule
from .errors import ScoutError
from .preview import page_type_preview, preview_rows
from .proxy_profiles import validate_revision
from .runs import _lock_single_slot, explore_revision, start_run
from .schedule_calendar import scheduled_time, START_WINDOW_SECONDS

HISTORY_BATCH_SIZE = 100
logger = logging.getLogger('celery.module.scout.tasks')


def sweep_schedules(*, now=None):
    now = now or timezone.now()
    progress_scheduled_runs()
    candidates = []
    current = ScheduleRevision.objects.filter(enabled=True, effective_until__isnull=True).select_related('schedule')
    for revision in current:
        zone = ZoneInfo(revision.timezone)
        today = now.astimezone(zone).date()
        for day in (today - timedelta(days=1), today):
            if day < revision.first_local_date:
                continue
            due, process_at = scheduled_time(day, revision.local_time, zone)
            if process_at <= now:
                candidates.append((process_at, str(revision.schedule.public_id), revision.pk, day))
    for _process_at, _public_id, revision_id, day in sorted(candidates):
        _process_day(revision_id, day, now)
    _backfill_history(now)
    progress_scheduled_runs()


def _backfill_history(now):
    remaining = HISTORY_BATCH_SIZE
    for revision_id in ScheduleRevision.objects.filter(
        enabled=True, next_local_date__isnull=False,
    ).order_by('effective_from', 'pk').values_list('pk', flat=True):
        while remaining:
            revision = ScheduleRevision.objects.get(pk=revision_id)
            day = revision.next_local_date
            if day is None:
                break
            _due, process_at = scheduled_time(day, revision.local_time, ZoneInfo(revision.timezone))
            if revision.effective_until and process_at >= revision.effective_until:
                ScheduleRevision.objects.filter(pk=revision_id, next_local_date=day).update(next_local_date=None)
                break
            if process_at > now:
                break
            _process_day(revision_id, day, now)
            following = day + timedelta(days=1)
            ScheduleRevision.objects.filter(pk=revision_id, next_local_date=day).update(next_local_date=following)
            remaining -= 1
        if not remaining:
            break


def _process_day(revision_id, day, now):
    schedule_id = ScheduleRevision.objects.values_list('schedule_id', flat=True).get(pk=revision_id)
    with transaction.atomic():
        _lock_single_slot()
        schedule = DailySchedule.objects.select_for_update().select_related('job').get(pk=schedule_id)
        revision = schedule.revisions.select_related('plan_revision').get(pk=revision_id)
        revision.schedule = schedule
        due, process_at = scheduled_time(day, revision.local_time, ZoneInfo(revision.timezone))
        if process_at < revision.effective_from or (
            revision.effective_until and process_at >= revision.effective_until
        ):
            return
        if schedule.occurrences.filter(local_date=day).exists():
            return
        reason = _skip_reason(schedule, revision, due, now)
        run = None
        if not reason:
            reason = _authorization_reason(revision)
            if reason:
                disable_schedule(revision, now)
            else:
                run = explore_revision(revision=revision.plan_revision, user_public_id=schedule.job.user_public_id)
        ScheduledOccurrence.objects.create(
            schedule=schedule, revision=revision, local_date=day, scheduled_at=due, processed_at=now,
            status='preparing' if run else 'skipped', reason=reason, run=run,
        )


def _skip_reason(schedule, revision, due, now):
    if due is None:
        return 'local_time_missing'
    if now >= due + timedelta(seconds=START_WINDOW_SECONDS):
        return 'missed'
    if revision.number != schedule.version:
        return 'superseded'
    if Run.objects.filter(status__in=OCCUPYING_STATUSES).exists():
        return 'slot_busy'
    return ''


def _authorization_reason(revision):
    if not owner_can_schedule(revision.schedule.job.user_public_id):
        return 'permission_revoked'
    plan = revision.plan_revision
    if plan.plan_hash != revision.plan_hash or plan.approved_plan_hash != revision.plan_hash:
        return 'plan_unapproved'
    try:
        validate_revision(plan.proxy_profile_revision_public_id, revision.schedule.job.user_public_id, lock=True)
    except ScoutError:
        return 'proxy_revoked'
    return ''


def progress_scheduled_runs(run_public_id=None):
    active = ScheduledOccurrence.objects.filter(status__in=('preparing', 'running'))
    if run_public_id:
        active = active.filter(run__public_id=run_public_id)
    for occurrence_id in list(active.values_list('pk', flat=True)):
        _progress_occurrence(occurrence_id)


def _progress_occurrence(occurrence_id):
    schedule_id = ScheduledOccurrence.objects.values_list('schedule_id', flat=True).get(pk=occurrence_id)
    with transaction.atomic():
        _lock_single_slot()
        schedule = DailySchedule.objects.select_for_update().select_related('job').get(pk=schedule_id)
        occurrence = ScheduledOccurrence.objects.select_for_update().select_related(
            'revision__plan_revision',
        ).get(pk=occurrence_id)
        if occurrence.status not in ('preparing', 'running'):
            return
        occurrence.revision.schedule = schedule
        run = Run.objects.select_for_update().select_related('job_revision').get(pk=occurrence.run_id)
        if run.status == RUN_AWAITING and occurrence.status == 'preparing':
            _start_prepared_run(occurrence, run)
            run.refresh_from_db()
        if run.status in ('exploring', 'awaiting_confirmation'):
            occurrence.status = 'preparing'
        elif run.status in OCCUPYING_STATUSES:
            occurrence.status = 'running'
        else:
            occurrence.status = run.status
            if run.status == RUN_FAILED and not occurrence.reason:
                failed = run.tasks.filter(status=TASK_FAILED).first()
                occurrence.reason = failed.error_code if failed else 'run_failed'
        occurrence.save(update_fields=['status', 'reason'])


def _start_prepared_run(occurrence, run):
    reason = _authorization_reason(occurrence.revision)
    if reason:
        disable_schedule(occurrence.revision, timezone.now())
    else:
        failed = run.tasks.filter(status=TASK_FAILED).first()
        if failed:
            reason = failed.error_code or 'preview_failed'
        else:
            try:
                if (run.job_revision.crawl or {}).get('page_types'):
                    _rows, diagnostics = page_type_preview(run)
                    if not diagnostics['can_approve']:
                        raise ScoutError('page_type_preview_blocked', 'Проверка типов страниц не пройдена')
                else:
                    preview_rows(run)
                start_run(run=run)
            except ScoutError as exc:
                reason = exc.code
            except DatabaseError:
                raise
            except Exception:
                logger.exception('SCOUT: сбой автоматической подготовки запуска %s', run.public_id)
                reason = 'unexpected'
    if reason:
        run.status = RUN_FAILED
        run.finished_at = timezone.now()
        run.error_summary = f'Автоматическая подготовка остановлена: {reason}'
        run.save(update_fields=['status', 'finished_at', 'error_summary'])
        run.tasks.filter(status__in=OPEN_TASK_STATUSES).update(status=TASK_CANCELLED, lease_until=None)
        occurrence.reason = reason
