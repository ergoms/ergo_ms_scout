from datetime import timedelta
from unittest.mock import patch
from uuid import UUID, uuid4

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from ..models import Job, JobRevision, Run
from ..permissions import CanViewScout
from ..services.run_history import list_job_runs


class RunHistoryTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username='history-owner')
        self.other = get_user_model().objects.create_user(username='history-other')
        self.enterContext(patch.object(CanViewScout, 'has_permission', return_value=True))
        self.enterContext(patch('modules.scout.api.services.access.PermissionService.is_admin', return_value=False))
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.job = Job.objects.create(user_public_id=self.user.public_id, title='History')
        self.revision = JobRevision.objects.create(job=self.job, revision_number=1, plan_hash='a'*64,
            fields=[{'name': 'price', 'kind': 'css', 'selector': 'p', 'scope': 'page', 'many': False}],
            identity={'kind': 'field', 'field': 'price', 'namespace': 'history'})
        self.url = f'/api/scout/jobs/{self.job.public_id}/runs/'

    def make_run(self, **kwargs):
        values = {'job_revision': self.revision, 'user_public_id': self.user.public_id,
                  'status': 'completed', 'started_at': timezone.now()-timedelta(minutes=1),
                  'finished_at': timezone.now()}
        values.update(kwargs)
        return Run.objects.create(**values)

    def test_tied_timestamps_and_new_insert_do_not_duplicate_or_shift_pages(self):
        date = timezone.now()-timedelta(days=1)
        runs = [self.make_run(public_id=UUID(int=i)) for i in (1, 2, 3, 4)]
        Run.objects.filter(pk__in=[run.pk for run in runs]).update(created_at=date)
        first = self.client.get(self.url, {'limit': 2})
        self.assertEqual(first.status_code, 200)
        ids = [item['public_id'] for item in first.data['runs']]
        self.assertEqual(ids, [str(runs[3].public_id), str(runs[2].public_id)])
        self.make_run()  # Arrived after the cursor's upper fence.
        second = self.client.get(self.url, {'cursor': first.data['next_cursor'], 'limit': 2})
        self.assertEqual(second.status_code, 200)
        self.assertEqual([item['public_id'] for item in second.data['runs']], [str(runs[1].public_id), str(runs[0].public_id)])
        self.assertIsNone(second.data['next_cursor'])

    def test_cursor_is_bound_to_job_and_owner_and_rejects_tampering(self):
        self.make_run(); self.make_run()
        cursor = self.client.get(self.url, {'limit': 1}).data['next_cursor']
        another = Job.objects.create(user_public_id=self.user.public_id, title='Another')
        other_url = f'/api/scout/jobs/{another.public_id}/runs/'
        for url, token in ((self.url, cursor+'x'), (other_url, cursor), (self.url, 'garbage')):
            response = self.client.get(url, {'cursor': token})
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.data['code'], 'invalid_cursor')
        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.get(self.url, {'cursor': 'garbage'}).status_code, 404)
        self.assertEqual(self.client.get(f'/api/scout/jobs/{uuid4()}/runs/').status_code, 404)
        with patch('modules.scout.api.services.access.PermissionService.is_admin', return_value=True):
            response = self.client.get(self.url, {'cursor': cursor})
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.data['code'], 'invalid_cursor')

    def test_run_owner_is_checked_as_well_as_job_owner(self):
        self.make_run(user_public_id=self.other.public_id)
        self.assertEqual(self.client.get(self.url).data['runs'], [])

    def test_metadata_for_partial_expired_and_unstarted_runs(self):
        partial = self.make_run(status='completed_with_errors', stop_reason='max_pages')
        expired = self.make_run(results_availability='expired', retained_counts={'record_count': 17})
        pending = self.make_run(status='awaiting_confirmation', started_at=None, finished_at=None)
        rows = {row['public_id']: row for row in self.client.get(self.url).data['runs']}
        self.assertEqual(rows[str(partial.public_id)]['completeness'], 'partial')
        self.assertTrue(rows[str(partial.public_id)]['comparison_eligible'])
        self.assertEqual(rows[str(expired.public_id)]['record_count'], 17)
        self.assertEqual(rows[str(expired.public_id)]['comparison_blocked_reason'], 'data_expired')
        self.assertIsNone(rows[str(pending.public_id)]['completeness'])
        self.assertEqual(rows[str(pending.public_id)]['comparison_blocked_reason'], 'not_ready')
        self.assertNotIn('id', rows[str(partial.public_id)])
        self.assertEqual(rows[str(partial.public_id)]['comparison_fields'], ['price'])

    def test_missing_identity_is_visible_but_not_comparable(self):
        self.revision.identity = None
        self.revision.save(update_fields=['identity'])
        self.make_run()
        row = self.client.get(self.url).data['runs'][0]
        self.assertFalse(row['comparison_eligible'])
        self.assertEqual(row['comparison_blocked_reason'], 'identity_missing')

    def test_limits_permissions_and_empty_history(self):
        self.assertEqual(self.client.get(self.url).data['runs'], [])
        for limit in ('0', '101', 'x', '-1', '1.5'):
            self.assertEqual(self.client.get(self.url, {'limit': limit}).status_code, 400)
        with patch.object(CanViewScout, 'has_permission', return_value=False):
            self.assertEqual(self.client.get(self.url).status_code, 403)
        self.client.force_authenticate(None)
        self.assertIn(self.client.get(self.url).status_code, (401, 403))

    def test_metadata_query_count_is_bounded(self):
        for _ in range(12):
            self.make_run()
        with CaptureQueriesContext(connection) as queries:
            result = list_job_runs(self.user, self.job)
        self.assertEqual(len(result['runs']), 12)
        self.assertLessEqual(len(queries), 4)
