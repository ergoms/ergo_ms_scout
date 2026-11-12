"""Предел попыток, отложенная загрузка и освобождение слота Scout."""

import os
from datetime import time, timedelta
from unittest.mock import patch
from uuid import uuid4

from django.test import TestCase
from django.utils import timezone

from ..models import DailySchedule, PageSnapshot, Run, ScheduleRevision, ScheduledOccurrence, Task
from ..services.constants import MAX_ATTEMPTS
from ..services.errors import FetchError
from ..services.frontier import open_site
from ..services.preview import preview_rows
from ..services.runs import create_job, explore_job
from ..services.scheduled_runs import progress_scheduled_runs
from ..services.tasks_exec import _claim, _fetch, _ready, recover_expired_leases, run_one
from ..tasks import dispatch_scout_pending, execute_scout_task


class LifecycleTests(TestCase):
    def setUp(self):
        self.owner = uuid4()

    def _run(self, crawl=None, *, status='running', selector=None, paths=('a',)):
        job = create_job(
            user_public_id=self.owner,
            title='Проверка Scout',
            seed_urls=[f'https://example.test/{path}' for path in paths],
            fields=[{'name': 'title', 'kind': 'xpath', 'selector': selector or '//h1'}],
            crawl=crawl,
        )
        return Run.objects.create(
            job_revision=job.revisions.get(), user_public_id=self.owner, status=status, started_at=timezone.now(),
        )

    def _expire_lease(self, task):
        Task.objects.filter(pk=task.pk).update(lease_until=timezone.now() - timedelta(seconds=1))
        self.assertEqual(recover_expired_leases(), 1)
        task.refresh_from_db()

    def test_expired_leases_reach_attempt_limit_and_close_the_frontier(self):
        run = self._run({'mode': 'site'})
        open_site(run)
        task = run.tasks.get(kind='fetch')
        for attempt in range(MAX_ATTEMPTS):
            claimed, token = _claim(str(task.public_id), task.fencing_token)
            self._expire_lease(claimed)
            task.refresh_from_db()
            self.assertEqual(task.status, 'pending' if attempt < MAX_ATTEMPTS - 1 else 'failed')
            self.assertFalse(run_one(str(task.public_id), token))

        run.refresh_from_db()
        self.assertEqual(task.attempt_count, MAX_ATTEMPTS)
        self.assertEqual(task.error_code, 'lease_lost')
        self.assertEqual(task.attempts.filter(status='failed', error_code='lease_lost').count(), MAX_ATTEMPTS)
        self.assertEqual(run.crawl_urls.get().state, 'failed')
        self.assertEqual(run.status, 'completed_with_errors')

    def test_expired_last_sitemap_attempt_cancels_other_maps(self):
        run = self._run({'mode': 'site'})
        task = Task.objects.create(
            run=run, kind='sitemap', status='leased', idempotency_key='sitemap', attempt_count=MAX_ATTEMPTS,
        )
        child = Task.objects.create(run=run, kind='sitemap', status='pending', idempotency_key='sitemap:child')
        self._expire_lease(task)
        child.refresh_from_db()
        run.refresh_from_db()
        self.assertEqual(task.status, 'failed')
        self.assertEqual(child.status, 'cancelled')
        self.assertEqual(run.status, 'failed')

    def test_parallel_fetch_waits_for_reserved_bytes_before_claiming(self):
        run = self._run({'mode': 'site', 'max_total_bytes': 1_500_000, 'parallel_fetch': 2}, paths=('a', 'b'))
        open_site(run)
        first, second = run.tasks.order_by('pk')

        def simulated_fetch(url, *, max_bytes, **kwargs):
            size = 10_000 if url.endswith('/a') else 600_000
            if size > max_bytes:
                raise FetchError('too_large', 'Ответ больше лимита', retryable=False)
            body = b'<h1>Test</h1>'.ljust(size, b' ')
            return {'body': body, 'final_url': url, 'content_type': 'text/html', 'status': 200}

        claimed, token = _claim(str(first.public_id), first.fencing_token)
        with (
            patch('modules.scout.api.services.tasks_exec.fetch_public_html', side_effect=simulated_fetch),
            patch('modules.scout.api.services.tasks_exec.store_snapshot_bytes', return_value=('x', 'test.html')),
        ):
            self.assertFalse(run_one(str(second.public_id), second.fencing_token))
            _fetch(claimed, token)
            self.assertTrue(run_one(str(second.public_id), second.fencing_token))

        run.refresh_from_db()
        self.assertEqual(run.stop_reason, '')
        self.assertEqual(sum(run.snapshots.values_list('byte_size', flat=True)), 610_000)

    def test_fetch_delay_schedules_one_dispatch_for_waiting_tasks(self):
        run = self._run({'mode': 'site', 'fetch_delay_ms': 500, 'parallel_fetch': 2}, paths=('a', 'b', 'c'))
        open_site(run)
        first, second, third = run.tasks.order_by('pk')
        now = timezone.now()
        with (
            patch.object(timezone, 'now', return_value=now),
            patch.dict(os.environ, {'SCOUT_TASK_RUNNER': 'celery'}),
            patch.object(dispatch_scout_pending, 'apply_async') as dispatch,
            patch('modules.scout.api.services.tasks_exec.fetch_public_html') as fetch,
            self.captureOnCommitCallbacks(execute=True),
        ):
            self.assertIsNotNone(_claim(str(first.public_id), first.fencing_token))
            for task in (second, third):
                self.assertEqual(execute_scout_task.run(str(task.public_id), task.fencing_token), {'changed': False})
            self.assertFalse(_ready(run.public_id).exists())
            fetch.assert_not_called()
        dispatch.assert_called_once_with(args=[str(run.public_id)], countdown=0.5)
        with patch.object(timezone, 'now', return_value=now + timedelta(milliseconds=500)):
            self.assertCountEqual(_ready(run.public_id).values_list('pk', flat=True), [second.pk, third.pk])

    def _scheduled_preview(self, selector):
        run = self._run(status='awaiting_confirmation', selector=selector)
        plan = run.job_revision
        plan.approved_plan_hash = plan.plan_hash
        plan.save(update_fields=['approved_plan_hash'])
        url = plan.seed_urls[0]
        PageSnapshot.objects.create(
            run=run, source_url=url, final_url=url, content_path='test.html', content_hash='a' * 64,
            content_type='text/html',
        )
        schedule = DailySchedule.objects.create(job=plan.job)
        now = timezone.now()
        revision = ScheduleRevision.objects.create(
            schedule=schedule, number=1, plan_revision=plan, plan_hash=plan.plan_hash, enabled=True,
            local_time=time(12), timezone='UTC', effective_from=now,
        )
        occurrence = ScheduledOccurrence.objects.create(
            schedule=schedule, revision=revision, local_date=now.date(), scheduled_at=now, processed_at=now,
            status='preparing', run=run,
        )
        self.enterContext(patch('modules.scout.api.services.scheduled_runs.owner_can_schedule', return_value=True))
        return run, occurrence

    def test_added_comment_does_not_break_the_scheduled_xpath_preview(self):
        run, occurrence = self._scheduled_preview('//h1 | //comment()')
        html = b'<div><h1>Product</h1><!-- notes --></div>'
        with patch('modules.scout.api.services.preview.read_snapshot_bytes', return_value=html):
            self.assertEqual(preview_rows(run)[0]['values']['title']['raw'], 'Product')
            progress_scheduled_runs(str(run.public_id))
        run.refresh_from_db()
        occurrence.refresh_from_db()
        self.assertEqual((run.status, occurrence.status), ('running', 'running'))

    def test_unexpected_preview_error_releases_the_global_slot(self):
        run, occurrence = self._scheduled_preview('//h1')
        with (
            patch('modules.scout.api.services.runs.preview_rows', side_effect=ValueError('parser error')),
            self.assertLogs('celery.module.scout.tasks', level='ERROR'),
        ):
            progress_scheduled_runs(str(run.public_id))
        run.refresh_from_db()
        occurrence.refresh_from_db()
        self.assertEqual((run.status, occurrence.status, occurrence.reason), ('failed', 'failed', 'unexpected'))
        self.assertEqual(explore_job(job=run.job_revision.job, user_public_id=self.owner).status, 'exploring')
