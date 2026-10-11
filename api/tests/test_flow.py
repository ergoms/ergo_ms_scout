"""Проверка извлечения, подтверждения, повтора задачи и чужого запуска."""

import os
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from modules.scout.api.models import Record, Task
from modules.scout.api.services.constants import (
    RUN_AWAITING,
    RUN_COMPLETED,
    RUN_RUNNING,
    TASK_LEASED,
    TASK_PENDING,
)
from modules.scout.api.services.errors import ScoutError
from modules.scout.api.services.exporting import export_run
from modules.scout.api.services.html_extract import extract_html
from modules.scout.api.services.runs import (
    approve_run,
    create_job,
    explore_job,
    pause_run,
    resume_run,
    start_run,
)
from modules.scout.api.services.tasks_exec import (
    _claim,
    _finish,
    publish_ready,
    recover_expired_leases,
    run_one,
)

FIXTURE = Path(__file__).resolve().parent.joinpath('fixtures', 'cards.html').read_bytes()
RECORD = {'kind': 'css', 'selector': '.card'}
FIELDS = [
    {'name': 'heading', 'kind': 'xpath', 'selector': '//h1', 'scope': 'page', 'many': False},
    {'name': 'title', 'kind': 'css', 'selector': '.title', 'scope': 'record', 'many': False},
    {'name': 'price', 'kind': 'css', 'selector': '.price', 'scope': 'record', 'many': False},
]


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = FIXTURE
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        return


class ExtractTests(TestCase):
    def test_repeating_cards_keep_page_heading(self):
        rows = extract_html(FIXTURE, FIELDS, record=RECORD)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['values']['heading']['normalized'], 'Станки')
        self.assertEqual(rows[1]['values']['heading']['normalized'], 'Станки')
        self.assertEqual(
            [row['values']['title']['normalized'] for row in rows],
            ['Токарный', 'Фрезерный'],
        )
        self.assertEqual(
            [row['values']['price']['normalized'] for row in rows],
            ['10', '20'],
        )


class FlowTests(TestCase):
    def setUp(self):
        os.environ['SCOUT_TASK_RUNNER'] = 'inline'
        self.user = get_user_model().objects.create_superuser(
            username='scout-admin',
            email='scout-admin@example.com',
            password='test-pass-123',
        )
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.url = f'http://{host}:{port}/cards'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def _job(self):
        return create_job(
            user_public_id=self.user.public_id,
            title='Каталог',
            seed_urls=[self.url],
            fields=FIELDS,
            record=RECORD,
        )

    def test_approval_gate_snapshot_and_idempotent_extract(self):
        job = self._job()
        run = explore_job(job=job, user_public_id=self.user.public_id)
        publish_ready(str(run.public_id))
        run.refresh_from_db()
        self.assertEqual(run.status, RUN_AWAITING)
        self.assertEqual(run.snapshots.count(), 1)

        with self.assertRaises(ScoutError) as denied:
            start_run(run=run)
        self.assertEqual(denied.exception.code, 'approval_required')

        busy = self._job()
        with self.assertRaises(ScoutError) as slot:
            explore_job(job=busy, user_public_id=self.user.public_id)
        self.assertEqual(slot.exception.code, 'slot_busy')

        approve_run(run=run, user_public_id=self.user.public_id)
        run = start_run(run=run)
        publish_ready(str(run.public_id))
        run.refresh_from_db()
        self.assertEqual(run.status, RUN_COMPLETED)
        self.assertEqual(Record.objects.filter(run=run).count(), 2)

        extract_task = run.tasks.get(kind='extract')
        extract_task.status = TASK_PENDING
        extract_task.published_at = None
        extract_task.save(update_fields=['status', 'published_at'])
        run.status = RUN_RUNNING
        run.finished_at = None
        run.save(update_fields=['status', 'finished_at'])
        publish_ready(str(run.public_id))
        self.assertEqual(Record.objects.filter(run=run).count(), 2)

    def test_stale_fencing_does_not_write_and_lost_worker_keeps_run(self):
        job = self._job()
        run = explore_job(job=job, user_public_id=self.user.public_id)
        publish_ready(str(run.public_id))
        run.refresh_from_db()
        snapshot = run.snapshots.get()
        run.status = RUN_RUNNING
        run.save(update_fields=['status'])
        task = Task.objects.create(
            run=run,
            kind='extract',
            status=TASK_PENDING,
            idempotency_key='extract:stale',
            payload={'snapshot_public_id': str(snapshot.public_id)},
            fencing_token=1,
        )
        claimed = _claim(str(task.public_id), 1)
        self.assertIsNotNone(claimed)
        before = Record.objects.count()
        _finish(task, 1, lambda: (_ for _ in ()).throw(AssertionError('stale write')))
        self.assertEqual(Record.objects.count(), before)

        task.refresh_from_db()
        task.status = TASK_LEASED
        task.lease_until = timezone.now() - timedelta(seconds=5)
        task.save(update_fields=['status', 'lease_until'])
        recover_expired_leases()
        task.refresh_from_db()
        run.refresh_from_db()
        self.assertEqual(task.status, TASK_PENDING)
        self.assertGreater(task.fencing_token, 1)
        self.assertEqual(run.status, RUN_RUNNING)

    def test_export_uses_media_path(self):
        job = self._job()
        run = explore_job(job=job, user_public_id=self.user.public_id)
        publish_ready(str(run.public_id))
        approve_run(run=run, user_public_id=self.user.public_id)
        run = start_run(run=run)
        publish_ready(str(run.public_id))
        run.refresh_from_db()

        class Storage:
            def save(self, path, content):
                self.path = path
                self.content = content
                return path

        storage = Storage()
        with patch('modules.scout.api.services.exporting.get_media_client', return_value=storage), patch(
            'modules.scout.api.services.exporting.get_signed_media_url',
            return_value='/serve/scout/exports/records.csv?signature=1&expires=1',
        ):
            _artifact, url, filename = export_run(run=run, kind='csv')
        self.assertEqual(filename, 'records.csv')
        self.assertIn('/serve/', url)
        self.assertIn('Токарный', storage.content.decode('utf-8'))

    def test_start_without_approval_is_rejected_over_http(self):
        client = APIClient()
        client.force_authenticate(user=self.user)
        with self.captureOnCommitCallbacks(execute=True):
            created = client.post(
                '/api/scout/jobs/',
                {'title': 'Каталог', 'seed_urls': [self.url], 'fields': FIELDS, 'record': RECORD},
                format='json',
            )
            self.assertEqual(created.status_code, 201, created.content)
            explored = client.post(f"/api/scout/jobs/{created.data['public_id']}/explore/", {}, format='json')
            self.assertEqual(explored.status_code, 202, explored.content)
        started = client.post(f"/api/scout/runs/{explored.data['public_id']}/start/", {}, format='json')
        self.assertEqual(started.status_code, 409)
        self.assertEqual(started.data['code'], 'approval_required')

    def test_resume_continues_without_duplicate_rows(self):
        job = self._job()
        run = explore_job(job=job, user_public_id=self.user.public_id)
        publish_ready(str(run.public_id))
        approve_run(run=run, user_public_id=self.user.public_id)
        run = start_run(run=run)
        run = pause_run(run=run)
        run.refresh_from_db()
        self.assertEqual(run.status, 'paused')
        run = resume_run(run=run)
        publish_ready(str(run.public_id))
        run.refresh_from_db()
        self.assertEqual(run.status, RUN_COMPLETED)
        self.assertEqual(Record.objects.filter(run=run).count(), 2)

    def test_resume_finishes_when_the_last_task_already_succeeded(self):
        job = self._job()
        run = explore_job(job=job, user_public_id=self.user.public_id)
        publish_ready(str(run.public_id))
        approve_run(run=run, user_public_id=self.user.public_id)
        run = start_run(run=run)
        task = run.tasks.get(kind='extract')
        claimed = _claim(str(task.public_id), task.fencing_token)
        self.assertIsNotNone(claimed)
        run.status = 'pausing'
        run.save(update_fields=['status'])
        _finish(task, claimed[1], lambda: None)
        run.refresh_from_db()
        self.assertEqual(run.status, 'paused')
        run = resume_run(run=run)
        run.refresh_from_db()
        self.assertEqual(run.status, RUN_COMPLETED)

    def test_lost_worker_during_pause_does_not_stay_pausing(self):
        job = self._job()
        run = explore_job(job=job, user_public_id=self.user.public_id)
        publish_ready(str(run.public_id))
        approve_run(run=run, user_public_id=self.user.public_id)
        run = start_run(run=run)
        task = run.tasks.get(kind='extract')
        claimed = _claim(str(task.public_id), task.fencing_token)
        self.assertIsNotNone(claimed)
        run.status = 'pausing'
        run.save(update_fields=['status'])
        task.refresh_from_db()
        task.lease_until = timezone.now() - timedelta(seconds=5)
        task.save(update_fields=['lease_until'])
        recover_expired_leases()
        task.refresh_from_db()
        run.refresh_from_db()
        self.assertEqual(task.status, TASK_PENDING)
        self.assertEqual(run.status, 'paused')

    def test_lost_worker_during_cancel_finishes_cancel(self):
        job = self._job()
        run = explore_job(job=job, user_public_id=self.user.public_id)
        publish_ready(str(run.public_id))
        approve_run(run=run, user_public_id=self.user.public_id)
        run = start_run(run=run)
        task = run.tasks.get(kind='extract')
        claimed = _claim(str(task.public_id), task.fencing_token)
        self.assertIsNotNone(claimed)
        run.status = 'cancelling'
        run.save(update_fields=['status'])
        task.refresh_from_db()
        task.lease_until = timezone.now() - timedelta(seconds=5)
        task.save(update_fields=['lease_until'])
        recover_expired_leases()
        task.refresh_from_db()
        run.refresh_from_db()
        self.assertEqual(task.status, 'cancelled')
        self.assertEqual(run.status, 'cancelled')


class DirectRepeatTests(TestCase):
    def test_second_delivery_of_same_token_is_skipped(self):
        os.environ['SCOUT_TASK_RUNNER'] = 'inline'
        user = get_user_model().objects.create_superuser(
            username='scout-repeat',
            email='scout-repeat@example.com',
            password='test-pass-123',
        )
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server.server_address
            job = create_job(
                user_public_id=user.public_id,
                title='Повтор',
                seed_urls=[f'http://{host}:{port}/cards'],
                fields=FIELDS,
                record=RECORD,
            )
            run = explore_job(job=job, user_public_id=user.public_id)
            task = run.tasks.get()
            token = task.fencing_token
            self.assertTrue(run_one(str(task.public_id), token))
            self.assertFalse(run_one(str(task.public_id), token))
            self.assertEqual(run.snapshots.count(), 1)
        finally:
            server.shutdown()
            server.server_close()
