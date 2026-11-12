"""Обход одного сайта: циклы, дубли, фильтры, пауза и потеря исполнителя."""

import gzip
import os
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from time import sleep
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from modules.scout.api.models import CrawlURL, Record, Run, Task
from modules.scout.api.services.constants import (
    RUN_COMPLETED, RUN_COMPLETED_WITH_ERRORS, RUN_FAILED, RUN_RUNNING,
    TASK_CRAWL, TASK_EXTRACT, TASK_FETCH, TASK_PENDING, TASK_SITEMAP,
)
from modules.scout.api.services.crawl_budget import reconcile
from modules.scout.api.services.errors import FetchError
from modules.scout.api.services.frontier import discover, remember
from modules.scout.api.services.public_http import fetch_public_html
from modules.scout.api.services.runs import approve_run, create_job, explore_job, pause_run, resume_run, start_run, _finish_if_idle
from modules.scout.api.services.tasks_exec import _claim, _fetch, publish_ready, recover_expired_leases, run_one
from modules.scout.api.tasks import execute_scout_task

FIELDS = [{'name': 'title', 'kind': 'css', 'selector': 'h1', 'scope': 'page', 'many': False}]


def _site(port_holder):
    class Handler(BaseHTTPRequestHandler):
        hits = {'/flaky': 0}

        def do_GET(self):
            host = self.headers.get('Host')
            path = self.path.split('?', 1)[0]
            if path == '/missing.xml':
                self.send_response(404)
                self.end_headers()
                return
            if path.endswith(('.xml', '.xml.gz')):
                Handler.hits[path] = Handler.hits.get(path, 0) + 1
                urls = (f'http://{host}/item/1', f'http://{host}/item/1#again',
                        f'http://localhost:{self.server.server_port}/item/0', f'http://{host}/outside',
                        'http://127.0.0.1:1/item/private', f'http://{host}/item/2', f'http://{host}/item/3')
                samples = {
                    '/sitemap.xml.gz': '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                    + ''.join(f'<url><loc>{url}</loc></url>' for url in urls) + '</urlset>',
                    '/invalid.xml': '<urlset>',
                    '/index.xml': '<sitemapindex/>',
                    '/entity.xml': '<!DOCTYPE urlset [<!ENTITY x SYSTEM "file:///no-such-file">]>'
                    '<urlset><url><loc>&x;</loc></url></urlset>',
                    '/empty.xml': '<urlset><url><loc>/relative</loc></url></urlset>',
                }
                body = samples[path].encode()
                compressed = path.endswith('.gz')
                body = gzip.compress(body) if compressed else body
                self.send_response(200)
                self.send_header('Content-Type', 'application/gzip' if compressed else 'application/xml')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path == '/out':
                self.send_response(302)
                self.send_header('Location', f'http://localhost:{self.server.server_port}/quiet')
                self.end_headers()
                return
            if path == '/go':
                self.send_response(302)
                self.send_header('Location', f'http://{host}/quiet')
                self.end_headers()
                return
            if path == '/redir':
                self.send_response(302)
                self.send_header('Location', f'http://{host}/land')
                self.end_headers()
                return
            if path == '/chain':
                self.send_response(302)
                self.send_header('Location', f'http://{host}/chain-mid')
                self.end_headers()
                return
            if path == '/visit-start':
                self.send_response(302)
                self.send_header('Location', f'http://{host}/chain-mid')
                self.end_headers()
                return
            if path == '/chain-mid':
                self.send_response(302)
                self.send_header('Location', f'http://{host}/chain-land')
                self.end_headers()
                return
            if path == '/flaky':
                Handler.hits['/flaky'] += 1
                if Handler.hits['/flaky'] == 1:
                    self.send_response(503)
                    self.end_headers()
                    return
            if path == '/slow-body':
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', '10')
                self.end_headers()
                for _ in range(10):
                    try:
                        self.wfile.write(b'0')
                    except OSError:
                        return
                    sleep(0.1)
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
    if path == '/other-port':
        return '<h1>Port</h1><a href="http://127.0.0.1:1/private">private</a>'
    if path.startswith('/storm'):
        return '<h1>Storm</h1>' + ''.join(f'<a href="/{i}.pdf">skip</a>' for i in range(60))
    if path.startswith('/wide'):
        return '<h1>Wide</h1>' + ''.join(f'<a href="/item/{i}">item</a>' for i in range(10))
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
    if path == '/redirect-start':
        return '<html><body><h1>Start</h1><a href="/hop">hop</a></body></html>'
    if path.startswith('/hop'):
        return '<html><body><h1>Hop</h1><a href="/redir">r</a></body></html>'
    if path.startswith('/land'):
        return '<html><body><h1>Land</h1><a href="/next">n</a></body></html>'
    if path.startswith('/next'):
        return '<html><body><h1>Next</h1></body></html>'
    if path.startswith('/chain-start'):
        return '<html><body><h1>Chain</h1><a href="/chain">c</a></body></html>'
    if path.startswith('/chain-land'):
        return '<html><body><h1>ChainLand</h1><a href="/chain-next">n</a></body></html>'
    if path.startswith('/chain-next'):
        return '<html><body><h1>ChainNext</h1></body></html>'
    if path.startswith('/diamond'):
        return '<html><body><h1>Diamond</h1><a href="/long">l</a><a href="/short">s</a></body></html>'
    if path.startswith('/long'):
        return '<html><body><h1>Long</h1><a href="/dmid">m</a></body></html>'
    if path.startswith('/dmid'):
        return '<html><body><h1>Mid</h1><a href="/target">t</a></body></html>'
    if path.startswith('/short'):
        return '<html><body><h1>Short</h1><a href="/target">t</a></body></html>'
    if path.startswith('/target'):
        return '<html><body><h1>Target</h1><a href="/leaf">f</a></body></html>'
    if path.startswith('/leaf'):
        return '<html><body><h1>Leaf</h1></body></html>'
    return '<html><body><h1>Other</h1></body></html>'


class CrawlTests(TestCase):
    def setUp(self):
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
        self.enterContext(patch.dict(os.environ, {
            'SCOUT_TASK_RUNNER': 'inline',
            'SCOUT_TRUSTED_FETCH_ENDPOINTS': f'{host}:{port}',
        }))

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

    def _open(self, path, **crawl):
        run = self._crawl(path, **crawl)
        run.status = RUN_RUNNING
        run.finished_at = None
        run.stop_reason = ''
        run.save(update_fields=['status', 'finished_at', 'stop_reason'])
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

    def test_sitemap_api_uses_approved_plan_and_existing_frontier(self):
        client = APIClient()
        client.force_authenticate(self.user)
        sitemap = f'{self.origin}/sitemap.xml.gz'
        request = {
            'title': 'Обход по карте', 'seed_urls': [f'{self.origin}/quiet'], 'fields': FIELDS,
            'crawl': {'mode': 'site', 'sitemap_url': sitemap, 'max_pages': 2,
                      'visit': [{'kind': 'prefix', 'pattern': '/item/'}]},
        }
        response = client.post('/api/scout/jobs/', request, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['revision']['crawl']['sitemap_url'], sitemap)
        job_id = response.data['public_id']
        response = client.post(f'/api/scout/jobs/{job_id}/explore/')
        self.assertEqual(response.status_code, 202, response.data)
        run_id = response.data['public_id']
        publish_ready(run_id)
        endpoint = f'/api/scout/runs/{run_id}/'
        self.assertEqual(client.post(endpoint + 'start/').status_code, 409)
        self.assertNotIn('/sitemap.xml.gz', self.server.RequestHandlerClass.hits)
        self.assertEqual(client.post(endpoint + 'approve/').status_code, 200)
        self.assertEqual(client.post(endpoint + 'start/').status_code, 202)
        publish_ready(run_id)
        run = Run.objects.get(public_id=run_id)
        self.assertEqual(run.status, RUN_COMPLETED)
        self.assertEqual(run.stop_reason, 'max_pages')
        self.assertEqual(run.tasks.get(kind=TASK_SITEMAP).status, 'succeeded')
        self.assertEqual(self.server.RequestHandlerClass.hits['/sitemap.xml.gz'], 1)
        self.assertEqual(set(run.crawl_urls.filter(state='done').values_list('normalized_url', 'depth')), {
            (f'{self.origin}/item/1', 0), (f'{self.origin}/item/2', 0),
        })
        self.assertEqual(run.tasks.filter(kind=TASK_FETCH, payload__has_key='crawl_public_id').count(), 2)
        self.assertEqual(run.records.count(), 2)
        self.assertFalse(run.snapshots.filter(source_url=sitemap).exists())

    def test_bad_or_inaccessible_sitemap_fails_without_starting_site_fetches(self):
        cases = {'/missing.xml': 'http_404', '/invalid.xml': 'sitemap_invalid', '/entity.xml': 'sitemap_invalid',
                 '/index.xml': 'sitemap_empty', '/empty.xml': 'sitemap_empty', '/out': 'redirect_scope'}
        for path, code in cases.items():
            with self.subTest(code=code, path=path):
                run = self._crawl('/quiet', sitemap_url=f'{self.origin}{path}')
                self.assertEqual(run.status, RUN_FAILED)
                task = run.tasks.get(kind=TASK_SITEMAP)
                self.assertEqual(task.error_code, code)
                self.assertEqual(run.error_summary, task.error_message)
                self.assertFalse(run.crawl_urls.exists())

    def test_discovery_batches_keep_link_order_and_limits(self):
        run = self._open('/quiet', max_pages=3, frontier_cap=6)
        snapshot = run.snapshots.get()
        hrefs = ('/new-a', '/new-a#duplicate', '/guide.pdf', '/new-b', '/over-limit',
                 'http://evil.test/no', '/over-frontier', '/new-b#duplicate')
        body = ''.join(f'<a href="{href}">link</a>' for href in hrefs).encode()

        with patch('modules.scout.api.services.frontier._DISCOVERY_BATCH_SIZE', 3):
            with CaptureQueriesContext(connection) as queries:
                discover(run, snapshot, body)
            discover(run, snapshot, body)

        self.assertEqual(sum('INSERT INTO "scout_crawlurl"' in query['sql'] for query in queries), 2)
        self.assertEqual(sum('INSERT INTO "scout_task"' in query['sql'] for query in queries), 2)
        rows = list(run.crawl_urls.exclude(snapshot=snapshot).order_by('pk'))
        self.assertEqual([(row.normalized_url, row.state, row.skip_reason) for row in rows], [
            (f'{self.origin}/new-a', 'active', ''),
            ('/guide.pdf', 'skipped', 'document'),
            (f'{self.origin}/new-b', 'active', ''),
            (f'{self.origin}/over-limit', 'skipped', 'max_pages'),
            ('http://evil.test/no', 'skipped', 'host'),
        ])
        payloads = list(
            run.tasks.filter(kind=TASK_FETCH, status=TASK_PENDING).order_by('pk').values_list('payload', flat=True)
        )
        self.assertEqual(payloads, [
            {'url': row.normalized_url, 'crawl_public_id': str(row.public_id)}
            for row in (rows[0], rows[2])
        ])
        run.refresh_from_db()
        self.assertEqual(run.stop_reason, 'max_pages')

    def test_discovery_batch_waits_to_schedule_fetches_during_pause(self):
        run = self._open('/quiet')
        run.status = 'pausing'
        run.save(update_fields=['status'])
        discover(run, run.snapshots.get(), b'<a href="/new-a">A</a><a href="/new-b">B</a>')

        self.assertEqual(list(run.crawl_urls.filter(state='queued').order_by('pk').values_list('normalized_url', flat=True)), [
            f'{self.origin}/new-a', f'{self.origin}/new-b',
        ])
        self.assertFalse(run.tasks.filter(kind=TASK_FETCH, status=TASK_PENDING).exists())

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
        self.assertEqual(run.snapshots.filter(has_crawl_links=False).count(), 2)
        self.assertEqual(run.tasks.filter(kind=TASK_CRAWL).count(), 2)

    def test_link_free_seed_reuses_preview_without_crawl_task(self):
        run = self._crawl('/quiet')
        self.assertEqual(run.status, RUN_COMPLETED)
        self.assertIs(run.snapshots.get().has_crawl_links, False)
        self.assertEqual(run.tasks.filter(kind=TASK_FETCH).count(), 1)
        self.assertFalse(run.tasks.filter(kind=TASK_CRAWL).exists())
        self.assertEqual(run.records.count(), 1)

    def test_unknown_link_status_keeps_crawl_for_reused_snapshot(self):
        with patch('modules.scout.api.services.page_links.has_crawl_links', return_value=None):
            run = self._crawl('/quiet')
        self.assertEqual(run.status, RUN_COMPLETED)
        self.assertIsNone(run.snapshots.get().has_crawl_links)
        self.assertEqual(run.tasks.filter(kind=TASK_CRAWL).count(), 1)
        self.assertEqual(run.records.count(), 1)

    def test_redirect_is_not_fetched_twice(self):
        run = self._crawl('/go', max_pages=5)
        self.assertEqual(run.status, RUN_COMPLETED)
        self.assertEqual(Task.objects.filter(run=run, kind='fetch', status='succeeded').count(), 1)
        self.assertTrue(CrawlURL.objects.filter(run=run, normalized_url=f'{self.origin}/quiet', skip_reason='redirect').exists())

    def test_redirect_alias_uses_one_html_page_from_budget(self):
        run = self._crawl('/redir', max_pages=2)

        self.assertEqual(run.status, RUN_COMPLETED)
        self.assertEqual(run.stop_reason, '')
        self.assertEqual(run.snapshots.count(), 2)
        self.assertEqual(run.records.count(), 2)
        self.assertEqual(run.crawl_urls.filter(state='done').count(), 3)

    def test_parallel_redirect_keeps_one_snapshot_and_releases_page_reservation(self):
        target = f'{self.origin}/target'
        response = {'body': b'<h1>Target</h1><a href="/next">Next</a>', 'final_url': target,
                    'status': 200, 'content_type': 'text/html', 'charset': 'utf-8'}
        for first, second in (('/go', '/target'), ('/target', '/go')):
            with self.subTest(first=first):
                run = self._open('/quiet', max_pages=3)
                claimed = {}
                for path in ('/go', '/target'):
                    url = f'{self.origin}{path}'
                    remember(run, url, base=self.origin, depth=1, parent=None)
                    task = run.tasks.get(payload__url=url)
                    claimed[path] = _claim(str(task.public_id), task.fencing_token)
                    self.assertIsNotNone(claimed[path])

                with patch('modules.scout.api.services.tasks_exec.fetch_public_html', return_value=response):
                    _fetch(*claimed[first])
                    if first == '/target':
                        crawl_task = run.tasks.get(kind=TASK_CRAWL, status=TASK_PENDING)
                        run_one(str(crawl_task.public_id), crawl_task.fencing_token)
                        self.assertEqual(run.crawl_urls.get(normalized_url=f'{self.origin}/next').skip_reason, 'max_pages')
                    _fetch(*claimed[second])

                publish_ready(run.public_id)
                run.refresh_from_db()
                self.assertEqual(run.status, RUN_COMPLETED)
                self.assertEqual(run.stop_reason, '')
                snapshot = run.snapshots.get(final_url=target)
                self.assertEqual(run.crawl_urls.filter(snapshot=snapshot, state='done').count(), 2)
                self.assertEqual(run.records.filter(snapshot=snapshot).count(), 1)
                self.assertEqual(run.records.count(), 3)
                self.assertEqual(run.crawl_urls.get(normalized_url=f'{self.origin}/next').state, 'done')

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

    def test_reconcile_reads_active_tasks_in_one_query(self):
        run = self._open('/quiet')
        snapshot = run.snapshots.get()
        rows = {}
        for status in ('missing', 'pending', 'leased', 'succeeded', 'failed', 'cancelled'):
            rows[status] = CrawlURL.objects.create(
                run=run, state='active', url_key=status, original_url=f'{self.origin}/{status}',
            )
            if status != 'missing':
                Task.objects.create(
                    run=run, kind='fetch', status=status, idempotency_key=f'fetch:{status}',
                    payload={'url': snapshot.source_url}, error_code='fetch_error' if status == 'failed' else '',
                )
        other = Run.objects.create(job_revision=run.job_revision, user_public_id=run.user_public_id, status='cancelled')
        Task.objects.create(run=other, kind='fetch', status='leased', idempotency_key='fetch:missing')

        with (
            patch('modules.scout.api.services.frontier.note_fetch') as note_fetch,
            patch('modules.scout.api.services.frontier.release_queued') as release_queued,
            CaptureQueriesContext(connection) as queries,
        ):
            reconcile(run)

        task_reads = [entry for entry in queries if entry['sql'].lstrip().upper().startswith('SELECT ')
                      and 'scout_task' in entry['sql']]
        self.assertEqual(len(task_reads), 1)
        note_fetch.assert_called_once_with(run, snapshot)
        release_queued.assert_called_once_with(run)
        for status, state, reason in (
            ('missing', 'queued', ''), ('pending', 'active', ''), ('leased', 'active', ''),
            ('failed', 'failed', 'fetch_error'), ('cancelled', 'skipped', 'cancelled'),
        ):
            row = rows[status]
            row.refresh_from_db()
            self.assertEqual((row.state, row.skip_reason), (state, reason))

    def test_dispatch_interleaves_fetch_and_extract_and_fills_the_batch(self):
        run = self._open('/quiet')
        Task.objects.create(
            run=run, kind=TASK_EXTRACT, status=TASK_PENDING, idempotency_key='extract:later',
            next_retry_at=timezone.now() + timedelta(minutes=1),
        )
        Task.objects.create(
            run=run, kind=TASK_EXTRACT, status=TASK_PENDING, idempotency_key='extract:published',
            published_at=timezone.now(),
        )

        def tasks_of_kind(kind, count):
            return Task.objects.bulk_create([
                Task(run=run, kind=kind, status=TASK_PENDING, idempotency_key=f'{kind}:batch:{index}')
                for index in range(count)
            ])

        fetches = tasks_of_kind(TASK_FETCH, 24)
        extracts = tasks_of_kind(TASK_EXTRACT, 5)
        crawls = tasks_of_kind(TASK_CRAWL, 2)

        with patch.dict(os.environ, {'SCOUT_TASK_RUNNER': 'celery'}), patch.object(execute_scout_task, 'delay') as send:
            publish_ready(str(run.public_id))
            first_batch = [call.args[0] for call in send.call_args_list]
            send.reset_mock()
            publish_ready(str(run.public_id))
            second_batch = [call.args[0] for call in send.call_args_list]

        expected_first = [fetches[0], extracts[0], *fetches[1:19]]
        expected_second = [fetches[19], extracts[1], *fetches[20:], *extracts[2:], *crawls]
        self.assertEqual(first_batch, [str(task.public_id) for task in expected_first])
        self.assertEqual(second_batch, [str(task.public_id) for task in expected_second])

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

    def test_main_crawl_retries_a_seed_that_exhausted_preview_attempts(self):
        flaky_url = f'{self.origin}/flaky'
        job = create_job(user_public_id=self.user.public_id, title='Повтор семени',
                         seed_urls=[f'{self.origin}/quiet', flaky_url], fields=FIELDS, crawl={'mode': 'site'})
        hits = 0

        def fetch(url, **_kwargs):
            nonlocal hits
            if url == flaky_url:
                hits += 1
                if hits <= 3:
                    raise FetchError('http_503', 'Временная ошибка', retryable=True)
            return {'body': b'<h1>Title</h1>', 'final_url': url, 'status': 200, 'content_type': 'text/html'}

        with patch('modules.scout.api.services.tasks_exec.fetch_public_html', side_effect=fetch):
            run = explore_job(job=job, user_public_id=self.user.public_id)
            publish_ready(run.public_id)
            failed_preview = run.tasks.get(payload__url=flaky_url, status='failed')
            self.assertEqual(failed_preview.attempt_count, 3)
            approve_run(run=run, user_public_id=self.user.public_id)
            run = start_run(run=run)
            publish_ready(run.public_id)

        run.refresh_from_db()
        self.assertEqual(hits, 4)
        self.assertEqual(run.status, RUN_COMPLETED)
        self.assertEqual(run.error_summary, '')
        self.assertEqual(run.records.count(), 2)
        self.assertEqual(run.tasks.get(payload__url=flaky_url, status='succeeded').attempt_count, 1)
        failed_preview.refresh_from_db()
        self.assertEqual(failed_preview.status, 'failed')
        self.assertEqual(failed_preview.attempts.count(), 3)

    def test_last_html_response_after_crawl_deadline_is_discarded(self):
        run = self._open('/quiet', max_seconds=10)
        late_url = f'{self.origin}/late'
        remember(run, late_url, base=self.origin, depth=1, parent=None)
        task = run.tasks.get(payload__url=late_url)
        clock = run.started_at

        def fetch(url, **_kwargs):
            nonlocal clock
            clock += timedelta(seconds=11)
            return {'body': b'<h1>Late</h1>', 'final_url': url, 'status': 200, 'content_type': 'text/html'}

        with (patch.object(timezone, 'now', side_effect=lambda: clock),
              patch('modules.scout.api.services.tasks_exec.fetch_public_html', side_effect=fetch)):
            run_one(str(task.public_id), task.fencing_token)

        run.refresh_from_db()
        task.refresh_from_db()
        self.assertEqual((run.status, run.stop_reason), (RUN_COMPLETED, 'time'))
        self.assertEqual(task.status, 'cancelled')
        self.assertEqual(task.attempts.get().status, 'cancelled')
        self.assertEqual(run.crawl_urls.get(normalized_url=late_url).skip_reason, 'time')
        self.assertEqual(run.snapshots.count(), 1)
        self.assertEqual(run.records.count(), 1)

    def test_sitemap_response_after_crawl_deadline_does_not_open_frontier(self):
        sitemap = f'{self.origin}/sitemap.xml'
        job = create_job(user_public_id=self.user.public_id, title='Медленная карта',
                         seed_urls=[f'{self.origin}/quiet'], fields=FIELDS,
                         crawl={'mode': 'site', 'sitemap_url': sitemap, 'max_seconds': 10})
        run = explore_job(job=job, user_public_id=self.user.public_id)
        publish_ready(run.public_id)
        approve_run(run=run, user_public_id=self.user.public_id)
        run = start_run(run=run)
        task = run.tasks.get(kind=TASK_SITEMAP)
        clock = run.started_at

        def fetch(url, **_kwargs):
            nonlocal clock
            clock += timedelta(seconds=11)
            body = f'<urlset><url><loc>{self.origin}/late</loc></url></urlset>'.encode()
            return {'body': body, 'final_url': url, 'status': 200, 'content_type': 'application/xml'}

        with (patch.object(timezone, 'now', side_effect=lambda: clock),
              patch('modules.scout.api.services.tasks_exec.fetch_public_xml', side_effect=fetch)):
            run_one(str(task.public_id), task.fencing_token)

        run.refresh_from_db()
        task.refresh_from_db()
        self.assertEqual((run.status, run.stop_reason), (RUN_COMPLETED, 'time'))
        self.assertEqual(task.status, 'cancelled')
        self.assertFalse(run.crawl_urls.exists())
        self.assertFalse(run.records.exists())

    def test_http_deadline_bounds_a_body_that_arrives_in_small_chunks(self):
        url = f'{self.origin}/slow-body'
        self.assertEqual(fetch_public_html(url, timeout_seconds=0.3, max_bytes=100)['body'], b'0' * 10)
        with self.assertRaises(FetchError) as caught:
            fetch_public_html(url, timeout_seconds=0.3, max_bytes=100, whole_timeout=True)
        self.assertEqual(caught.exception.code, 'network')

    def test_skipped_urls_are_bounded_by_frontier_cap(self):
        run = self._crawl('/storm', frontier_cap=3)
        self.assertEqual(run.status, RUN_COMPLETED)
        self.assertEqual(run.crawl_urls.count(), 3)
        self.assertEqual(run.stop_reason, 'frontier')

    def test_preview_seed_still_respects_main_visit_filter(self):
        run = self._crawl('/a', visit=[{'kind': 'prefix', 'pattern': '/b'}])
        self.assertEqual(run.status, RUN_COMPLETED)
        self.assertEqual(run.records.count(), 0)
        self.assertTrue(run.crawl_urls.filter(skip_reason='visit').exists())

    def test_preview_redirect_rechecks_every_hop_against_main_visit_filter(self):
        cases = [
            ('/redir', '/land', ['/redir']),
            ('/visit-start', '/chain-land', ['/visit-start', '/chain-land']),
        ]
        for source, final, allowed in cases:
            with self.subTest(source=source):
                run = self._crawl(source, visit=[{'kind': 'prefix', 'pattern': path} for path in allowed])
                self.assertEqual(run.status, RUN_COMPLETED_WITH_ERRORS)
                self.assertEqual(run.snapshots.get().final_url, f'{self.origin}{final}')
                self.assertEqual(run.crawl_urls.get().state, 'failed')
                self.assertEqual(run.tasks.get(status='failed').error_code, 'redirect_scope')
                self.assertEqual(run.tasks.get(status='succeeded').attempt_count, 1)
                self.assertFalse(run.records.exists())
                self.assertFalse(run.tasks.filter(kind__in=(TASK_CRAWL, TASK_EXTRACT)).exists())

    def test_main_fetch_updates_cached_final_url_for_identical_html(self):
        result = {
            'body': b'<h1>Title</h1>', 'status': 200, 'content_type': 'text/html', 'charset': 'utf-8',
        }
        responses = [
            dict(result, final_url=f'{self.origin}/outside'),
            dict(result, final_url=f'{self.origin}/quiet'),
        ]
        with patch('modules.scout.api.services.tasks_exec.fetch_public_html', side_effect=responses) as fetch:
            run = self._crawl('/go', visit=[{'kind': 'prefix', 'pattern': path} for path in ['/go', '/quiet']])

        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(run.status, RUN_COMPLETED)
        snapshot = run.snapshots.get()
        self.assertEqual(snapshot.final_url, f'{self.origin}/quiet')
        self.assertEqual(run.records.get().snapshot_id, snapshot.pk)
        self.assertTrue(run.crawl_urls.filter(normalized_url=f'{self.origin}/quiet', state='done').exists())
        self.assertFalse(run.crawl_urls.filter(normalized_url=f'{self.origin}/outside').exists())

    def test_unvisited_control_preview_does_not_use_main_byte_budget(self):
        run = self._crawl('/quiet', preview_urls=[f'{self.origin}/wide'], max_total_bytes=100)
        self.assertEqual(run.status, RUN_COMPLETED)
        self.assertEqual(run.records.count(), 1)
        self.assertEqual(run.stop_reason, '')

    def test_redirect_alias_cancels_an_unclaimed_fetch(self):
        from modules.scout.api.services.frontier import _alias_redirect

        run = self._crawl('/go', max_pages=5)
        snapshot = run.snapshots.get()
        row = run.crawl_urls.get(normalized_url=f'{self.origin}/quiet')
        row.state = 'active'
        row.save(update_fields=['state'])
        task = Task.objects.create(run=run, kind='fetch', status=TASK_PENDING,
                                   idempotency_key=f'fetch:{row.url_key}',
                                   payload={'url': row.normalized_url}, fencing_token=1)
        _alias_redirect(run, snapshot)
        row.refresh_from_db()
        task.refresh_from_db()
        self.assertEqual(row.state, 'done')
        self.assertEqual(task.status, 'cancelled')

    def test_fetch_finishing_during_pause_can_resume(self):
        from modules.scout.api.services.tasks_exec import _claim, _fetch, run_one

        job = create_job(user_public_id=self.user.public_id, title='Пауза загрузки',
                         seed_urls=[f'{self.origin}/a'], fields=FIELDS,
                         crawl={'mode': 'site', 'allowed_hosts': ['127.0.0.1']})
        run = explore_job(job=job, user_public_id=self.user.public_id)
        publish_ready(str(run.public_id))
        approve_run(run=run, user_public_id=self.user.public_id)
        run = start_run(run=run)
        crawl_task = run.tasks.get(kind='crawl')
        run_one(str(crawl_task.public_id), crawl_task.fencing_token)
        fetch_task = run.tasks.get(kind='fetch', status='pending')
        task, token = _claim(str(fetch_task.public_id), fetch_task.fencing_token)
        pause_run(run=run)
        _fetch(task, token)
        run.refresh_from_db()
        self.assertEqual(run.status, 'paused')
        self.assertFalse(run.crawl_urls.filter(state='active').exists())
        resume_run(run=run)
        publish_ready(str(run.public_id))
        run.refresh_from_db()
        self.assertEqual(run.status, RUN_COMPLETED)
        self.assertEqual(run.records.count(), 2)

    def test_cancel_discards_an_inflight_fetch_result(self):
        from modules.scout.api.services.tasks_exec import _claim, _fetch, run_one
        from modules.scout.api.services.runs import cancel_run

        job = create_job(user_public_id=self.user.public_id, title='Отмена загрузки',
                         seed_urls=[f'{self.origin}/a'], fields=FIELDS,
                         crawl={'mode': 'site', 'allowed_hosts': ['127.0.0.1']})
        run = explore_job(job=job, user_public_id=self.user.public_id)
        publish_ready(str(run.public_id))
        approve_run(run=run, user_public_id=self.user.public_id)
        run = start_run(run=run)
        crawl_task = run.tasks.get(kind='crawl')
        run_one(str(crawl_task.public_id), crawl_task.fencing_token)
        fetch_task = run.tasks.get(kind='fetch', status='pending')
        task, token = _claim(str(fetch_task.public_id), fetch_task.fencing_token)
        cancel_run(run=run)
        _fetch(task, token)
        run.refresh_from_db()
        self.assertEqual(run.status, 'cancelled')
        self.assertEqual(run.snapshots.count(), 1)
        self.assertFalse(run.crawl_urls.filter(state__in=('queued', 'active')).exists())

    def test_byte_budget_applies_to_already_queued_fetches(self):
        run = self._crawl('/wide', max_total_bytes=400, max_pages=30)
        self.assertEqual(run.status, RUN_COMPLETED)
        self.assertEqual(run.stop_reason, 'bytes')
        self.assertLessEqual(sum(run.snapshots.values_list('byte_size', flat=True)), 400)

    def test_redirect_keeps_the_depth_of_the_page_that_found_it(self):
        run = self._crawl('/hop', max_depth=1, max_pages=10)
        self.assertEqual(run.status, RUN_COMPLETED)
        redir = run.crawl_urls.get(normalized_url=f'{self.origin}/redir')
        land = run.crawl_urls.get(normalized_url=f'{self.origin}/land')
        nxt = run.crawl_urls.get(normalized_url=f'{self.origin}/next')
        self.assertEqual(redir.depth, 1)
        self.assertEqual(land.depth, 1)
        self.assertEqual(land.skip_reason, 'redirect')
        self.assertEqual(nxt.depth, 2)
        self.assertEqual(nxt.state, 'skipped')
        self.assertEqual(nxt.skip_reason, 'depth')
        self.assertEqual(Task.objects.filter(run=run, kind='fetch', status='succeeded').count(), 2)
        self.assertFalse(run.snapshots.filter(source_url=f'{self.origin}/next').exists())

    def test_redirect_chain_does_not_open_the_next_level(self):
        run = self._crawl('/chain-start', max_depth=1, max_pages=10)
        self.assertEqual(run.status, RUN_COMPLETED)
        land = run.crawl_urls.get(normalized_url=f'{self.origin}/chain-land')
        nxt = run.crawl_urls.get(normalized_url=f'{self.origin}/chain-next')
        self.assertEqual(land.depth, 1)
        self.assertEqual(land.skip_reason, 'redirect')
        self.assertEqual(nxt.depth, 2)
        self.assertEqual(nxt.skip_reason, 'depth')
        self.assertEqual(Task.objects.filter(run=run, kind='fetch', status='succeeded').count(), 2)
        self.assertFalse(run.snapshots.filter(source_url=f'{self.origin}/chain-next').exists())

    def test_shorter_path_to_redirect_target_reopens_its_child_links(self):
        run = self._open('/redirect-start', max_depth=2)
        seed = run.snapshots.get(source_url=f'{self.origin}/redirect-start')
        target = run.crawl_urls.get(normalized_url=f'{self.origin}/land')
        child = run.crawl_urls.get(normalized_url=f'{self.origin}/next')
        self.assertEqual((target.depth, target.skip_reason), (2, 'redirect'))
        self.assertEqual((child.depth, child.state, child.skip_reason), (3, 'skipped', 'depth'))

        discover(run, seed, b'<a href="/land">short path</a>')

        target.refresh_from_db()
        child.refresh_from_db()
        self.assertEqual(target.depth, 1)
        self.assertEqual((child.depth, child.state, child.parent_id), (2, 'active', target.pk))
        self.assertEqual(run.tasks.filter(kind=TASK_FETCH, payload__url=child.normalized_url).count(), 1)
        self.assertFalse(run.tasks.filter(kind=TASK_FETCH, payload__url=target.normalized_url).exists())

    def test_shorter_path_requeues_a_url_skipped_for_depth(self):
        run = self._open('/quiet', max_depth=2, max_pages=10)
        deep = remember(run, f'{self.origin}/later', base=self.origin, depth=3, parent=None)
        shallow = remember(run, f'{self.origin}/later', base=self.origin, depth=2, parent=None)
        shallow.refresh_from_db()
        self.assertEqual(shallow.pk, deep.pk)
        self.assertEqual(shallow.depth, 2)
        self.assertEqual(shallow.state, 'active')
        self.assertEqual(shallow.skip_reason, '')
        self.assertEqual(Task.objects.filter(run=run, idempotency_key=f'fetch:{deep.url_key}').count(), 1)

    def test_longer_path_does_not_duplicate_a_url(self):
        run = self._open('/quiet', max_depth=2, max_pages=10)
        first = remember(run, f'{self.origin}/later', base=self.origin, depth=2, parent=None)
        second = remember(run, f'{self.origin}/later', base=self.origin, depth=3, parent=None)
        second.refresh_from_db()
        self.assertEqual(second.pk, first.pk)
        self.assertEqual(second.depth, 2)
        self.assertEqual(Task.objects.filter(run=run, idempotency_key=f'fetch:{first.url_key}').count(), 1)
        self.assertEqual(run.snapshots.filter(source_url=f'{self.origin}/later').count(), 0)

    def test_shallower_path_reopens_links_of_a_parsed_page(self):
        from modules.scout.api.models import PageSnapshot
        from modules.scout.api.services.snapshot_store import store_snapshot_bytes
        from modules.scout.api.services.url_norm import url_key

        run = self._open('/quiet', max_depth=3, max_pages=10)
        target = f'{self.origin}/target'
        leaf = f'{self.origin}/leaf'
        body = f'<html><body><h1>Target</h1><a href="/leaf">leaf</a></body></html>'.encode()
        digest, relative = store_snapshot_bytes(body)
        snapshot = PageSnapshot.objects.create(
            run=run,
            source_url=target,
            final_url=target,
            http_status=200,
            content_hash=digest,
            content_path=relative,
            byte_size=len(body),
            content_type='text/html; charset=utf-8',
            charset='utf-8',
        )
        CrawlURL.objects.create(
            run=run,
            original_url=target,
            normalized_url=target,
            url_key=url_key(target),
            depth=3,
            state='done',
            snapshot=snapshot,
        )
        CrawlURL.objects.create(
            run=run,
            original_url=leaf,
            normalized_url=leaf,
            url_key=url_key(leaf),
            depth=4,
            state='skipped',
            skip_reason='depth',
        )
        seed = run.snapshots.get(source_url=f'{self.origin}/quiet')
        discover(run, seed, b'<a href="/target">short path</a><a href="/target#duplicate">duplicate</a>')
        leaf_row = run.crawl_urls.get(normalized_url=leaf)
        self.assertEqual(run.crawl_urls.get(normalized_url=target).depth, 1)
        self.assertEqual(leaf_row.depth, 2)
        self.assertEqual(leaf_row.state, 'active')
        self.assertEqual(Task.objects.filter(run=run, idempotency_key=f'fetch:{leaf_row.url_key}').count(), 1)
        self.assertEqual(run.snapshots.filter(source_url=target).count(), 1)
        self.assertEqual(run.records.filter(snapshot=snapshot).count(), 0)

    def test_rejected_urls_stay_rejected_when_a_shorter_path_appears(self):
        run = self._open('/quiet', max_depth=2, max_pages=1)
        foreign = remember(run, 'http://evil.test/no', base=self.origin, depth=1, parent=None)
        again = remember(run, 'http://evil.test/no', base=self.origin, depth=0, parent=None)
        self.assertEqual(again.state, 'skipped')
        self.assertEqual(again.skip_reason, 'host')
        self.assertFalse(Task.objects.filter(run=run, idempotency_key=f'fetch:{foreign.url_key}').exists())
        failed = remember(run, f'{self.origin}/gone', base=self.origin, depth=3, parent=None)
        failed.state = 'failed'
        failed.skip_reason = ''
        failed.save(update_fields=['state', 'skip_reason'])
        kept = remember(run, f'{self.origin}/gone', base=self.origin, depth=1, parent=None)
        self.assertEqual(kept.state, 'failed')
        self.assertEqual(kept.depth, 3)
        blocked = remember(run, f'{self.origin}/later', base=self.origin, depth=4, parent=None)
        revived = remember(run, f'{self.origin}/later', base=self.origin, depth=1, parent=None)
        revived.refresh_from_db()
        self.assertEqual(revived.state, 'skipped')
        self.assertFalse(Task.objects.filter(run=run, idempotency_key=f'fetch:{blocked.url_key}').exists())

    def test_redirect_cannot_escape_allowed_host(self):
        from modules.scout.api.services.url_norm import host_allowed
        from modules.scout.api.services.errors import FetchError

        with self.assertRaises(FetchError) as caught:
            fetch_public_html(f'{self.origin}/out', timeout_seconds=5, max_bytes=1000,
                              url_allowed=lambda url: host_allowed(url, ['127.0.0.1']))
        self.assertEqual(caught.exception.code, 'redirect_scope')

    def test_crawl_rejects_an_untrusted_port_on_an_allowed_host(self):
        run = self._crawl('/other-port')
        blocked = run.tasks.get(kind='fetch', payload__url='http://127.0.0.1:1/private')
        self.assertEqual(blocked.error_code, 'url_blocked')
        self.assertEqual(blocked.status, 'failed')
        self.assertEqual(blocked.attempt_count, 1)


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
        path = f'/api/scout/runs/{run.public_id}/'
        self.assertEqual(client.get(path).status_code, 404)
        self.assertEqual(client.get(path, {'summary': 1}).status_code, 404)
        self.assertEqual(client.get(path + 'records/', {'limit': 1}).status_code, 404)
        self.assertEqual(client.post(path + 'export/', {'kind': 'json'}).status_code, 403)
