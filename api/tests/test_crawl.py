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
        remember(run, target, base=self.origin, depth=2, parent=None)
        leaf_row = run.crawl_urls.get(normalized_url=leaf)
        self.assertEqual(run.crawl_urls.get(normalized_url=target).depth, 2)
        self.assertEqual(leaf_row.depth, 3)
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
        from modules.scout.api.services.http_fetch import fetch_html
        from modules.scout.api.services.url_norm import host_allowed
        from modules.scout.api.services.errors import FetchError

        with self.assertRaises(FetchError) as caught:
            fetch_html(f'{self.origin}/out', timeout_seconds=5, max_bytes=1000,
                       url_allowed=lambda url: host_allowed(url, ['127.0.0.1']))
        self.assertEqual(caught.exception.code, 'redirect_scope')


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
