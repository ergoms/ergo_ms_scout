"""API EXT-002 на последовательных запусках контрольного каталога."""

import os
from threading import Thread
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from ..services.tasks_exec import publish_ready
from .fixtures.ext002_catalog import ComparisonCatalog, comparison_plan
from .fixtures.spec001_catalog import set_product


class RunComparisonTests(TestCase):
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
        self.client.force_authenticate(get_user_model().objects.create_superuser(username='scout-ext002'))
        self.plan = comparison_plan(self.site.origin)
        self.job = self._create(self.plan)

    def _create(self, plan):
        result = self.client.post('/api/scout/jobs/', plan, format='json')
        self.assertEqual(result.status_code, 201, result.data)
        return result.data['public_id']

    def _explore(self, job):
        pending = self.client.post(f'/api/scout/jobs/{job}/explore/')
        self.assertEqual(pending.status_code, 202, pending.data)
        run_id = pending.data['public_id']
        publish_ready(run_id)
        return run_id

    def _collect(self, job=None):
        run_id = self._explore(job or self.job)
        path = f'/api/scout/runs/{run_id}/'
        self.assertEqual(self.client.post(path + 'approve/').status_code, 200)
        self.assertEqual(self.client.post(path + 'start/').status_code, 202)
        publish_ready(run_id)
        return run_id

    def _compare(self, base, target, **params):
        query = {'base_run_public_id': base, 'field': 'price', **params}
        return self.client.get(f'/api/scout/runs/{target}/compare/', query)

    def test_history_selects_collected_runs_with_real_counts_and_comparison(self):
        base = self._collect()
        self.site.add_product_and_change_price()
        target = self._collect()
        url = f'/api/scout/jobs/{self.job}/runs/'
        first = self.client.get(url, {'limit': 1})
        self.assertEqual(first.status_code, 200, first.data)
        self.assertEqual(first['Cache-Control'], 'private, no-store')
        second = self.client.get(url, {'limit': 1, 'cursor': first.data['next_cursor']})
        self.assertEqual(second.status_code, 200, second.data)
        later, earlier = first.data['runs'][0], second.data['runs'][0]
        self.assertEqual((later['public_id'], earlier['public_id']), (target, base))
        self.assertEqual((later['record_count'], earlier['record_count']), (4, 3))
        self.assertTrue(later['comparison_eligible'] and earlier['comparison_eligible'])
        self.assertIn('price', later['comparison_fields'])
        self.assertIsNone(second.data['next_cursor'])
        comparison = self._compare(earlier['public_id'], later['public_id'])
        self.assertEqual(comparison.status_code, 200, comparison.data)
        self.assertEqual(comparison.data['counts']['changed'], 1)
        self.assertEqual(comparison.data['counts']['added'], 1)
        # A history read is metadata only: no new run, task or collection.
        self.assertEqual(len(self.client.get(url).data['runs']), 2)

    def test_catalog_full_and_partial_comparison_with_evidence(self):
        base = self._collect()
        self.site.add_product_and_change_price()
        full = self._collect()
        response = self._compare(base, full)
        self.assertEqual(response.status_code, 200, response.data)
        data = response.data
        self.assertEqual(data['counts'], {
            'changed': 1, 'unchanged': 1, 'uncertain': 0, 'added': 1,
            'not_seen_before': 0, 'not_seen': 0, 'not_checked': 0,
        })
        self.assertEqual([(data[side]['record_count'], data[side]['unkeyed_records']) for side in ('base', 'target')],
                         [(3, 1), (4, 1)])
        changes = {item['sku']: item for item in data['changes']}
        self.assertEqual(changes['K-03']['status'], 'added')
        self.assertIsNone(changes['K-03']['before'])
        price = changes['K-01']
        self.assertEqual(price['status'], 'changed')
        snapshots = []
        for side, amount in [('before', '1250.00'), ('after', '1350.00')]:
            observed = price[side]
            self.assertEqual(observed['state'], 'ok')
            self.assertEqual(observed['value']['typed']['value'], {'amount': amount, 'currency': 'RUB'})
            self.assertEqual({item['source_url'] for item in observed['evidence']},
                             {self.site.origin + '/products/k-01', self.site.origin + '/sale/k-01'})
            self.assertEqual({item['typed']['value']['amount'] for item in observed['evidence']}, {amount})
            self.assertEqual(sum(item['selected'] for item in observed['evidence']), 1)
            snapshots.append({item['snapshot_public_id'] for item in observed['evidence']})
        self.assertTrue(snapshots[0].isdisjoint(snapshots[1]))
        page = self._compare(base, full, limit=1).data
        self.assertEqual(page['counts'], data['counts'])
        cursor = page['next_cursor']
        self.assertTrue(cursor)
        last = self._compare(base, full, limit=1, cursor=cursor).data
        self.assertIsNone(last['next_cursor'])
        self.assertEqual(page['changes'] + last['changes'], data['changes'])
        self.assertEqual(self._compare(base, full, field='title', cursor=cursor).data['code'], 'invalid_cursor')

        missing = self.site.pages.pop('/products/k-02')
        partial = self._collect()
        data = self._compare(base, partial).data
        self.assertEqual(data['target'], {
            'run_public_id': partial, 'status': 'completed_with_errors', 'completeness': 'partial',
            'stop_reason': '', 'failed_tasks': 1, 'record_count': 3, 'unkeyed_records': 1,
        })
        changes = {item['sku']: item for item in data['changes']}
        self.assertEqual(data['counts']['not_checked'], 1)
        self.assertEqual(changes['K-02']['status'], 'not_checked')
        self.assertIsNone(changes['K-02']['after'])
        self.assertEqual(changes['K-02']['before']['value']['typed']['value']['amount'], '990.00')
        self.assertEqual(changes['K-01']['status'], 'changed')
        self.assertEqual(changes['K-03']['status'], 'added')

        self.site.pages['/products/k-02'] = missing
        restored = self._collect()
        self.assertEqual(self._compare(partial, restored).data['changes'][0]['status'], 'not_seen_before')
        self.site.links.remove('/products/k-02')
        self.site.update_links()
        unlisted = self._collect()
        self.assertEqual(self._compare(restored, unlisted).data['changes'][0]['status'], 'not_seen')

    def test_equal_formatted_prices_and_conflicting_observations(self):
        base = self._collect()
        for path in ('/products/k-01', '/sale/k-01'):
            set_product(self.site, path, 'K-01', '1250,0')
        same = self._compare(base, self._collect()).data
        self.assertEqual(same['counts']['unchanged'], 2)
        self.assertEqual(same['changes'], [])
        self.assertIsNone(same['next_cursor'])

        set_product(self.site, '/sale/k-01', 'K-01', '1 360,00')
        data = self._compare(base, self._collect()).data
        row, = data['changes']
        self.assertEqual(row['status'], 'uncertain')
        self.assertEqual(row['before']['state'], 'ok')
        self.assertEqual(row['after']['state'], 'conflict')
        self.assertEqual({item['typed']['value']['amount'] for item in row['after']['evidence']},
                         {'1250.00', '1360.00'})

    def test_comparison_checks_order_scope_and_terminal_state(self):
        base = self._collect()
        target = self._collect()
        for before, after, params, code, error in [
            (base, base, {}, 400, 'invalid_comparison'),
            (target, base, {}, 400, 'invalid_comparison'),
            (base, target, {'field': 'unknown'}, 400, 'invalid_comparison'),
            (base, target, {'limit': 0}, 400, 'invalid_limit'),
            (base, target, {'cursor': 'fake'}, 400, 'invalid_cursor'),
            (base, self._collect(self._create(self.plan)), {}, 409, 'comparison_incompatible'),
            (base, self._explore(self.job), {}, 409, 'comparison_not_ready'),
        ]:
            with self.subTest(error=error, params=params):
                result = self._compare(before, after, **params)
                self.assertEqual((result.status_code, result.data['code']), (code, error))
        self.client.force_authenticate(get_user_model().objects.create_user(username='other-comparison-reader'))
        self.assertEqual(self._compare(base, target).status_code, 404)
