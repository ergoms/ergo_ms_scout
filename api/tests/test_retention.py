"""Очистка не подменяет результат пустым и восстанавливает сохранённое намерение."""

import os
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from ..models import (
    Evidence, ExportArtifact, PageSnapshot, RetentionCleanup, RetentionCleanupItem,
    RetentionLease, RetentionPolicy, RetentionPreview, Run, Task, VisualSnapshot,
)
from ..services.retention import build_manifest, digest, process_cleanup, sweep_retention, update_policy
from ..services.retention_locks import read_results, resource_lock
from ..services.snapshot_store import safe_snapshot_path
from ..services.tasks_exec import publish_ready
from .fixtures.ext002_catalog import ComparisonCatalog, comparison_plan


class RetentionTests(TestCase):
    def setUp(self):
        self.site = ComparisonCatalog()
        Thread(target=self.site.serve_forever, daemon=True).start()
        self.addCleanup(self.site.server_close)
        self.addCleanup(self.site.shutdown)
        self.files = self.enterContext(TemporaryDirectory(prefix='scout-retention-test-'))
        self.enterContext(patch('modules.scout.api.services.snapshot_store.CACHE_DIR', Path(self.files)))
        self.enterContext(patch.dict(os.environ, {
            'SCOUT_TASK_RUNNER': 'inline',
            'SCOUT_TRUSTED_FETCH_ENDPOINTS': f'127.0.0.1:{self.site.server_port}',
        }))
        self.enqueue = self.enterContext(patch('modules.scout.api.services.retention.enqueue_cleanup'))
        self.user = get_user_model().objects.create_superuser(username='scout-retention')
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        response = self.client.post('/api/scout/jobs/', comparison_plan(self.site.origin), format='json')
        self.assertEqual(response.status_code, 201, response.data)
        self.job_id = response.data['public_id']

    def collect(self):
        response = self.client.post(f'/api/scout/jobs/{self.job_id}/explore/')
        self.assertEqual(response.status_code, 202, response.data)
        uid = response.data['public_id']
        publish_ready(uid)
        path = f'/api/scout/runs/{uid}/'
        self.assertEqual(self.client.post(path + 'approve/').status_code, 200)
        self.assertEqual(self.client.post(path + 'start/').status_code, 202)
        publish_ready(uid)
        run = Run.objects.get(public_id=uid)
        self.assertEqual(run.status, 'completed')
        Run.objects.filter(pk=run.pk).update(finished_at=timezone.now() - timedelta(days=400))
        run.refresh_from_db()
        return run

    def preview(self, scopes, **kwargs):
        result = self.client.post('/api/scout/retention/previews/', {
            'policy_version': 1, 'job_public_id': self.job_id, 'scopes': scopes, **kwargs,
        }, format='json')
        self.assertEqual(result.status_code, 201, result.data)
        return result.data

    def accept(self, preview, **kwargs):
        body = {'preview_public_id': preview['preview_public_id'], 'manifest_hash': preview['manifest_hash'],
                'idempotency_key': str(uuid4()), **kwargs}
        response = self.client.post('/api/scout/retention/cleanups/', body, format='json')
        self.assertEqual(response.status_code, 202, response.data)
        return RetentionCleanup.objects.get(public_id=response.data['cleanup_public_id']), body

    def finish(self, cleanup):
        for _ in range(100):
            process_cleanup(cleanup.public_id)
            cleanup.refresh_from_db()
            if cleanup.status == 'completed':
                return
        self.fail(f'Cleanup did not finish: {cleanup.status}')

    def compare(self, base, target):
        return self.client.get(f'/api/scout/runs/{target.public_id}/compare/', {
            'base_run_public_id': str(base.public_id), 'field': 'price',
        })

    def test_html_preview_preserves_all_evidence_and_comparison(self):
        base = self.collect()
        self.site.add_product_and_change_price()
        target = self.collect()
        before = self.compare(base, target).data
        evidence = Evidence.objects.count()
        records = list(base.records.values_list('public_id', flat=True))
        paths = {safe_snapshot_path(value) for value in PageSnapshot.objects.values_list('content_path', flat=True)}
        preview = self.preview(['html'])
        self.assertEqual(preview['totals']['html_files'], len(paths))
        self.assertEqual(preview['totals']['file_bytes_reclaimable'], sum(path.stat().st_size for path in paths))
        self.assertTrue(all(path.exists() for path in paths))
        cleanup, _body = self.accept(preview)
        self.finish(cleanup)
        self.assertTrue(all(not path.exists() for path in paths))
        self.assertEqual(list(base.records.values_list('public_id', flat=True)), records)
        self.assertEqual(Evidence.objects.count(), evidence)
        self.assertEqual(set(PageSnapshot.objects.values_list('html_availability', flat=True)), {'expired'})
        after = self.compare(base, target).data
        for response in (before, after):
            for change in response['changes']:
                for side in ('before', 'after'):
                    if change[side]:
                        for item in change[side]['evidence']:
                            item.pop('html_availability')
        self.assertEqual(before, after)
        self.assertEqual(self.client.get(f'/api/scout/runs/{base.public_id}/records/').status_code, 200)

    def test_active_paused_and_awaiting_runs_are_protected(self):
        run = self.collect()
        for status in ('running', 'paused', 'pausing', 'awaiting_confirmation', 'exploring', 'cancelling'):
            Run.objects.filter(pk=run.pk).update(status=status)
            preview = self.preview(['html', 'results'])
            self.assertEqual(preview['totals']['html_files'], 0)
            self.assertIn('active_run', preview['protected'])

    def test_terminal_run_with_open_task_is_protected(self):
        run = self.collect()
        Task.objects.create(run=run, kind='extract', status='retry_wait', idempotency_key='open')
        self.assertIn('open_tasks', self.preview(['html'])['protected'])

    def test_latest_two_hold_and_expired_result_response(self):
        base, second, third = self.collect(), self.collect(), self.collect()
        preview = self.preview(['results'])
        self.assertEqual(preview['totals']['runs_with_result_deletion'], 1)
        self.assertEqual(preview['protected']['latest_results'], 2)
        response = self.client.get(f"/api/scout/retention/previews/{preview['preview_public_id']}/items/")
        self.assertEqual(response.status_code, 200)
        items = {item['public_id']: item for item in response.data['items']}
        self.assertTrue(items[str(base.public_id)]['comparison_will_expire'])
        self.assertEqual(items[str(base.public_id)]['export_files'], 0)
        for run in (second, third):
            item = items[str(run.public_id)]
            self.assertEqual(item['reason'], 'latest_results')
            self.assertNotIn('comparison_will_expire', item)
        cleanup, body = self.accept(preview)
        replay = self.client.post('/api/scout/retention/cleanups/', body, format='json')
        self.assertEqual(replay.data['cleanup_public_id'], str(cleanup.public_id))
        self.finish(cleanup)
        base.refresh_from_db()
        self.assertEqual(base.results_availability, 'expired')
        self.assertEqual(base.records.count(), 0)
        response = self.client.get(f'/api/scout/runs/{base.public_id}/records/')
        self.assertEqual((response.status_code, response.data['code']), (410, 'run_data_expired'))
        self.assertEqual(self.compare(base, third).status_code, 410)
        summary = self.client.get(f'/api/scout/runs/{base.public_id}/', {'summary': 1}).data
        self.assertEqual(summary['record_count'], 3)
        self.assertEqual(second.records.count(), 3)
        self.assertEqual(third.records.count(), 3)

    def test_hold_created_after_acceptance_skips_cleanup(self):
        base = self.collect()
        self.collect()
        self.collect()
        cleanup, _body = self.accept(self.preview(['results']))
        url = f'/api/scout/runs/{base.public_id}/retention/hold/'
        key = str(uuid4())
        response = self.client.put(url, {'scope': 'results', 'expected_version': 1}, format='json',
                                   HTTP_IDEMPOTENCY_KEY=key)
        self.assertEqual(response.status_code, 200, response.data)
        replay = self.client.put(url, {'scope': 'results', 'expected_version': 1}, format='json',
                                 HTTP_IDEMPOTENCY_KEY=key)
        self.assertEqual(response.data, replay.data)
        self.finish(cleanup)
        self.assertEqual(cleanup.items.get().reason, 'hold')
        self.assertEqual(base.records.count(), 3)

    def test_new_hold_invalidates_preview_and_all_keeps_html(self):
        run = self.collect()
        preview = self.preview(['html'])
        run.retention_hold = 'all'
        run.save(update_fields=['retention_hold'])
        response = self.client.post('/api/scout/retention/cleanups/', {
            'preview_public_id': preview['preview_public_id'], 'manifest_hash': preview['manifest_hash'],
            'idempotency_key': str(uuid4()),
        }, format='json')
        self.assertEqual((response.status_code, response.data['code']), (409, 'cleanup_preview_changed'))
        self.assertEqual(self.preview(['html'])['totals']['html_files'], 0)

    def test_read_lease_prevents_selection(self):
        run = self.collect()
        with read_results(run):
            self.assertIn('operation_in_progress', self.preview(['html'])['protected'])
        self.assertEqual(RetentionLease.objects.count(), 0)

    def test_shared_html_with_active_other_run_is_not_removed(self):
        self.collect()
        other = self.collect()
        Run.objects.filter(pk=other.pk).update(status='paused')
        preview = self.preview(['html'])
        self.assertEqual(preview['totals']['html_files'], 0)
        self.assertIn('shared_blob', preview['protected'])

    def test_recovery_after_unlink_before_database_update(self):
        self.collect()
        cleanup, _body = self.accept(self.preview(['html']))
        real = Path.unlink
        def crash(path, **kwargs):
            real(path, **kwargs)
            raise OSError('simulated worker failure')
        with patch.object(Path, 'unlink', crash):
            process_cleanup(cleanup.public_id)
        cleanup.refresh_from_db()
        self.assertEqual(cleanup.status, 'completed_with_errors')
        self.assertTrue(PageSnapshot.objects.filter(html_availability='purging').exists())
        run = Run.objects.get(pk=PageSnapshot.objects.filter(html_availability='purging').first().run_id)
        response = self.client.put(f'/api/scout/runs/{run.public_id}/retention/hold/',
                                   {'scope':'all', 'expected_version':1}, format='json',
                                   HTTP_IDEMPOTENCY_KEY=str(uuid4()))
        self.assertEqual(response.data['code'], 'cleanup_in_progress')
        RetentionCleanupItem.objects.filter(cleanup=cleanup).update(next_retry_at=None)
        self.finish(cleanup)
        self.assertFalse(PageSnapshot.objects.exclude(html_availability='expired').exists())
        count = PageSnapshot.objects.count()
        self.assertFalse(process_cleanup(cleanup.public_id))
        self.assertEqual(PageSnapshot.objects.count(), count)

    def test_policy_versions_validation_and_owner_isolation(self):
        defaults = self.client.get('/api/scout/retention/policy/').data
        self.assertFalse(defaults['automatic_enabled'])
        for value in (0, -1, True, 1.5, 3651):
            result = self.client.patch('/api/scout/retention/policy/', {
                'expected_version': 1, 'html_days': value,
            }, format='json')
            self.assertEqual(result.status_code, 400)
        result = self.client.patch('/api/scout/retention/policy/', {
            'expected_version': 1, 'html_days': None,
        }, format='json')
        self.assertEqual(result.data['version'], 2)
        result = self.client.patch('/api/scout/retention/policy/', {
            'expected_version': 1, 'export_days': 10,
        }, format='json')
        self.assertEqual(result.status_code, 409)
        run = self.collect()
        other = get_user_model().objects.create_superuser(username='scout-retention-other')
        self.client.force_authenticate(other)
        self.assertEqual(self.client.get(f'/api/scout/runs/{run.public_id}/retention/').status_code, 404)

    def test_preview_cursor_is_bound_and_does_not_leak_paths(self):
        self.collect()
        preview = self.preview(['html'])
        url = f"/api/scout/retention/previews/{preview['preview_public_id']}/items/"
        response = self.client.get(url, {'limit': 1})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('path', str(response.data))
        self.assertEqual(self.client.get(url, {'cursor': 'bad'}).status_code, 400)
        second = self.preview(['html'])
        other_url = f"/api/scout/retention/previews/{second['preview_public_id']}/items/"
        self.assertEqual(self.client.get(other_url, {'cursor': response.data['next_cursor']}).status_code, 400)

    def test_visual_expiry_selection_does_not_delete_before_execution(self):
        snapshot = VisualSnapshot.objects.create(user_public_id=self.user.public_id, source_url='https://example.com',
                                                 body=b'<body>test</body>')
        VisualSnapshot.objects.filter(pk=snapshot.pk).update(created_at=timezone.now()-timedelta(hours=2))
        preview = self.preview(['visual_snapshots'], job_public_id=None)
        self.assertEqual(preview['totals']['visual_snapshots'], 1)
        self.assertTrue(VisualSnapshot.objects.filter(pk=snapshot.pk).exists())
        cleanup, _body = self.accept(preview)
        self.finish(cleanup)
        self.assertFalse(VisualSnapshot.objects.filter(pk=snapshot.pk).exists())

    def test_export_is_removed_and_can_be_rebuilt_after_html_cleanup(self):
        from src.core.utils.media_client.local import LocalMediaClient
        run = self.collect()
        media = LocalMediaClient(str(Path(self.files) / 'media'))
        with patch('modules.scout.api.services.exporting.get_media_client', return_value=media), patch(
            'modules.scout.api.services.retention.get_media_client', return_value=media,
        ):
            url = f'/api/scout/runs/{run.public_id}/export/'
            self.assertEqual(self.client.post(url, {'kind': 'json'}, format='json').status_code, 200)
            first = ExportArtifact.objects.get()
            data = media.read_bytes(first.storage_path)
            ExportArtifact.objects.filter(pk=first.pk).update(created_at=timezone.now()-timedelta(days=8))
            cleanup, _body = self.accept(self.preview(['html', 'exports']))
            self.finish(cleanup)
            self.assertFalse(media.exists(first.storage_path))
            self.assertEqual(self.client.post(url, {'kind': 'json'}, format='json').status_code, 200)
            second = ExportArtifact.objects.latest('pk')
            self.assertNotEqual(first.storage_path, second.storage_path)
            import json
            before, after = json.loads(data), json.loads(media.read_bytes(second.storage_path))
            for rows in (before, after):
                for row in rows:
                    for item in row['evidence']:
                        item.pop('html_availability')
            self.assertEqual(before, after)

    def test_expired_preview_and_policy_change_do_not_delete(self):
        self.collect()
        preview = self.preview(['html'])
        RetentionPreview.objects.filter(public_id=preview['preview_public_id']).update(expires_at=timezone.now())
        response = self.client.post('/api/scout/retention/cleanups/', {
            'preview_public_id': preview['preview_public_id'], 'manifest_hash': preview['manifest_hash'],
            'idempotency_key': str(uuid4()),
        }, format='json')
        self.assertEqual(response.data['code'], 'cleanup_preview_expired')
        cleanup, _body = self.accept(self.preview(['html']))
        self.client.patch('/api/scout/retention/policy/', {'expected_version':1, 'html_days':60}, format='json')
        self.finish(cleanup)
        self.assertTrue(all(item.reason == 'policy_changed' for item in cleanup.items.all()))
        self.assertFalse(PageSnapshot.objects.exclude(html_availability='available').exists())

    def test_automatic_daily_cleanup_is_disabled_then_created_once(self):
        self.collect()
        self.client.get('/api/scout/retention/policy/')
        sweep_retention()
        self.assertEqual(RetentionCleanup.objects.count(), 0)
        RetentionPolicy.objects.filter(user_public_id=self.user.public_id).update(automatic_enabled=True)
        sweep_retention()
        sweep_retention()
        self.assertEqual(RetentionCleanup.objects.count(), 1)
        self.assertEqual(RetentionCleanup.objects.get().trigger, 'automatic')

    def test_automatic_cleanup_saves_one_non_executable_preview_per_day(self):
        run = self.collect()
        path = safe_snapshot_path(run.snapshots.first().content_path)
        body = path.read_bytes()
        path.unlink()
        RetentionPolicy.objects.create(user_public_id=self.user.public_id, automatic_enabled=True)
        now = timezone.now()
        with patch('modules.scout.api.services.retention.timezone.now', return_value=now):
            sweep_retention()
            sweep_retention()
            self.assertFalse(RetentionPreview.objects.get().summary['executable'])
            self.assertFalse(RetentionCleanup.objects.exists())
            path.write_bytes(body)
            sweep_retention()
            self.assertEqual(RetentionPreview.objects.count(), 1)
        with patch('modules.scout.api.services.retention.timezone.now', return_value=now + timedelta(days=1)):
            sweep_retention()
            self.assertEqual(RetentionPreview.objects.count(), 2)
            self.assertEqual(RetentionCleanup.objects.get().trigger, 'automatic')

    def test_changed_policy_during_automatic_preview_does_not_consume_day(self):
        run = self.collect()
        path = safe_snapshot_path(run.snapshots.first().content_path)
        path.unlink()
        policy = RetentionPolicy.objects.create(user_public_id=self.user.public_id, automatic_enabled=True)

        def change_policy_after_scanning(*args):
            result = build_manifest(*args)
            update_policy(self.user.public_id, {'expected_version': 1, 'html_days': None})
            return result

        now = timezone.now()
        with patch('modules.scout.api.services.retention.timezone.now', return_value=now):
            with patch('modules.scout.api.services.retention.build_manifest', change_policy_after_scanning):
                sweep_retention()
            policy.refresh_from_db()
            self.assertEqual(policy.version, 2)
            self.assertIsNone(policy.last_automatic_date)
            self.assertFalse(RetentionPreview.objects.exists())
            sweep_retention()
            self.assertEqual(RetentionCleanup.objects.get().preview.policy_version, 2)

    def test_policy_changed_while_waiting_for_blob_leaves_html_available(self):
        run = self.collect()
        cleanup, _body = self.accept(self.preview(['html']))
        paths = {safe_snapshot_path(path) for path in run.snapshots.values_list('content_path', flat=True)}
        changed = []

        @contextmanager
        def change_policy_during_blob_lock(resource, **kwargs):
            if resource.startswith('blob:') and not changed:
                response = self.client.patch('/api/scout/retention/policy/', {
                    'expected_version': 1, 'html_days': 500, 'result_days': 600,
                }, format='json')
                changed.append(response.status_code)
            with resource_lock(resource, **kwargs) as acquired:
                yield acquired

        with patch('modules.scout.api.services.retention.resource_lock', change_policy_during_blob_lock):
            self.finish(cleanup)
        self.assertEqual(changed, [200])
        self.assertEqual(set(cleanup.items.values_list('reason', flat=True)), {'policy_changed'})
        self.assertTrue(all(path.exists() for path in paths))
        self.assertFalse(run.snapshots.exclude(html_availability='available').exists())

    def test_other_owner_shared_blob_protects_file_without_disclosing_identity(self):
        run = self.collect()
        second = self.collect()
        Run.objects.filter(pk=second.pk).update(user_public_id=uuid4())
        preview = self.preview(['html'])
        self.assertEqual(preview['totals']['html_files'], 0)
        self.assertIn('shared_blob', preview['protected'])
        self.assertNotIn(str(second.public_id), str(preview))
        self.assertTrue(all(safe_snapshot_path(value).exists() for value in run.snapshots.values_list('content_path',flat=True)))

    def test_unsafe_path_blocks_preview(self):
        run = self.collect()
        row = run.snapshots.first()
        row.content_path = '../outside.html'
        row.save(update_fields=['content_path'])
        preview = self.preview(['html'])
        self.assertFalse(preview['executable'])
        response = self.client.post('/api/scout/retention/cleanups/', {
            'preview_public_id':preview['preview_public_id'], 'manifest_hash':preview['manifest_hash'],
            'idempotency_key':str(uuid4()),
        }, format='json')
        self.assertEqual(response.data['code'], 'cleanup_preview_not_executable')

    def test_recreated_blob_does_not_reactivate_expired_snapshot(self):
        from ..services.retention import recover_pending_blob
        from ..services.retention_locks import resource_lock
        from ..services.snapshot_store import read_snapshot_bytes, store_snapshot_bytes
        from ..services.errors import ScoutError
        self.collect()
        cleanup, _body = self.accept(self.preview(['html']))
        item = cleanup.items.first()
        old = PageSnapshot.objects.get(public_id=item.specification['refs'][0])
        body = read_snapshot_bytes(old.content_path)
        PageSnapshot.objects.filter(public_id__in=item.specification['refs']).update(html_availability='purging')
        safe_snapshot_path(old.content_path).unlink()
        with resource_lock('blob:' + old.content_hash):
            recover_pending_blob(old.content_hash)
            store_snapshot_bytes(body)
        with self.assertRaises(ScoutError) as error:
            read_snapshot_bytes(old.content_path, snapshot=old)
        self.assertEqual(error.exception.code, 'snapshot_content_expired')
        self.assertEqual(read_snapshot_bytes(old.content_path), body)
        self.finish(cleanup)

    def test_result_cleanup_removes_exports_and_handles_storage_outage(self):
        from src.core.utils.media_client.local import LocalMediaClient
        base = self.collect()
        self.collect()
        self.collect()
        media = LocalMediaClient(str(Path(self.files)/'media'))
        with patch('modules.scout.api.services.exporting.get_media_client', return_value=media), patch(
            'modules.scout.api.services.retention.get_media_client', return_value=media,
        ):
            self.assertEqual(self.client.post(f'/api/scout/runs/{base.public_id}/export/',
                                             {'kind':'json'},format='json').status_code,200)
            export = ExportArtifact.objects.get()
            cleanup, _body = self.accept(self.preview(['results']))
            with patch.object(media,'delete',side_effect=OSError('do not disclose credentials')):
                process_cleanup(cleanup.public_id)
            base.refresh_from_db()
            self.assertEqual(base.results_availability,'purging')
            self.assertEqual(base.records.count(),3)
            self.assertTrue(media.exists(export.storage_path))
            self.assertNotIn('credentials', str(self.client.get(
                f'/api/scout/retention/cleanups/{cleanup.public_id}/').data))
            cleanup.items.update(next_retry_at=None)
            self.finish(cleanup)
            self.assertFalse(media.exists(export.storage_path))
            self.assertEqual(base.records.count(),0)

    def test_result_cleanup_finishes_previously_interrupted_export_deletions(self):
        from src.core.utils.media_client.local import LocalMediaClient
        base = self.collect()
        self.collect()
        self.collect()
        media = LocalMediaClient(str(Path(self.files) / 'media'))
        with patch('modules.scout.api.services.exporting.get_media_client', return_value=media), patch(
            'modules.scout.api.services.retention.get_media_client', return_value=media,
        ):
            url = f'/api/scout/runs/{base.public_id}/export/'
            for kind in ('json', 'csv'):
                self.assertEqual(self.client.post(url, {'kind': kind}, format='json').status_code, 200)
            base.exports.update(created_at=timezone.now() - timedelta(days=8))
            export_cleanup, _body = self.accept(self.preview(['exports']))
            original_delete = media.delete

            def crash_after_deletion(path):
                original_delete(path)
                raise OSError('worker stopped before saving the deletion')

            with patch.object(media, 'delete', side_effect=OSError('storage unavailable')):
                process_cleanup(export_cleanup.public_id)
            with patch.object(media, 'delete', side_effect=crash_after_deletion):
                process_cleanup(export_cleanup.public_id)
            self.assertEqual(base.exports.filter(availability='purging').count(), 2)
            paths = list(base.exports.values_list('storage_path', flat=True))
            self.assertEqual(sum(media.exists(path) for path in paths), 1)

            preview = self.preview(['results'])
            self.assertTrue(preview['executable'])
            self.assertEqual(preview['totals']['export_files'], 2)
            cleanup, _body = self.accept(preview)
            self.finish(cleanup)
            base.refresh_from_db()
            self.assertEqual(base.results_availability, 'expired')
            self.assertEqual(base.exports.filter(availability='expired').count(), 2)
            self.assertTrue(all(not media.exists(path) for path in paths))

    def test_legacy_started_result_cleanup_waits_for_omitted_export(self):
        from src.core.utils.media_client.local import LocalMediaClient
        base = self.collect()
        self.collect()
        self.collect()
        media = LocalMediaClient(str(Path(self.files) / 'media'))
        with patch('modules.scout.api.services.exporting.get_media_client', return_value=media), patch(
            'modules.scout.api.services.retention.get_media_client', return_value=media,
        ):
            url = f'/api/scout/runs/{base.public_id}/export/'
            self.assertEqual(self.client.post(url, {'kind': 'json'}, format='json').status_code, 200)
            base.exports.update(created_at=timezone.now() - timedelta(days=8))
            artifact = base.exports.get()
            export_cleanup, _body = self.accept(self.preview(['exports']))
            with patch.object(media, 'delete', side_effect=OSError('storage unavailable')):
                process_cleanup(export_cleanup.public_id)
            artifact.refresh_from_db()
            self.assertEqual(artifact.availability, 'purging')

            cleanup, _body = self.accept(self.preview(['results']))
            item = cleanup.items.get()
            item.specification['exports'] = []
            item.save(update_fields=['specification'])
            preview = cleanup.preview
            preview.manifest = [item.specification]
            preview.manifest_hash = digest(preview.manifest)
            preview.save(update_fields=['manifest', 'manifest_hash'])
            Run.objects.filter(pk=base.pk).update(results_availability='purging')

            process_cleanup(cleanup.public_id)
            item.refresh_from_db()
            base.refresh_from_db()
            self.assertEqual(base.results_availability, 'purging')
            self.assertEqual(base.records.count(), 3)
            self.assertEqual(item.status, 'failed')
            self.assertGreater(item.next_retry_at, timezone.now())
            self.assertTrue(media.exists(artifact.storage_path))

            export_cleanup.items.update(next_retry_at=None)
            self.finish(export_cleanup)
            cleanup.items.update(next_retry_at=None)
            self.finish(cleanup)
            base.refresh_from_db()
            self.assertEqual(base.results_availability, 'expired')
            self.assertFalse(media.exists(artifact.storage_path))

    def test_database_rollback_releases_writer_lock(self):
        from django.db import connection, transaction, DatabaseError
        from ..services.retention_locks import resource_lock
        if connection.vendor != 'postgresql':
            self.skipTest('Проверка блокировок сеанса PostgreSQL')
        def locks():
            with connection.cursor() as cursor:
                cursor.execute("SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND pid=pg_backend_pid()")
                return cursor.fetchone()[0]
        before = locks()
        with self.assertRaises(DatabaseError):
            with transaction.atomic():
                with resource_lock('rollback-test'):
                    with connection.cursor() as cursor:
                        cursor.execute('SELECT 1 / 0')
        self.assertEqual(locks(), before)
