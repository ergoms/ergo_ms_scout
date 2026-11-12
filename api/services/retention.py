"""Политика хранения, неизменяемый просмотр и возобновляемые порции очистки Scout."""

import hashlib
import json
from collections import Counter
from datetime import timedelta
from uuid import UUID, uuid5, NAMESPACE_URL

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from src.core.utils.media_client import get_media_client

from ..models import (
    Attempt, Evidence, ExportArtifact, Job, PageSnapshot, Record, RetentionCleanup,
    RetentionCleanupItem, RetentionHoldRequest, RetentionLease, RetentionPolicy,
    RetentionPreview, Run, VisualSnapshot,
)
from .constants import OCCUPYING_STATUSES, OPEN_TASK_STATUSES, TERMINAL_RUN_STATUSES
from .errors import ScoutError
from .retention_locks import resource_lock

POLICY_FIELDS = ('automatic_enabled', 'html_days', 'result_days', 'export_days', 'keep_latest_results')
SCOPES = ('html', 'results', 'exports', 'visual_snapshots')
BATCH_SIZE = 200


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def policy_for(owner):
    return RetentionPolicy.objects.get_or_create(user_public_id=owner)[0]


def policy_payload(policy):
    return {'version': policy.version, **{name: getattr(policy, name) for name in POLICY_FIELDS},
            'visual_snapshot_hours': 1}


def update_policy(owner, data):
    if not isinstance(data, dict) or set(data) - {*POLICY_FIELDS, 'expected_version'}:
        raise ScoutError('invalid_retention_policy', 'Неизвестные поля политики')
    with transaction.atomic():
        policy_for(owner)
        policy = RetentionPolicy.objects.select_for_update().get(user_public_id=owner)
        if type(data.get('expected_version')) is not int or data['expected_version'] != policy.version:
            raise ScoutError('retention_policy_changed', 'Политика изменилась; получите её заново')
        values = {name: data.get(name, getattr(policy, name)) for name in POLICY_FIELDS}
        if type(values['automatic_enabled']) is not bool:
            raise ScoutError('invalid_retention_policy', 'automatic_enabled должен быть логическим значением')
        for name in ('html_days', 'result_days', 'export_days'):
            value = values[name]
            if value is not None and (type(value) is not int or not 1 <= value <= 3650):
                raise ScoutError('invalid_retention_policy', 'Срок — от 1 до 3650 суток или null', path=name)
        if type(values['keep_latest_results']) is not int or not 2 <= values['keep_latest_results'] <= 100:
            raise ScoutError('invalid_retention_policy', 'Нужно сохранять от 2 до 100 последних результатов')
        for name in ('html_days', 'export_days'):
            if (values['result_days'] is not None and values[name] is not None
                    and values['result_days'] < values[name]):
                raise ScoutError('invalid_retention_policy', 'Срок результата меньше срока его материалов')
        for name, value in values.items():
            setattr(policy, name, value)
        policy.version += 1
        policy.save()
        return policy


def _uuid(value):
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise ScoutError('invalid', 'Требуется UUID') from None


def hold_payload(run):
    return {'scope': run.retention_hold or None, 'version': run.retention_hold_version}


def change_hold(run, data, key, *, remove=False):
    key = _uuid(key)
    if not isinstance(data, dict) or set(data) - ({'expected_version'} if remove else {'scope', 'expected_version'}):
        raise ScoutError('invalid', 'Неизвестные поля закрепления')
    scope = '' if remove else data.get('scope')
    if scope not in ('', 'results', 'all') or (not remove and not scope):
        raise ScoutError('invalid', 'Закрепление: results или all')
    hashed = digest([str(run.public_id), remove, data])
    with resource_lock(f'run:{run.pk}'), transaction.atomic():
        policy_for(run.user_public_id)
        RetentionPolicy.objects.select_for_update().get(user_public_id=run.user_public_id)
        previous = RetentionHoldRequest.objects.filter(user_public_id=run.user_public_id, key=key).first()
        if previous:
            if previous.request_hash != hashed:
                raise ScoutError('idempotency_conflict', 'Ключ уже использован для другого запроса')
            return previous.response
        locked = Run.objects.select_for_update().get(pk=run.pk)
        if (locked.results_availability == 'purging'
                or locked.snapshots.filter(html_availability='purging').exists()
                or locked.exports.filter(availability='purging').exists()):
            raise ScoutError('cleanup_in_progress', 'Результат уже очищается')
        if not remove and locked.results_availability == 'expired':
            raise ScoutError('run_data_expired', 'Результат уже удалён')
        if locked.status not in TERMINAL_RUN_STATUSES:
            raise ScoutError('run_not_terminal', 'Выполняемый сбор уже защищён')
        if type(data.get('expected_version')) is not int or data['expected_version'] != locked.retention_hold_version:
            raise ScoutError('retention_hold_changed', 'Закрепление изменилось')
        locked.retention_hold = scope
        locked.retention_hold_version += 1
        locked.save(update_fields=['retention_hold', 'retention_hold_version'])
        response = hold_payload(locked)
        response['unavailable'] = []
        if scope == 'all':
            if locked.snapshots.exclude(html_availability='available').exists():
                response['unavailable'].append('html')
            if locked.exports.exclude(availability='available').exists():
                response['unavailable'].append('exports')
        RetentionHoldRequest.objects.create(user_public_id=run.user_public_id, key=key,
                                            request_hash=hashed, response=response)
        return response


def protection(run, scope, policy):
    if run.status not in TERMINAL_RUN_STATUSES or run.finished_at is None:
        return 'active_run'
    if run.tasks.filter(status__in=OPEN_TASK_STATUSES).exists() or Attempt.objects.filter(
        task__run=run, finished_at__isnull=True,
    ).exists():
        return 'open_tasks'
    if run.retention_hold == 'all' or (scope == 'results' and run.retention_hold == 'results'):
        return 'hold'
    if scope == 'results':
        recent = Run.objects.filter(
            job_revision__job_id=run.job_revision.job_id, job_revision__plan_hash=run.job_revision.plan_hash,
            status__in=('completed', 'completed_with_errors'),
        ).order_by('-started_at', '-public_id').values_list('pk', flat=True)[:policy.keep_latest_results]
        if run.pk in recent:
            return 'latest_results'
    if RetentionLease.objects.filter(resource=f'run:{run.pk}', expires_at__gt=timezone.now()).exists():
        return 'operation_in_progress'
    return ''


def run_retention(run):
    policy = policy_for(run.user_public_id)
    states = set(run.snapshots.values_list('html_availability', flat=True))
    return {
        'policy': policy_payload(policy), 'hold': hold_payload(run),
        'results_availability': run.results_availability,
        'html_availability': next(iter(states)) if len(states) == 1 else ('partial' if states else 'available'),
        'eligible_at': {scope: ((run.finished_at + timedelta(days=days)).isoformat()
                               if days is not None and run.finished_at else None)
                        for scope, days in [('html', policy.html_days), ('results', policy.result_days)]},
        'protected': {scope: protection(run, scope, policy) or None for scope in ('html', 'results', 'exports')},
        'retained_counts': run.retained_counts,
    }


def _old(run, days, at):
    return days is not None and run.finished_at is not None and run.finished_at <= at - timedelta(days=days)


def _item(kind, obj, run=None, **extra):
    return {'kind': kind, 'public_id': str(obj.public_id),
            'run_public_id': str(run.public_id) if run else None, **extra}


def build_manifest(owner, policy, selection, at):
    runs = Run.objects.filter(user_public_id=owner).select_related('job_revision').order_by('public_id')
    if selection.get('job_public_id'):
        runs = runs.filter(job_revision__job__public_id=selection['job_public_id'])
    scopes = selection['scopes']
    manifest, excluded, html = [], [], {}
    for run in runs.iterator():
        result_selected = False
        if 'results' in scopes and run.results_availability == 'available' and _old(run, policy.result_days, at):
            reason = protection(run, 'results', policy)
            if reason:
                excluded.append(_item('results', run, run, reason=reason))
            else:
                exports = run.exports.exclude(availability='expired').order_by('public_id')
                export_specs = [_export_spec(item, run) for item in exports]
                spec = _item('results', run, run, records=run.records.count(),
                             evidence=Evidence.objects.filter(record__run=run).count(),
                             eligible_at=(run.finished_at + timedelta(days=policy.result_days)).isoformat(),
                             hold_version=run.retention_hold_version, exports=export_specs)
                spec['executable'] = all(item['executable'] for item in export_specs)
                manifest.append(spec)
                result_selected = True
        if 'exports' in scopes and not result_selected and policy.export_days is not None:
            for artifact in run.exports.filter(
                availability='available', created_at__lte=at - timedelta(days=policy.export_days),
            ).order_by('public_id'):
                reason = protection(run, 'exports', policy)
                if reason:
                    excluded.append(_item('exports', artifact, run, reason=reason))
                else:
                    refs = ExportArtifact.objects.filter(storage_path=artifact.storage_path).exclude(availability='expired')
                    if refs.exclude(run=run).exists() or refs.filter(
                        created_at__gt=at - timedelta(days=policy.export_days),
                    ).exists():
                        excluded.append(_item('exports', artifact, run, reason='shared_blob'))
                    else:
                        spec = _export_spec(artifact, run)
                        spec['eligible_at'] = (artifact.created_at + timedelta(days=policy.export_days)).isoformat()
                        manifest.append(spec)
        if 'html' in scopes and _old(run, policy.html_days, at):
            for snapshot in run.snapshots.filter(html_availability='available').order_by('public_id'):
                reason = protection(run, 'html', policy)
                if reason:
                    excluded.append(_item('html', snapshot, run, reason=reason))
                else:
                    html.setdefault(snapshot.content_hash, []).append(snapshot)
        elif run.status not in TERMINAL_RUN_STATUSES and 'html' in scopes:
            excluded.append(_item('html', run, run, reason='active_run'))
    for content_hash, snapshots in sorted(html.items()):
        refs = list(PageSnapshot.objects.filter(content_hash=content_hash).exclude(html_availability='expired'))
        if {row.pk for row in refs} != {row.pk for row in snapshots}:
            excluded.append(_item('html', snapshots[0], snapshots[0].run, reason='shared_blob'))
            continue
        from .snapshot_store import safe_snapshot_path
        executable, size = True, None
        try:
            expected_path = f'scout/snapshots/{content_hash}.html'
            if any(row.content_path != expected_path for row in refs):
                raise ValueError('snapshot references disagree on path')
            path = safe_snapshot_path(snapshots[0].content_path)
            if path.name != content_hash + '.html':
                raise ValueError('snapshot filename mismatches hash')
            size = path.stat().st_size
        except (OSError, ValueError):
            executable = False
        manifest.append(_item('html', snapshots[0], snapshots[0].run,
                              refs=sorted(str(row.public_id) for row in snapshots), content_hash=content_hash,
                              eligible_at=max(row.run.finished_at + timedelta(days=policy.html_days)
                                              for row in snapshots).isoformat(),
                              path=snapshots[0].content_path, bytes=size, executable=executable))
    if 'visual_snapshots' in scopes:
        for snapshot in VisualSnapshot.objects.filter(
            user_public_id=owner, created_at__lte=at - timedelta(hours=1),
        ).order_by('public_id'):
            if RetentionLease.objects.filter(resource=f'visual:{snapshot.pk}', expires_at__gt=timezone.now()).exists():
                excluded.append(_item('visual_snapshots', snapshot, reason='operation_in_progress'))
            else:
                manifest.append(_item('visual_snapshots', snapshot, bytes=len(snapshot.body), executable=True,
                                      eligible_at=(snapshot.created_at + timedelta(hours=1)).isoformat()))
    manifest.sort(key=lambda value: (value['kind'], value['public_id']))
    return manifest, excluded


def _export_spec(artifact, run):
    size, executable = None, True
    try:
        path = _media_path(artifact)
        size = get_media_client().size(path)
    except FileNotFoundError:
        if artifact.availability == 'purging':
            size = 0
        else:
            executable = False
    except Exception:
        executable = False
    return _item('exports', artifact, run, path=artifact.storage_path, bytes=size, executable=executable,
                 refs=sorted(str(value) for value in ExportArtifact.objects.filter(
                     storage_path=artifact.storage_path,
                 ).exclude(availability='expired').values_list('public_id', flat=True)))


def _media_path(artifact):
    from pathlib import PurePosixPath
    path = PurePosixPath(artifact.storage_path)
    expected = f'scout/exports/{artifact.run.public_id}/'
    if (not artifact.storage_path.startswith(expected) or '\\' in artifact.storage_path
            or '..' in path.parts or path.is_absolute()):
        raise ValueError('invalid export namespace')
    return artifact.storage_path


def preview_summary(manifest, excluded):
    export_files, html_files = {}, {}
    records = evidence = runs = visual = visual_bytes = 0
    for item in manifest:
        if item['kind'] == 'results':
            runs += 1
            records += item['records']
            evidence += item['evidence']
            for artifact in item['exports']:
                export_files[artifact['path']] = artifact['bytes']
        elif item['kind'] == 'exports':
            export_files[item['path']] = item['bytes']
        elif item['kind'] == 'html':
            html_files[item['path']] = item['bytes']
        else:
            visual += 1
            visual_bytes += item['bytes']
    sizes = [*export_files.values(), *html_files.values()]
    executable = all(item.get('executable', True) for item in manifest)
    return {
        'executable': executable,
        'totals': {'runs_with_result_deletion': runs, 'records': records, 'evidence': evidence,
                   'html_files': len(html_files), 'export_files': len(export_files), 'visual_snapshots': visual,
                   'file_bytes_reclaimable': sum(sizes) if all(size is not None for size in sizes) else None,
                   'visual_body_bytes': visual_bytes, 'database_bytes_estimate': None},
        'protected': dict(Counter(item['reason'] for item in excluded)),
        'warnings': [] if executable else ['storage_not_verified'],
    }


def create_preview(owner, data):
    if not isinstance(data, dict) or set(data) - {'policy_version', 'job_public_id', 'scopes'}:
        raise ScoutError('invalid', 'Неизвестные поля просмотра')
    policy = policy_for(owner)
    if type(data.get('policy_version')) is not int or data['policy_version'] != policy.version:
        raise ScoutError('retention_policy_changed', 'Политика изменилась')
    scopes = data.get('scopes')
    if not isinstance(scopes, list) or not scopes or any(scope not in SCOPES for scope in scopes):
        raise ScoutError('invalid', 'Выберите допустимые категории очистки')
    selection = {'scopes': sorted(set(scopes))}
    if data.get('job_public_id'):
        job_id = _uuid(data['job_public_id'])
        from django.http import Http404
        if not Job.objects.filter(public_id=job_id, user_public_id=owner).exists():
            raise Http404
        if 'visual_snapshots' in scopes:
            raise ScoutError('invalid', 'Временные снимки не связаны с заданием')
        selection['job_public_id'] = str(job_id)
    at = timezone.now()
    manifest, excluded = build_manifest(owner, policy, selection, at)
    summary = preview_summary(manifest, excluded)
    return RetentionPreview.objects.create(
        user_public_id=owner, policy_version=policy.version, evaluated_at=at,
        expires_at=at + timedelta(minutes=15), selection=selection,
        manifest=manifest, manifest_hash=digest(manifest), summary={**summary, 'excluded': excluded},
    )


def preview_payload(preview):
    return {'preview_public_id': str(preview.public_id), 'policy_version': preview.policy_version,
            'evaluated_at': preview.evaluated_at.isoformat(), 'expires_at': preview.expires_at.isoformat(),
            'manifest_hash': preview.manifest_hash,
            **{key: value for key, value in preview.summary.items() if key != 'excluded'}}


def public_item(item):
    return {key: value for key, value in item.items() if key in (
        'kind', 'public_id', 'run_public_id', 'reason', 'records', 'evidence', 'bytes', 'executable',
        'eligible_at',
    )} | ({'comparison_will_expire': True, 'export_files': len({row['path'] for row in item['exports']})}
         if item['kind'] == 'results' and 'exports' in item else {})


def accept_cleanup(owner, data, *, trigger='manual'):
    if not isinstance(data, dict) or set(data) != {'preview_public_id', 'manifest_hash', 'idempotency_key'}:
        raise ScoutError('invalid', 'Укажите просмотр, хеш и ключ запроса')
    key = _uuid(data['idempotency_key'])
    hashed = digest(data)
    with transaction.atomic():
        policy_for(owner)
        policy = RetentionPolicy.objects.select_for_update().get(user_public_id=owner)
        previous = RetentionCleanup.objects.filter(user_public_id=owner, idempotency_key=key).first()
        if previous:
            if previous.request_hash != hashed:
                raise ScoutError('idempotency_conflict', 'Ключ уже использован')
            return previous
        from django.shortcuts import get_object_or_404
        preview = get_object_or_404(RetentionPreview, public_id=_uuid(data['preview_public_id']), user_public_id=owner)
        if preview.manifest_hash != data['manifest_hash']:
            raise ScoutError('invalid_manifest', 'Хеш перечня не совпадает')
        if RetentionCleanup.objects.filter(preview=preview).exists():
            raise ScoutError('cleanup_preview_changed', 'Просмотр уже принят; получите его заново')
        if preview.expires_at <= timezone.now():
            raise ScoutError('cleanup_preview_expired', 'Время просмотра истекло')
        if not preview.summary['executable']:
            raise ScoutError('cleanup_preview_not_executable', 'Хранилище не удалось проверить')
        current, _excluded = build_manifest(owner, policy, preview.selection, preview.evaluated_at)
        if policy.version != preview.policy_version or digest(current) != preview.manifest_hash:
            raise ScoutError('cleanup_preview_changed', 'Перечень изменился; получите новый просмотр')
        cleanup = RetentionCleanup.objects.create(
            preview=preview, user_public_id=owner, idempotency_key=key, request_hash=hashed, trigger=trigger,
        )
        RetentionCleanupItem.objects.bulk_create([
            RetentionCleanupItem(cleanup=cleanup, number=index, specification=item)
            for index, item in enumerate(preview.manifest)
        ])
        transaction.on_commit(lambda: enqueue_cleanup(cleanup.public_id))
        return cleanup


def enqueue_cleanup(public_id):
    from ..tasks import execute_retention_cleanup
    try:
        execute_retention_cleanup.apply_async(args=[str(public_id)], queue='scout')
    except Exception:
        # Намерение уже записано. Beat повторит публикацию по БД.
        import logging
        logging.getLogger('modules.scout').warning('Очистка Scout ожидает повторной публикации %s', public_id)


def cleanup_payload(cleanup):
    counts = {name: dict(Counter(row.specification['kind'] for row in cleanup.items.filter(status=status)))
              for name, status in [('deleted', 'deleted'), ('skipped', 'skipped'), ('failed', 'failed'),
                                   ('pending', 'pending')]}
    return {'cleanup_public_id': str(cleanup.public_id), 'status': cleanup.status,
            'trigger': cleanup.trigger, **counts,
            'deleted_bytes': sum(cleanup.items.values_list('deleted_bytes', flat=True)),
            'deleted_rows': sum(cleanup.items.values_list('deleted_rows', flat=True)),
            'errors': list(cleanup.items.exclude(reason='').values_list('reason', flat=True).distinct())}


def process_cleanup(public_id):
    with resource_lock(f'cleanup:{public_id}', wait=False) as acquired:
        if not acquired:
            return False
        cleanup = RetentionCleanup.objects.select_related('preview').filter(public_id=public_id).first()
        if cleanup is None or cleanup.status == 'completed':
            return False
        if Run.objects.filter(status__in=OCCUPYING_STATUSES).exists():
            return False
        cleanup.status = 'running'
        cleanup.save(update_fields=['status'])
        item = cleanup.items.filter(status__in=('pending', 'failed')).filter(
            Q(next_retry_at__isnull=True) | Q(next_retry_at__lte=timezone.now()),
        ).first()
        if item:
            try:
                done, reason = _step(cleanup, item)
                item.status = 'skipped' if reason else ('deleted' if done else 'pending')
                item.reason = reason
                item.next_retry_at = None
            except Exception:
                # Не сохраняем текст исключения: хранилище может включать секреты в ошибку.
                item.status, item.reason = 'failed', 'storage_unavailable'
                item.next_retry_at = timezone.now() + timedelta(seconds=30)
            item.save(update_fields=['status', 'reason', 'next_retry_at'])
        if cleanup.items.filter(status='failed').exists():
            cleanup.status = 'completed_with_errors'
        elif cleanup.items.filter(status='pending').exists():
            cleanup.status = 'running'
        else:
            cleanup.status = 'completed'
            cleanup.finished_at = timezone.now()
        cleanup.save(update_fields=['status', 'finished_at'])
        if cleanup.status == 'running':
            enqueue_cleanup(cleanup.public_id)
        return True


def _step(cleanup, item):
    spec = item.specification
    run = (Run.objects.select_related('job_revision').filter(
        public_id=spec['run_public_id'], user_public_id=cleanup.user_public_id,
    ).first() if spec['run_public_id'] else None)
    if spec['run_public_id'] and run is None:
        return True, 'object_missing'
    resource = f'run:{run.pk}' if run else f"visual:{spec['public_id']}"
    if not run:
        visual = VisualSnapshot.objects.filter(public_id=spec['public_id'], user_public_id=cleanup.user_public_id).first()
        if visual is None:
            return True, ''
        resource = f'visual:{visual.pk}'
    with resource_lock(resource, wait=False) as acquired:
        if not acquired:
            return True, 'operation_in_progress'
        if spec['kind'] == 'html':
            return _purge_html(cleanup, item)
        policy = policy_for(cleanup.user_public_id)
        if run:
            run.refresh_from_db()
        # Начатое намерение доводим до конца даже после правки политики.
        started = (run is not None and spec['kind'] == 'results' and run.results_availability == 'purging')
        if spec['kind'] == 'exports':
            started = ExportArtifact.objects.filter(public_id=spec['public_id'], availability='purging').exists()
        if policy.version != cleanup.preview.policy_version and not started:
            return True, 'policy_changed'
        if run and not started:
            reason = protection(run, spec['kind'], policy)
            if reason:
                return True, reason
        if spec['kind'] == 'exports':
            _purge_export(spec, item)
            return True, ''
        if spec['kind'] == 'results':
            return _purge_results(run, item)
        if RetentionLease.objects.filter(resource=resource, expires_at__gt=timezone.now()).exists():
            return True, 'operation_in_progress'
        visual.delete()
        _progress(item, rows=1)
        return True, ''


def _progress(item, *, rows=0, size=0):
    item.deleted_rows += rows
    item.deleted_bytes += size
    item.save(update_fields=['deleted_rows', 'deleted_bytes'])


def _purge_html(cleanup, item):
    from contextlib import ExitStack
    from .snapshot_store import safe_snapshot_path
    spec = item.specification
    with resource_lock('blob:' + spec['content_hash']):
        refs = list(PageSnapshot.objects.filter(content_hash=spec['content_hash']).exclude(
            html_availability='expired',
        ).select_related('run__job_revision'))
        if not refs:
            return True, ''
        if sorted(str(row.public_id) for row in refs) != spec['refs']:
            return True, 'shared_blob'
        if any(row.content_path != spec['path'] for row in refs):
            raise ValueError('snapshot reference path changed')
        with ExitStack() as stack:
            runs = sorted({row.run_id: row.run for row in refs}.values(), key=lambda run: run.pk)
            for run in runs:
                if run.user_public_id != cleanup.user_public_id:
                    return True, 'shared_blob'
                if not stack.enter_context(resource_lock(f'run:{run.pk}', wait=False)):
                    return True, 'operation_in_progress'
            path = safe_snapshot_path(spec['path'])
            if path.name != spec['content_hash'] + '.html':
                raise ValueError('snapshot filename mismatches hash')
            with transaction.atomic():
                policy, _created = RetentionPolicy.objects.select_for_update().get_or_create(
                    user_public_id=cleanup.user_public_id,
                )
                started = all(row.html_availability == 'purging' for row in refs)
                if policy.version != cleanup.preview.policy_version and not started:
                    return True, 'policy_changed'
                for run in runs:
                    run.refresh_from_db()
                    if reason := protection(run, 'html', policy):
                        return True, reason
                PageSnapshot.objects.filter(pk__in=[row.pk for row in refs]).update(html_availability='purging')
            # Намерение уже в БД. Отсутствие после падения между unlink и update — успех.
            path.unlink(missing_ok=True)
            with transaction.atomic():
                PageSnapshot.objects.filter(pk__in=[row.pk for row in refs]).update(
                    html_availability='expired', purged_at=timezone.now(),
                )
                _progress(item, size=spec['bytes'] or 0)
        return True, ''


def _purge_export(spec, item):
    artifact = ExportArtifact.objects.select_related('run').filter(public_id=spec['public_id']).first()
    if artifact is None or artifact.availability == 'expired':
        return
    path = _media_path(artifact)
    with resource_lock('media:' + path):
        refs = ExportArtifact.objects.filter(storage_path=path).exclude(availability='expired')
        if refs.exclude(run_id=artifact.run_id).exists():
            raise ValueError('shared export outside run')
        if set(str(value) for value in refs.values_list('public_id', flat=True)) - set(spec['refs']):
            raise ValueError('export references changed')
        refs.update(availability='purging')
        client = get_media_client()
        client.delete(path)
        try:
            client.size(path)
        except FileNotFoundError:
            pass
        else:
            raise OSError('export still exists')
        with transaction.atomic():
            refs.update(availability='expired')
            _progress(item, size=spec['bytes'] or 0)


def _purge_results(run, item):
    spec = item.specification
    if run.results_availability == 'expired':
        return True, ''
    if run.results_availability == 'available':
        current = run.exports.exclude(availability='expired').values_list('public_id', flat=True)
        if {str(value) for value in current} - {row['public_id'] for row in spec['exports']}:
            return True, 'exports_changed'
        with transaction.atomic():
            Job.objects.select_for_update().get(pk=run.job_revision.job_id)
            reason = protection(Run.objects.select_related('job_revision').get(pk=run.pk), 'results',
                                policy_for(run.user_public_id))
            if reason:
                return True, reason
            run.retained_counts = {'record_count': run.records.count(),
                                   'evidence_count': Evidence.objects.filter(record__run=run).count(),
                                   'unkeyed_records': run.records.filter(entity_key__isnull=True).count()}
            run.results_availability = 'purging'
            run.save(update_fields=['retained_counts', 'results_availability'])
    remaining = [row for row in spec['exports'] if ExportArtifact.objects.filter(
        public_id=row['public_id'], availability__in=('available', 'purging'),
    ).exists()]
    if remaining:
        for export in remaining[:BATCH_SIZE]:
            _purge_export(export, item)
        return False, ''
    if run.exports.exclude(availability='expired').exists():
        raise ValueError('result manifest omits a pending export')
    ids = list(Evidence.objects.filter(record__run=run).values_list('pk', flat=True)[:BATCH_SIZE])
    if ids:
        with transaction.atomic():
            Evidence.objects.filter(pk__in=ids).delete()
            _progress(item, rows=len(ids))
        return False, ''
    ids = list(run.records.values_list('pk', flat=True)[:BATCH_SIZE])
    if ids:
        with transaction.atomic():
            Record.objects.filter(pk__in=ids).delete()
            _progress(item, rows=len(ids))
        return False, ''
    Run.objects.filter(pk=run.pk).update(results_availability='expired')
    return True, ''


def recover_pending_blob(content_hash):
    """Вызывается писателем под блокировкой хеша перед появлением новых ссылок."""
    refs = PageSnapshot.objects.filter(content_hash=content_hash, html_availability='purging')
    if not refs.exists():
        return
    if refs.exclude(content_path=f'scout/snapshots/{content_hash}.html').exists():
        raise OSError('invalid deletion reference')
    if PageSnapshot.objects.filter(content_hash=content_hash).exclude(
        html_availability__in=('expired', 'purging'),
    ).exists():
        raise OSError('deletion intent has new references')
    pending_ids = sorted(str(value) for value in refs.values_list('public_id', flat=True))
    candidates = RetentionCleanupItem.objects.filter(
        specification__kind='html', specification__content_hash=content_hash,
    ).exclude(status__in=('deleted', 'skipped')).order_by('pk')
    item = next((row for row in candidates if row.specification['refs'] == pending_ids), None)
    if item is None:
        raise OSError('unowned deletion intent')
    from .snapshot_store import safe_snapshot_path
    safe_snapshot_path(item.specification['path']).unlink(missing_ok=True)
    with transaction.atomic():
        refs.update(html_availability='expired', purged_at=timezone.now())
        _progress(item, size=item.specification['bytes'] or 0)
        item.status, item.reason = 'deleted', ''
        item.save(update_fields=['status', 'reason'])


def sweep_retention():
    RetentionLease.objects.filter(expires_at__lte=timezone.now()).delete()
    for policy in RetentionPolicy.objects.filter(automatic_enabled=True).iterator():
        with resource_lock(f'automatic:{policy.user_public_id}', wait=False) as acquired:
            if not acquired:
                continue
            policy.refresh_from_db()
            day = timezone.now().date()
            if not policy.automatic_enabled or policy.last_automatic_date == day:
                continue
            try:
                with transaction.atomic():
                    preview = create_preview(policy.user_public_id, {
                        'policy_version': policy.version, 'scopes': list(SCOPES),
                    })
                    claimed = RetentionPolicy.objects.filter(
                        pk=policy.pk, version=preview.policy_version, automatic_enabled=True,
                    ).update(last_automatic_date=day)
                    if not claimed:
                        preview.delete()
                        continue
                    if preview.summary['executable']:
                        accept_cleanup(policy.user_public_id, {
                            'preview_public_id': str(preview.public_id), 'manifest_hash': preview.manifest_hash,
                            'idempotency_key': str(uuid5(NAMESPACE_URL, f'scout-retention:{policy.user_public_id}:{day}')),
                        }, trigger='automatic')
            except ScoutError as exc:
                if exc.code not in ('retention_policy_changed', 'cleanup_preview_changed'):
                    raise
    for cleanup_id in RetentionCleanup.objects.exclude(status='completed').values_list('public_id', flat=True):
        enqueue_cleanup(cleanup_id)
