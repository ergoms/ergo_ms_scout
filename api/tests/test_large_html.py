import gzip
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from modules.scout.api.services.constants import DEFAULT_LIMITS
from modules.scout.api.services.plan import normalize_plan
from modules.scout.api.services.runs import create_job, revise_job

from .test_html_io import _connection


_HTML_LIMIT = 4_000_000
_URL = 'http://catalog.example/items'
_FIELDS = [{'name': 'title', 'kind': 'css', 'selector': 'h1'}]


class LargeHTMLTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_superuser(username='large-html-owner')
        self.client = APIClient()
        self.client.force_authenticate(self.owner)

    def test_new_jobs_use_the_larger_limit_and_revisions_keep_the_saved_limit(self):
        job_args = {'user_public_id': self.owner.public_id, 'title': 'Каталог', 'seed_urls': [_URL], 'fields': _FIELDS}
        with patch.dict(DEFAULT_LIMITS, {'max_bytes': 1_000_000}):
            legacy_job = create_job(**job_args)
        original = legacy_job.revisions.get()

        fresh = create_job(**job_args).revisions.get()
        self.assertEqual(fresh.limits['max_bytes'], _HTML_LIMIT)
        self.assertNotEqual(fresh.plan_hash, original.plan_hash)
        capped = normalize_plan([_URL], _FIELDS, limits={'max_bytes': _HTML_LIMIT + 1})
        self.assertEqual(capped['limits']['max_bytes'], _HTML_LIMIT)

        revised = revise_job(job=legacy_job, seed_urls=[_URL], fields=_FIELDS)
        self.assertEqual(revised.limits['max_bytes'], 1_000_000)
        self.assertEqual(revised.plan_hash, original.plan_hash)

    def _load_page(self, size, *, compressed=False):
        start = b'<html><body><!--'
        end = b'--><article><h1>Last heading</h1></article></body></html>'
        body = start + b'x' * (size - len(start) - len(end)) + end
        wire_body = gzip.compress(body) if compressed else body
        headers = f'Content-Length: {len(wire_body)}\r\n'.encode('ascii')
        if compressed:
            headers += b'Content-Encoding: gzip\r\n'
        with (
            patch.dict('os.environ', {'SCOUT_TASK_RUNNER': 'inline'}),
            patch('modules.scout.api.services.public_http._connect_public', return_value=_connection(headers, wire_body)),
            self.captureOnCommitCallbacks(execute=True),
        ):
            created = self.client.post('/api/scout/visual-snapshots/', {'url': _URL}, format='json')
        self.assertEqual(created.status_code, 202, created.data)
        return f"/api/scout/visual-snapshots/{created.data['public_id']}/"

    def test_at_the_limit_preview_and_tree_expose_content_at_the_end(self):
        path = self._load_page(_HTML_LIMIT, compressed=True)
        preview = self.client.get(path)
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.data['status'], 'ready')
        self.assertIn('Last heading', preview.data['html'])

        tree = self.client.get(path + 'tree/', {'tree_version': preview.data['tree_version']})
        self.assertEqual(tree.status_code, 200, tree.data)
        self.assertEqual(tree.data['node_count'], 2)
        article = tree.data['items'][0]
        self.assertEqual(article['tag'], 'article')
        children = self.client.get(path + 'tree/', {
            'tree_version': preview.data['tree_version'], 'parent_id': article['node_id'],
        })
        self.assertEqual(children.status_code, 200, children.data)
        self.assertEqual(children.data['items'][0]['text_preview'], 'Last heading')

    def test_one_byte_over_the_limit_fails_before_a_visual_snapshot_is_published(self):
        for compressed in (False, True):
            with self.subTest(compressed=compressed):
                path = self._load_page(_HTML_LIMIT + 1, compressed=compressed)
                preview = self.client.get(path)
                self.assertEqual(preview.status_code, 200)
                self.assertEqual(preview.data['status'], 'failed')
                self.assertEqual(preview.data['error']['code'], 'too_large')
                self.assertNotIn('html', preview.data)
