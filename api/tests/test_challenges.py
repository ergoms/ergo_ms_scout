"""Заглушка проверки не становится снимком/записью, включая успешный HTTP 200."""

import gzip
import io
import os
import threading
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from urllib.error import HTTPError
from uuid import uuid4

from django.test import SimpleTestCase, TestCase

from ..models import VisualSnapshot
from ..services.challenges import raise_for_challenge
from ..services.errors import FetchError
from ..services.http_fetch import fetch_html, fetch_text, fetch_xml
from ..services.runs import approve_run, create_job, explore_job, start_run
from ..services.tasks_exec import publish_ready, run_one
from ..services.visual_snapshots import load_snapshot

CF = b'<title>Just a moment...</title><script src="/cdn-cgi/challenge-platform/h/g/orchestrate/chl_page/v1"></script>'
YANDEX = b'<title>CAPTCHA</title><script src="https://smartcaptcha.cloud.yandex.ru/captcha.js"></script><div class="smart-captcha"></div>'


def headers(**values):
    result = Message()
    for name, value in values.items():
        result[name.replace('_', '-')] = value
    return result


class ChallengeDetectionTests(SimpleTestCase):
    def test_gate_pages_and_header_are_recognized(self):
        for body, url, values, code in (
            (CF, 'https://example.com/', {}, 'cloudflare_challenge'),
            (b'not HTML', 'https://example.com/', {'CF_Mitigated': 'challenge'}, 'cloudflare_challenge'),
            (YANDEX, 'https://example.com/', {}, 'yandex_captcha'),
            (b'<form action="/checkcaptcha"><input name="rep"></form>', 'https://yandex.ru/showcaptcha', {}, 'yandex_captcha'),
            (b'<title>CAPTCHA</title><form><input name="captcha"><img src="/c.png"></form>', 'https://example.com/', {}, 'captcha_required'),
        ):
            with self.subTest(code=code, url=url):
                with self.assertRaises(FetchError) as error:
                    raise_for_challenge(headers=headers(**values), url=url, body=body)
                self.assertEqual(error.exception.code, code)
                self.assertFalse(error.exception.retryable)

    def test_widgets_documentation_and_ordinary_403_are_not_gates(self):
        for body, url in (
            (b'<title>Catalog</title>' + YANDEX.split(b'</title>', 1)[1], 'https://example.com/'),
            (b'<title>SmartCaptcha integration guide</title>' + YANDEX.split(b'</title>', 1)[1], 'https://example.com/'),
            (b'<title>Login</title><div class="cf-turnstile"></div>', 'https://example.com/'),
            (b'<title>Cloudflare CAPTCHA documentation</title><pre>&lt;script src="/cdn-cgi/challenge-platform/"&gt;</pre>', 'https://example.com/'),
            (b'<title>Error 403</title><p>Forbidden</p>', 'https://checko.ru/'),
            (b'<form action="https://yandex.ru.evil.test/checkcaptcha"><input name="rep"></form>', 'https://yandex.ru/showcaptcha'),
            (b'<title>CAPTCHA</title><script src="https://smartcaptcha.cloud.yandex.ru.evil.test/captcha.js"></script>', 'https://example.com/'),
            (b'<title>Article</title><p>captcha Cloudflare Yandex</p>', 'https://example.com/'),
        ):
            raise_for_challenge(headers=headers(), url=url, body=body)
        raise_for_challenge(headers=headers(), url='https://example.com/', body=CF, content_type='application/xml')

    def test_http_error_body_is_bounded_closed_and_does_not_leak_tokens(self):
        for status, body, extra, code in (
            (403, CF, {}, 'cloudflare_challenge'),
            (503, gzip.compress(YANDEX), {'Content_Encoding': 'gzip'}, 'yandex_captcha'),
            (200, b'', {'CF_Mitigated': 'challenge'}, 'cloudflare_challenge'),
            (403, b'<title>Error 403</title>', {}, 'http_403'),
        ):
            with self.subTest(status=status, code=code):
                response = HTTPError('https://example.com/?secret=sentinel', status, 'sentinel',
                                     headers(Content_Type='text/html', **extra), io.BytesIO(body))
                with patch('modules.scout.api.services.http_fetch.urlrequest.build_opener') as opener:
                    opener.return_value.open.side_effect = response
                    with self.assertRaises(FetchError) as failure:
                        fetch_html('https://example.com/', timeout_seconds=1, max_bytes=10000)
                self.assertEqual(failure.exception.code, code)
                self.assertNotIn('sentinel', str(failure.exception))
                self.assertTrue(response.closed)


class _Source(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.paths.append(self.path)
        gate = self.path.startswith('/gate')
        body = CF if gate else b'<h1>Catalog</h1><a href="/gate">Next</a><a href="/later">Later</a>'
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class ChallengeFlowTests(TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), _Source)
        self.server.paths = []
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        host, port = self.server.server_address
        self.url = f'http://{host}:{port}'
        self.owner = uuid4()
        self.enterContext(patch.dict(os.environ, {'SCOUT_TASK_RUNNER': 'inline',
                                                'SCOUT_TRUSTED_FETCH_ENDPOINTS': f'{host}:{port}'}))

    def _job(self, path, crawl=None):
        return create_job(user_public_id=self.owner, title='Challenge test', seed_urls=[self.url + path],
                          fields=[{'name': 'title', 'kind': 'css', 'selector': 'h1'}], crawl=crawl)

    def test_preview_failure_has_no_snapshot_records_or_retry(self):
        run = explore_job(job=self._job('/gate'), user_public_id=self.owner)
        publish_ready(str(run.public_id))
        run.refresh_from_db()
        self.assertEqual((run.status, run.stop_reason), ('failed', 'cloudflare_challenge'))
        self.assertFalse(run.snapshots.exists())
        self.assertFalse(run.records.exists())
        task = run.tasks.get()
        self.assertEqual((task.status, task.attempt_count, task.error_code), ('failed', 1, 'cloudflare_challenge'))
        run_one(str(task.public_id), task.fencing_token)
        self.assertEqual(self.server.paths, ['/gate'])

    def test_partial_collection_stops_pending_fetches_and_keeps_real_records(self):
        run = explore_job(job=self._job('/catalog', {'mode': 'site', 'parallel_fetch': 1}), user_public_id=self.owner)
        publish_ready(str(run.public_id))
        run.refresh_from_db()
        approve_run(run=run, user_public_id=self.owner)
        start_run(run=run)
        publish_ready(str(run.public_id))
        run.refresh_from_db()
        self.assertEqual((run.status, run.stop_reason), ('completed_with_errors', 'cloudflare_challenge'))
        self.assertEqual(run.records.count(), 1)
        self.assertFalse(run.snapshots.filter(source_url__contains='/gate').exists())
        self.assertFalse(run.tasks.filter(status__in=['pending', 'retry_wait', 'leased']).exists())
        self.assertNotIn('/later', self.server.paths)
        count = run.records.count()
        publish_ready(str(run.public_id))
        self.assertEqual(run.records.count(), count)

    def test_visual_xml_and_robots_do_not_accept_a_gate(self):
        snapshot = VisualSnapshot.objects.create(user_public_id=self.owner, source_url=self.url + '/gate')
        load_snapshot(snapshot.public_id)
        snapshot.refresh_from_db()
        self.assertEqual((snapshot.status, snapshot.error_code), ('failed', 'cloudflare_challenge'))
        self.assertEqual(bytes(snapshot.body), b'')
        from ..services.public_http import _PublicHTTPHandler
        from urllib.request import ProxyHandler
        for fetch in (fetch_xml, fetch_text):
            with self.assertRaises(FetchError) as failure:
                fetch(self.url + '/gate', timeout_seconds=2, max_bytes=10000,
                      handlers=(ProxyHandler({}), _PublicHTTPHandler()))
            self.assertEqual(failure.exception.code, 'cloudflare_challenge')
