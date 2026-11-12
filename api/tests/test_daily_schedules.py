"""Ежедневные даты, подтверждённая версия и восстановление очереди."""

import os
from datetime import datetime, time, timedelta, timezone as utc_timezone
from threading import Thread
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import TestCase, SimpleTestCase
from rest_framework.test import APIClient

from ..models import DailySchedule, Run, ScheduledOccurrence
from ..services.schedule_calendar import first_future_date, scheduled_time
from ..services.scheduled_runs import sweep_schedules
from ..services.tasks_exec import publish_ready
from .fixtures.ext002_catalog import ComparisonCatalog, comparison_plan

UTC = utc_timezone.utc


class DailyScheduleTests(TestCase):
    def setUp(self):
        self.site = ComparisonCatalog()
        Thread(target=self.site.serve_forever, daemon=True).start()
        self.addCleanup(self.site.server_close)
        self.addCleanup(self.site.shutdown)
        self.enterContext(patch.dict(os.environ, {
            'SCOUT_TASK_RUNNER': 'inline',
            'SCOUT_TRUSTED_FETCH_ENDPOINTS': f'127.0.0.1:{self.site.server_port}',
        }))
        self.client = APIClient()
        self.user = get_user_model().objects.create_superuser(username='scout-schedule-owner')
        self.client.force_authenticate(self.user)
        self.anchor = datetime(2026, 11, 7, 5, tzinfo=UTC)
        self.due = self.anchor + timedelta(hours=1)
        self.plan = comparison_plan(self.site.origin)

    def _prepare(self):
        job = self.client.post('/api/scout/jobs/', self.plan, format='json').data
        self.job_id = job['public_id']
        pending = self.client.post(f'/api/scout/jobs/{self.job_id}/explore/').data
        path = f"/api/scout/runs/{pending['public_id']}/"
        publish_ready(pending['public_id'])
        approved = self.client.post(path + 'approve/')
        self.assertEqual(approved.status_code, 200, approved.data)
        self.assertEqual(self.client.post(path + 'cancel/').status_code, 200)
        self.url = f'/api/scout/jobs/{self.job_id}/schedule/'
        self.body = {
            'plan_revision_public_id': job['revision']['public_id'],
            'plan_hash': job['revision']['plan_hash'], 'local_time': '09:00',
            'timezone': 'Europe/Moscow', 'enabled': True,
        }
        with patch('django.utils.timezone.now', return_value=self.anchor):
            response = self.client.post(self.url, self.body, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        self.schedule = DailySchedule.objects.get(public_id=response.data['public_id'])
        self.assertEqual(response.data['next_scheduled_at'], self.due)

    def _tick(self, when):
        sweep_schedules(now=when)
        publish_ready()
        return self.schedule.occurrences.select_related('run', 'revision').order_by('local_date')

    def test_daily_runs_repeat_safely_and_compare_prices(self):
        self._prepare()
        rows = self._tick(self.due + timedelta(seconds=5))
        first = rows.get()
        self.assertEqual(first.status, 'completed')
        self.assertEqual(first.run.records.count(), 3)
        first_id = first.run.public_id
        self._tick(self.due + timedelta(minutes=5))
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.get().run.public_id, first_id)
        self.site.add_product_and_change_price()
        self._tick(self.due + timedelta(days=1, seconds=10))
        second = rows.last()
        self.assertEqual(second.run.records.count(), 4)
        compare = self.client.get(f'/api/scout/runs/{second.run.public_id}/compare/', {
            'base_run_public_id': str(first_id), 'field': 'price',
        })
        self.assertEqual(compare.status_code, 200, compare.data)
        self.assertEqual(compare.data['counts']['changed'], 1)
        self.assertEqual(compare.data['counts']['added'], 1)
        history = self.client.get(self.url + 'occurrences/?limit=1').data
        self.assertEqual(history['occurrences'][0]['run_public_id'], str(second.run.public_id))
        tail = self.client.get(self.url + 'occurrences/', {'limit': 1, 'cursor': history['next_cursor']}).data
        self.assertEqual(tail['occurrences'][0]['run_public_id'], str(first_id))
        self.assertIsNone(tail['next_cursor'])

    def test_busy_day_and_late_wakeup_are_not_replayed(self):
        self._prepare()
        pending = self.client.post(f'/api/scout/jobs/{self.job_id}/explore/').data
        publish_ready(pending['public_id'])
        self._tick(self.due + timedelta(seconds=5))
        skipped = self.schedule.occurrences.get()
        self.assertEqual((skipped.status, skipped.reason, skipped.run_id), ('skipped', 'slot_busy', None))
        self.client.post(f"/api/scout/runs/{pending['public_id']}/cancel/")
        self._tick(self.due + timedelta(minutes=10))
        self.assertEqual(self.schedule.occurrences.count(), 1)
        self._tick(self.due + timedelta(days=1, minutes=15))
        self.assertEqual(self.schedule.occurrences.filter(reason='missed').count(), 1)
        self._tick(self.due + timedelta(days=3, minutes=25))
        self.assertEqual(self.schedule.occurrences.filter(reason='missed').count(), 3)
        self.assertFalse(self.schedule.occurrences.exclude(run_id=None).exists())

    def test_change_after_downtime_preserves_old_time_in_history(self):
        self._prepare()
        changed_at = self.anchor + timedelta(days=1)
        with patch('django.utils.timezone.now', return_value=changed_at):
            response = self.client.patch(self.url, {'expected_version': 1, 'local_time': '10:00'}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        rows = self._tick(self.due + timedelta(days=1, hours=1, seconds=5))
        old, current = list(rows)
        self.assertEqual((old.reason, old.scheduled_at), ('missed', self.due))
        self.assertEqual(current.status, 'completed')
        self.assertEqual(current.revision.number, 2)
        self.assertEqual(current.scheduled_at, self.due + timedelta(days=1, hours=1))

    def test_change_during_window_is_superseded_even_after_late_tick(self):
        self._prepare()
        with patch('django.utils.timezone.now', return_value=self.due + timedelta(minutes=5)):
            response = self.client.patch(self.url, {'expected_version': 1, 'local_time': '10:00'}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        old = self._tick(self.due + timedelta(minutes=20)).get()
        self.assertEqual((old.status, old.reason, old.scheduled_at), ('skipped', 'superseded', self.due))
        self.assertEqual(old.revision.number, 1)
        self.assertIsNone(old.run_id)

    def test_change_at_end_of_window_leaves_previous_occurrence_missed(self):
        self._prepare()
        with patch('django.utils.timezone.now', return_value=self.due + timedelta(minutes=15)):
            response = self.client.patch(self.url, {'expected_version': 1, 'local_time': '10:00'}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        old = self._tick(self.due + timedelta(minutes=20)).get()
        self.assertEqual((old.status, old.reason), ('skipped', 'missed'))
        self.assertIsNone(old.run_id)

    def test_change_at_opening_takes_precedence_over_replacement_on_same_day(self):
        self._prepare()
        with patch('django.utils.timezone.now', return_value=self.due):
            response = self.client.patch(self.url, {'expected_version': 1, 'local_time': '10:00'}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        old = self._tick(self.due + timedelta(hours=1, minutes=5)).get()
        self.assertEqual((old.status, old.reason, old.scheduled_at), ('skipped', 'superseded', self.due))
        self.assertEqual(old.revision.number, 1)
        self.assertIsNone(old.run_id)

    def test_version_checks_disable_and_pin_old_plan(self):
        self._prepare()
        self.assertEqual(self.client.post(self.url, self.body, format='json').data['code'], 'schedule_exists')
        self.assertEqual(self.client.patch(self.url, {'expected_version': 0, 'enabled': False}, format='json').data['code'],
                         'schedule_changed')
        self.plan['fields'][1]['selector'] = 'h2'
        changed = self.client.patch(f'/api/scout/jobs/{self.job_id}/', self.plan, format='json')
        self.assertEqual(changed.status_code, 200, changed.data)
        occurrence = self._tick(self.due + timedelta(minutes=10)).get()
        self.assertEqual(str(occurrence.run.job_revision.public_id), self.body['plan_revision_public_id'])
        self.assertEqual(occurrence.run.records.count(), 3)
        with patch('django.utils.timezone.now', return_value=self.due + timedelta(minutes=11)):
            disabled = self.client.patch(self.url, {'expected_version': 1, 'enabled': False}, format='json')
        self.assertEqual(disabled.status_code, 200, disabled.data)
        self.assertEqual(disabled.data['version'], 2)
        self.assertIsNone(disabled.data['next_scheduled_at'])
        self._tick(self.due + timedelta(days=2))
        self.assertEqual(self.schedule.occurrences.count(), 1)

    def test_fresh_preview_failure_releases_slot(self):
        self.plan['crawl'].pop('extract_on')
        self.plan['crawl']['page_types'] = [{
            'id': 'product', 'match': [{'kind': 'prefix', 'pattern': '/products/'}], 'action': 'extract',
        }]
        self._prepare()
        self.site.pages['/products/k-01'] = '<p>Разметка изменилась</p>'
        occurrence = self._tick(self.due + timedelta(seconds=1)).get()
        self.assertEqual(occurrence.status, 'failed')
        self.assertEqual(occurrence.reason, 'page_type_preview_blocked')
        self.assertEqual(occurrence.run.status, 'failed')
        self.assertEqual(occurrence.run.records.count(), 0)
        pending = self.client.post(f'/api/scout/jobs/{self.job_id}/explore/')
        self.assertEqual(pending.status_code, 202, pending.data)

    def test_revoked_permission_disables_schedule_and_validation_is_strict(self):
        self._prepare()
        with patch('modules.scout.api.services.scheduled_runs.owner_can_schedule', return_value=False):
            occurrence = self._tick(self.due + timedelta(seconds=1)).get()
        self.assertEqual(occurrence.reason, 'permission_revoked')
        self.assertFalse(self.client.get(self.url).data['enabled'])
        for values in ({'local_time': '9:00'}, {'timezone': 'Missing/Zone'}, {'enabled': 'true'}):
            response = self.client.patch(self.url, {'expected_version': 2, **values}, format='json')
            self.assertEqual(response.status_code, 400, response.data)
        self.client.force_authenticate(get_user_model().objects.create_user(username='schedule-other-reader'))
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.assertEqual(self.client.get(self.url + 'occurrences/').status_code, 404)


class ScheduleCalendarTests(SimpleTestCase):
    def test_dst_gap_overlap_and_future_date(self):
        zone = ZoneInfo('Europe/Berlin')
        gap = datetime(2026, 3, 29).date()
        due, process_at = scheduled_time(gap, time(2, 30), zone)
        self.assertIsNone(due)
        self.assertGreaterEqual(process_at.astimezone(zone).hour, 3)
        overlap = datetime(2026, 10, 25).date()
        due, _process_at = scheduled_time(overlap, time(2, 30), zone)
        self.assertEqual(due, datetime(2026, 10, 25, 0, 30, tzinfo=UTC))
        now = datetime(2026, 11, 7, 6, tzinfo=UTC)
        self.assertEqual(first_future_date(now, time(9), 'Europe/Moscow'), now.date() + timedelta(days=1))
