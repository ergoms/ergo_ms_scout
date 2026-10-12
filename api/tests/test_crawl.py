"""Обход одного сайта: циклы, дубли, фильтры, пауза и потеря исполнителя."""

import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from modules.scout.api.models import CrawlURL, Record, Task
from modules.scout.api.services.constants import RUN_COMPLETED, RUN_RUNNING, TASK_PENDING
from modules.scout.api.services.frontier import discover, remember
from modules.scout.api.services.runs import approve_run, create_job, explore_job, pause_run, resume_run, start_run, _finish_if_idle
from modules.scout.api.services.tasks_exec import publish_ready, recover_expired_leases

FIELDS = [{'name': 'title', 'kind': 'css', 'selector': 'h1', 'scope': 'page', 'many': False}]


def _site(port_holder):
    class Handler(BaseHTTPRequestHandler):
        hits = {'/flaky': 0}

        def do_GET(self):
            host = self.headers.get('Host')
            path = self.path.split('?', 1)[0]
            if path == '/go':
                self.send_response(302)
                self.send_header('Location', f'http://{host}/quiet')
                self.end_headers()
                return
            if path == '/flaky':
                Handler.hits['/flaky'] += 1
                if Handler.hits['/flaky'] == 1:
                    self.send_response(503)
                    self.end_headers()
                    return
            body = _body(self.path, host).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            return

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    port_holder.append(server)
    return server


def _body(path, host):
    if 'page=2' in path:
        return '<html><body><h1>Page2</h1><a href="/item?id=9">id</a></body></html>'
    if path.startswith('/a'):
        return f'<html><body><h1>A</h1><a href="/b">b</a><a href="/a#frag">self</a><a href="http://evil.test/no">out</a><a href="/file.pdf">pdf</a></body></html>'
    if path.startswith('/b'):
        return '<html><body><h1>B</h1><a href="/a">back</a></body></html>'
    if path.startswith('/cat'):
        return '<html><body><h1>Catalog</h1><a href="item/1">item</a><link rel="next" href="?page=2"></body></html>'
    if path.startswith('/item'):
        return '<html><body><h1>Item</h1></body></html>'
    if 'page=2' in path:
        return '<html><body><h1>Page2</h1><a href="/item?id=9">id</a></body></html>'
    if path.startswith('/quiet'):
        return '<html><body><h1>Quiet</h1></body></html>'
    return '<html><body><h1>Other</h1></body></html>'


class CrawlTests(TestCase):
    def setUp(self):
        import os
        os.environ['SCOUT_TASK_RUNNER'] = 'inline'
        self.user = get_user_model().objects.create_superuser(
            username='scout-crawl',
            email='scout-crawl@example.com',
            password='test-pass-123',
        )
        self.holder = []
        self.server = _site(self.holder)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.origin = f'http://{host}:{port}'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def _crawl(self, path, **crawl):
        config = {
            'mode': 'site',
            'allowed_hosts': ['127.0.0.1'],
            'max_depth': 2,
            'max_pages': 10,
            'preview_pages': 3,
        }
        config.update(crawl)
        job = create_job(
            user_public_id=self.user.public_id,
            title='Обход',
            seed_urls=[f'{self.origin}{path}'],
            fields=FIELDS,
            crawl=config,
        )
        run = explore_job(job=job, user_public_id=self.user.public_id)
        publish_ready(str(run.public_id))
        approve_run(run=run, user_public_id=self.user.public_id)
        run = start_run(run=run)
        publish_ready(str(run.public_id))
        run.refresh_from_db()
        return run

    def test_cycle_fragment_and_outside_host_do_not_duplicate(self):
        run = self._crawl('/a')
        self.assertEqual(run.status, RUN_COMPLETED)
        done = set(CrawlURL.objects.filter(run=run, state='done').values_list('normalized_url', flat=True))
        self.assertEqual(done, {f'{self.origin}/a', f'{self.origin}/b'})
        self.assertTrue(CrawlURL.objects.filter(run=run, skip_reason='host').exists())
        self.assertTrue(CrawlURL.objects.filter(run=run, skip_reason='document').exists())
        self.assertEqual(Task.objects.filter(run=run, kind='fetch', status='succeeded').count(), 2)
        self.assertEqual(Record.objects.filter(run=run).count(), 2)

    def test_catalog_is_not_extracted_and_pagination_is_kept(self):
        run = self._crawl(
            '/cat',
            extract_on=[{'kind': 'prefix', 'pattern': '/item/'}],
            max_pages=6,
        )
        self.assertEqual(run.status, RUN_COMPLETED)
        titles = sorted(Record.objects.filter(run=run).values_list('values__title__normalized', flat=True))
        self.assertEqual(titles, ['Item'])
        urls = set(CrawlURL.objects.filter(run=run, state='done').values_list('normalized_url', flat=True))
        self.assertIn(f'{self.origin}/cat?page=2', urls)
        self.assertIn(f'{self.origin}/item?id=9', urls)
        self.assertIn(f'{self.origin}/item/1', urls)

    def test_redirect_is_not_fetched_twice(self):
        run = self._crawl('/go', max_pages=5)
        self.assertEqual(run.status, RUN_COMPLETED)
        self.assertEqual(Task.objects.filter(run=run, kind='fetch', status='succeeded').count(), 1)
        self.assertTrue(CrawlURL.objects.filter(run=run, normalized_url=f'{self.origin}/quiet', skip_reason='redirect').exists())

    def test_same_url_is_remembered_once(self):
        job = create_job(
            user_public_id=self.user.public_id,
            title='Дубль',
            seed_urls=[f'{self.origin}/a'],
            fields=FIELDS,
            crawl={'mode': 'site', 'allowed_hosts': ['127.0.0.1'], 'max_pages': 10},
        )
        run = explore_job(job=job, user_public_id=self.user.public_id)
        run.status = RUN_RUNNING
        run.save(update_fields=['status'])
        first = remember(run, f'{self.origin}/only', base=self.origin, depth=0, parent=None)
        second = remember(run, f'{self.origin}/only#x', base=self.origin, depth=0, parent=None)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(Task.objects.filter(run=run, idempotency_key=f'fetch:{first.url_key}').count(), 1)

    def test_pause_keeps_frontier_and_resume_finishes(self):
        job = create_job(
            user_public_id=self.user.public_id,
            title='Пауза',
            seed_urls=[f'{self.origin}/a'],
            fields=FIELDS,
            crawl={'mode': 'site', 'allowed_hosts': ['127.0.0.1'], 'max_pages': 5, 'max_depth': 1},
        )
        run = explore_job(job=job, user_public_id=self.user.public_id)
        publish_ready(str(run.public_id))
        approve_run(run=run, user_public_id=self.user.public_id)
        run = start_run(run=run)
        run = pause_run(run=run)
        run.refresh_from_db()
        self.assertEqual(run.status, 'paused')
        self.assertGreater(run.crawl_urls.count(), 0)
        run = resume_run(run=run)
        publish_ready(str(run.public_id))
        run.refresh_from_db()
        self.assertEqual(run.status, RUN_COMPLETED)

    def test_cancel_blocks_late_discovery(self):
        run = self._crawl('/a', max_depth=0, max_pages=1)
        run.status = 'cancelled'
        run.save(update_fields=['status'])
        before = Task.objects.filter(run=run).count()
        snapshot = run.snapshots.get()
        discover(run, snapshot, b'<a href="/new-page">x</a>')
        self.assertEqual(Task.objects.filter(run=run).count(), before)

    def test_open_discovery_prevents_early_completion(self):
        run = self._crawl('/a', max_depth=0, max_pages=1)
        run.status = RUN_RUNNING
        run.finished_at = None
        run.save(update_fields=['status', 'finished_at'])
        task = Task.objects.create(
            run=run,
            kind='crawl',
            status=TASK_PENDING,
            idempotency_key='crawl:still-open',
            payload={},
            fencing_token=1,
        )
        _finish_if_idle(run)
        run.refresh_from_db()
        self.assertEqual(run.status, RUN_RUNNING)
        task.status = 'succeeded'
        task.save(update_fields=['status'])
        _finish_if_idle(run)
        run.refresh_from_db()
        self.assertEqual(run.status, RUN_COMPLETED)

    def test_lost_worker_during_crawl_keeps_the_run(self):
        run = self._crawl('/a', max_depth=0, max_pages=1)
        run.status = RUN_RUNNING
        run.finished_at = None
        run.save(update_fields=['status', 'finished_at'])
        task = Task.objects.create(
            run=run,
            kind='crawl',
            status='leased',
            idempotency_key='crawl:leased',
            payload={},
            fencing_token=1,
            lease_until=timezone.now() - timedelta(seconds=5),
        )
        recover_expired_leases()
        task.refresh_from_db()
        run.refresh_from_db()
        self.assertEqual(task.status, TASK_PENDING)
        self.assertEqual(run.status, RUN_RUNNING)

    def test_http_503_retries_without_a_second_manual_start(self):
        job = create_job(
            user_public_id=self.user.public_id,
            title='Повтор',
            seed_urls=[f'{self.origin}/flaky'],
            fields=FIELDS,
        )
        run = explore_job(job=job, user_public_id=self.user.public_id)
        publish_ready(str(run.public_id))
        run.refresh_from_db()
        self.assertEqual(run.status, 'awaiting_confirmation')
        self.assertEqual(run.snapshots.count(), 1)


class AccessTests(TestCase):
    def test_another_user_does_not_see_the_job_or_run(self):
        from rest_framework.test import APIClient

        User = get_user_model()
        owner = User.objects.create_user('scout-owner', 'scout-owner@example.com', 'test-pass-123')
        other = User.objects.create_user('scout-other', 'scout-other@example.com', 'test-pass-123')
        job = create_job(
            user_public_id=owner.public_id,
            title='Чужое',
            seed_urls=['https://example.com/page'],
            fields=FIELDS,
        )
        run = explore_job(job=job, user_public_id=owner.public_id)
        client = APIClient()
        client.force_authenticate(user=other)
        self.assertEqual(client.get(f'/api/scout/jobs/{job.public_id}/').status_code, 404)
        self.assertEqual(client.get(f'/api/scout/runs/{run.public_id}/').status_code, 404)
        self.assertEqual(client.post(f'/api/scout/runs/{run.public_id}/export/', {'kind': 'json'}).status_code, 403)
