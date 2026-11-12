"""Проверка извлечения, подтверждения, повтора задачи и чужого запуска."""

import csv
import io
import json
import os
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from modules.scout.api.models import Job, Record, Run, Task, VisualSnapshot
from modules.scout.api.present import record_payload
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
from modules.scout.api.tasks import DISPATCH_REQUEST_HEADER, _request_dispatch, dispatch_scout_pending

FIXTURE = Path(__file__).resolve().parent.joinpath('fixtures', 'cards.html').read_bytes()
DATA001_FIXTURE = Path(__file__).resolve().parent.joinpath('fixtures', 'data001_catalog.html').read_bytes()
DATA001_FIELDS = [
    {'name': 'price', 'kind': 'css', 'selector': '.price', 'scope': 'record', 'many': False,
     'conversion': {'type': 'money', 'currency': 'RUB', 'decimal_separator': ','}},
    {'name': 'quantity', 'kind': 'css', 'selector': '.quantity', 'scope': 'record', 'many': False,
     'conversion': {'type': 'integer'}},
    {'name': 'available_on', 'kind': 'css', 'selector': '.available-on', 'scope': 'record', 'many': False,
     'conversion': {'type': 'date', 'format': 'DD.MM.YYYY'}},
]
RECORD = {'kind': 'css', 'selector': '.card'}
FIELDS = [
    {'name': 'heading', 'kind': 'xpath', 'selector': '//h1', 'scope': 'page', 'many': False},
    {'name': 'title', 'kind': 'css', 'selector': '.title', 'scope': 'record', 'many': False},
    {'name': 'price', 'kind': 'css', 'selector': '.price', 'scope': 'record', 'many': False},
]


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = DATA001_FIXTURE if self.path == '/data001/items/1' else FIXTURE
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


class DispatchRequestTests(TestCase):
    def setUp(self):
        owner = uuid4()
        job = create_job(
            user_public_id=owner,
            title='Каталог',
            seed_urls=['https://example.com/'],
            fields=FIELDS,
            record=RECORD,
        )
        self.run = explore_job(job=job, user_public_id=owner)
        self.run_id = str(self.run.public_id)

    def test_one_request_waits_and_the_next_can_be_queued_during_dispatch(self):
        with patch.object(dispatch_scout_pending, 'apply_async') as enqueue:
            _request_dispatch(self.run_id)
            _request_dispatch(self.run_id)
            enqueue.assert_called_once()
            first_message = enqueue.call_args.kwargs

            with patch('modules.scout.api.tasks.publish_ready', side_effect=_request_dispatch) as sweep:
                dispatch_scout_pending.apply(**first_message, throw=True)
                sweep.assert_called_once_with(self.run_id)
                self.assertEqual(enqueue.call_count, 2)
                self.assertNotEqual(enqueue.call_args.kwargs['headers'], first_message['headers'])

                dispatch_scout_pending.apply(**first_message, throw=True)
                sweep.assert_called_once_with(self.run_id)
                self.run.refresh_from_db()
                self.assertEqual(
                    self.run.dispatch_requested_until.isoformat(),
                    enqueue.call_args.kwargs['headers'][DISPATCH_REQUEST_HEADER],
                )

    def test_expired_request_can_be_replaced_and_beat_can_still_sweep(self):
        expired = timezone.now() - timedelta(seconds=1)
        self.run.dispatch_requested_until = expired
        self.run.save(update_fields=['dispatch_requested_until'])
        with patch.object(dispatch_scout_pending, 'apply_async') as enqueue:
            _request_dispatch(self.run_id)
        enqueue.assert_called_once()

        with patch('modules.scout.api.tasks.publish_ready') as sweep:
            dispatch_scout_pending.apply(
                args=[self.run_id], headers={DISPATCH_REQUEST_HEADER: expired.isoformat()}, throw=True,
            )
            sweep.assert_not_called()
            dispatch_scout_pending.apply(throw=True)
            sweep.assert_called_once_with(None)

    def test_broker_failure_releases_the_request(self):
        with patch.object(dispatch_scout_pending, 'apply_async', side_effect=OSError('broker')):
            with self.assertLogs('celery.module.scout.tasks', level='ERROR'):
                _request_dispatch(self.run_id)
        self.run.refresh_from_db()
        self.assertIsNone(self.run.dispatch_requested_until)


class FlowTests(TestCase):
    def setUp(self):
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
        self.enterContext(patch.dict(os.environ, {
            'SCOUT_TASK_RUNNER': 'inline',
            'SCOUT_TRUSTED_FETCH_ENDPOINTS': f'{host}:{port}',
        }))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def _job(self, url=None):
        return create_job(
            user_public_id=self.user.public_id,
            title='Каталог',
            seed_urls=[url or self.url],
            fields=FIELDS,
            record=RECORD,
        )

    def _plan_suggestion_input(self, **snapshot_overrides):
        snapshot = VisualSnapshot.objects.create(**{
            'user_public_id': self.user.public_id,
            'source_url': self.url.replace('/cards', '/data001/items/1'),
            'status': 'ready', 'charset': 'utf-8', 'body': DATA001_FIXTURE,
            **snapshot_overrides,
        })
        client = APIClient()
        client.force_authenticate(user=self.user)
        return client, {'snapshot_public_id': str(snapshot.public_id), 'goal': '  Собери название, цену и остаток  '}

    @override_settings(SCOUT_AI_ENABLED=True, SCOUT_AI_TIMEOUT_SECONDS=7, SCOUT_AI_MODEL='qwen3.5:4b')
    def test_ai_draft_uses_ordinary_preview_and_collection(self):
        body = DATA001_FIXTURE + (
            b'<script>private-script</script><form>private-form</form>'
            b'<a href="private-link"><!-- private-comment -->visible-link</a>'
        )
        client, payload = self._plan_suggestion_input(body=body)
        rules = {'record': {'kind': 'css', 'selector': 'article'}, 'fields': [
            {'name': 'name', 'kind': 'css', 'selector': 'h2', 'scope': 'record', 'many': False},
            *DATA001_FIELDS[:2],
        ]}
        with patch('modules.scout.api.services.plan_suggestions.bridge') as model_bridge:
            model = model_bridge.call
            model.return_value = f'```json\n{json.dumps(rules)}\n```'
            proposed = client.post('/api/scout/plan-suggestions/', payload, format='json')
            self.assertEqual(proposed.status_code, 200, proposed.data)
            self.assertFalse(Job.objects.exists())
            self.assertFalse(Run.objects.exists())
            prompt = json.loads(model.call_args.kwargs['messages'][1]['content'])
            self.assertEqual(model.call_args.args, ('ollama_framework.chat',))
            self.assertEqual(model.call_args.kwargs['config']['request_timeout'], 7)
            self.assertEqual(model.call_args.kwargs['config']['model'], 'qwen3.5:4b')
            self.assertTrue(model.call_args.kwargs['skip_env_injection'])
            self.assertEqual(prompt['goal'], payload['goal'].strip())
            self.assertIn('class="price"', prompt['html'])
            self.assertIn('visible-link', prompt['html'])
            self.assertNotIn('private-', prompt['html'])
            self.assertEqual(proposed.data['snapshot_public_id'], payload['snapshot_public_id'])

            created = client.post('/api/scout/jobs/', proposed.data['draft'], format='json')
            self.assertEqual(created.status_code, 201, created.data)
            explored = client.post(f"/api/scout/jobs/{created.data['public_id']}/explore/", {}, format='json')
            self.assertEqual(explored.status_code, 202, explored.data)
            run_id = explored.data['public_id']
            publish_ready(run_id)
            path = f'/api/scout/runs/{run_id}/'
            preview = client.get(path, {'value_format': 'typed_v1'}).data['preview']
            self.assertEqual([row['values']['name']['normalized'] for row in preview], ['1-1', '1-2', '1-3'])
            self.assertEqual(preview[0]['values']['price']['typed']['value'], {'amount': '1234.50', 'currency': 'RUB'})
            self.assertEqual(preview[0]['values']['quantity']['typed']['value'], 0)
            self.assertEqual(preview[1]['values']['quantity']['typed']['status'], 'missing')
            self.assertEqual(client.post(path + 'approve/', {}, format='json').status_code, 200)
            self.assertEqual(client.post(path + 'start/', {}, format='json').status_code, 202)
            publish_ready(run_id)
            self.assertEqual(client.get(path, {'summary': 1}).data['status'], RUN_COMPLETED)
            model.assert_called_once()

    @override_settings(SCOUT_AI_ENABLED=True)
    def test_ai_suggestion_errors_leave_manual_setup_available(self):
        client, payload = self._plan_suggestion_input()
        rules = {'record': {'kind': 'css', 'selector': 'article'}, 'fields': DATA001_FIELDS[:1]}
        with patch('modules.scout.api.services.plan_suggestions.bridge') as model_bridge:
            model = model_bridge.call
            for reply, status, code, path in (
                (None, 503, 'model_unavailable', None),
                ('private-provider-response', 502, 'invalid_model_response', None),
                (json.dumps({**rules, 'fields': [{**DATA001_FIELDS[0], 'selector': '.price['}]}),
                 422, 'selector', '/fields/0/selector'),
                (json.dumps({**rules, 'fields': [{**DATA001_FIELDS[0], 'selector': '.price-missing'}]}),
                 422, 'selector_no_match', '/fields/0/selector'),
            ):
                with self.subTest(code=code):
                    model.return_value = reply
                    response = client.post('/api/scout/plan-suggestions/', payload, format='json')
                    self.assertEqual(response.status_code, status, response.data)
                    self.assertEqual(response.data['code'], code)
                    self.assertEqual(response.data.get('path'), path)
                    self.assertNotIn('draft', response.data)
                    self.assertNotIn(b'private-provider-response', response.content)
            self.assertFalse(Job.objects.exists())
            self.assertFalse(Run.objects.exists())
            created = client.post('/api/scout/jobs/', {'seed_urls': [self.url], 'fields': FIELDS, 'record': RECORD}, format='json')
            self.assertEqual(created.status_code, 201, created.data)
            self.assertEqual(model.call_count, 4)

    @override_settings(SCOUT_AI_ENABLED=True)
    def test_ai_rejects_unusable_snapshots_and_disabled_mode_before_calling_model(self):
        client, payload = self._plan_suggestion_input(user_public_id=uuid4())
        _, pending = self._plan_suggestion_input(status='queued')
        _, expired = self._plan_suggestion_input()
        VisualSnapshot.objects.filter(public_id=expired['snapshot_public_id']).update(
            created_at=timezone.now() - timedelta(hours=2),
        )
        with patch('modules.scout.api.services.plan_suggestions.bridge') as model_bridge:
            for data, status in ((payload, 404), (pending, 409), (expired, 404), ({**payload, 'goal': ' '}, 400)):
                response = client.post('/api/scout/plan-suggestions/', data, format='json')
                self.assertEqual(response.status_code, status, response.data)
            _, ready = self._plan_suggestion_input()
            with override_settings(SCOUT_AI_ENABLED=False):
                response = client.post('/api/scout/plan-suggestions/', ready, format='json')
            self.assertEqual(response.status_code, 503, response.data)
            self.assertEqual(response.data['code'], 'model_unavailable')
            model_bridge.call.assert_not_called()

    @override_settings(SCOUT_AI_ENABLED=True, SCOUT_AI_TIMEOUT_SECONDS=None)
    def test_ai_timeout_keeps_provider_concurrency_bounded(self):
        client, payload = self._plan_suggestion_input()
        release = threading.Event()
        started = threading.Event()
        completed = threading.Event()
        calls = 0
        lock = threading.Lock()

        def slow_provider(*args, **kwargs):
            nonlocal calls
            with lock:
                calls += 1
                if calls == 2:
                    started.set()
            release.wait(timeout=5)
            with lock:
                calls -= 1
                if calls == 0:
                    completed.set()

        module = 'modules.scout.api.services.plan_suggestions'
        with patch(f'{module}._TIMEOUT_SECONDS', 0.05), patch(f'{module}.bridge') as model_bridge:
            model_bridge.call.side_effect = slow_provider
            try:
                for _ in range(2):
                    response = client.post('/api/scout/plan-suggestions/', payload, format='json')
                    self.assertEqual(response.status_code, 503, response.data)
                    self.assertEqual(response.data['code'], 'model_unavailable')
                self.assertTrue(started.wait(timeout=1))
                response = client.post('/api/scout/plan-suggestions/', payload, format='json')
                self.assertEqual(response.status_code, 503, response.data)
                self.assertEqual(model_bridge.call.call_count, 2)
            finally:
                release.set()
                self.assertTrue(completed.wait(timeout=2))

    def test_preview_rejects_a_private_ip_without_an_operator_exception(self):
        job = self._job('http://127.0.0.1/')
        run = explore_job(job=job, user_public_id=self.user.public_id)
        publish_ready(str(run.public_id))
        task = run.tasks.get(kind='fetch')
        self.assertEqual(task.error_code, 'url_blocked')
        self.assertEqual(task.status, 'failed')
        self.assertEqual(task.attempt_count, 1)
        self.assertFalse(run.snapshots.exists())

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
        records = run.records.order_by('created_at', 'pk')
        retained, removed = records
        evidence = list(retained.evidence.values())
        removed_identity = removed.identity_key
        removed_evidence = list(removed.evidence.values_list('field_path', 'normalized_value'))
        removed.delete()

        extract_task = run.tasks.get(kind='extract')
        extract_task.status = TASK_PENDING
        extract_task.published_at = None
        extract_task.save(update_fields=['status', 'published_at'])
        run.status = RUN_RUNNING
        run.finished_at = None
        run.save(update_fields=['status', 'finished_at'])
        publish_ready(str(run.public_id))
        self.assertEqual(Record.objects.filter(run=run).count(), 2)
        self.assertEqual(list(retained.evidence.values()), evidence)
        recreated = run.records.get(identity_key=removed_identity)
        self.assertEqual(list(recreated.evidence.values_list('field_path', 'normalized_value')), removed_evidence)

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

        client = APIClient()
        client.force_authenticate(user=self.user)
        invalid = client.post(f'/api/scout/runs/{run.public_id}/export/', [], format='json')
        self.assertEqual(invalid.status_code, 400, invalid.data)
        self.assertEqual((invalid.data['code'], invalid.data['path']), ('invalid', '/'))

        class Storage:
            def commit_local(self, local_path, path):
                self.path = path
                self.local_path = Path(local_path)
                self.content = self.local_path.read_bytes()
                return path

        storage = Storage()
        with patch('modules.scout.api.services.exporting.get_media_client', return_value=storage), patch(
            'modules.scout.api.services.exporting.get_signed_media_url',
            return_value='/serve/scout/exports/records.csv?signature=1&expires=1',
        ), patch('modules.scout.api.services.exporting.EXPORT_BATCH_SIZE', 1):
            _artifact, url, filename = export_run(run=run, kind='csv')
            self.assertEqual(filename, 'records.csv')
            self.assertIn('/serve/', url)
            self.assertTrue(storage.content.startswith(b'\xef\xbb\xbf'))
            self.assertIn('Токарный', storage.content.decode('utf-8'))
            self.assertFalse(storage.local_path.exists())

            _artifact, _url, filename = export_run(run=run, kind='json')
            self.assertEqual(filename, 'records.json')
            self.assertEqual(json.loads(storage.content), [
                record_payload(record) for record in run.records.order_by('created_at', 'pk')
            ])
            self.assertFalse(storage.local_path.exists())

    def test_records_cursor_and_lightweight_status(self):
        run = explore_job(job=self._job(), user_public_id=self.user.public_id)
        publish_ready(str(run.public_id))
        approve_run(run=run, user_public_id=self.user.public_id)
        run = start_run(run=run)
        publish_ready(str(run.public_id))
        run.records.update(created_at=timezone.now())

        client = APIClient()
        client.force_authenticate(user=self.user)
        path = f'/api/scout/runs/{run.public_id}/'
        full = client.get(path + 'records/').data
        first = client.get(path + 'records/', {'limit': 1}).data
        second = client.get(path + 'records/', {'limit': 1, 'cursor': first['next_cursor']}).data
        self.assertEqual(set(full), {'records'})
        self.assertEqual(first['records'] + second['records'], full['records'])
        self.assertEqual(first['next_cursor'], first['records'][0]['public_id'])
        self.assertIsNone(second['next_cursor'])
        latest = client.get(path + 'records/', {'order': 'recent', 'limit': 1}).data
        previous = client.get(path + 'records/', {
            'order': 'recent', 'limit': 1, 'cursor': latest['next_cursor'],
        }).data
        self.assertEqual(latest['records'] + previous['records'], full['records'][::-1])
        self.assertIsNone(previous['next_cursor'])
        self.assertEqual(client.get(path + 'records/', {'order': 'invalid'}).data['code'], 'invalid_order')
        self.assertEqual(client.get(path + 'records/', {'limit': 201}).data['code'], 'invalid_limit')
        self.assertEqual(client.get(path + 'records/', {'cursor': str(uuid4())}).data['code'], 'invalid_cursor')

        with patch('modules.scout.api.present.journal_lines', side_effect=AssertionError('summary read journal')):
            summary = client.get(path, {'summary': 1}).data
        self.assertEqual(summary['status'], RUN_COMPLETED)
        self.assertEqual(summary['record_count'], 2)
        self.assertEqual(summary['snapshot_count'], 1)
        self.assertEqual(summary['progress'], {'fetched': 1, 'extracted': 1, 'errors': 0})
        self.assertTrue({'journal', 'events', 'preview', 'tasks'}.isdisjoint(summary))
        with patch('modules.scout.api.services.journal.read_snapshot_bytes') as read_snapshot:
            details = client.get(path)
        self.assertEqual(details.status_code, 200)
        read_snapshot.assert_not_called()
        self.assertTrue(any('совпадений' in line for line in details.data['journal']))

    def test_data001_preview_records_and_exports_keep_both_formats(self):
        client = APIClient()
        client.force_authenticate(user=self.user)
        url = self.url.replace('/cards', '/data001/items/1')
        created = client.post('/api/scout/jobs/', {
            'title': 'DATA-001', 'seed_urls': [url], 'fields': DATA001_FIELDS,
            'record': {'kind': 'css', 'selector': 'article'},
        }, format='json')
        self.assertEqual(created.status_code, 201, created.data)
        self.assertEqual(created.data['revision']['fields'], DATA001_FIELDS)
        explored = client.post(f"/api/scout/jobs/{created.data['public_id']}/explore/", {}, format='json')
        run_id = explored.data['public_id']
        publish_ready(run_id)
        path = f'/api/scout/runs/{run_id}/'
        format_param = {'value_format': 'typed_v1'}
        preview = client.get(path, format_param).data['preview']
        old_preview = client.get(path).data['preview']
        self.assertEqual(len(preview), 3)
        self.assertEqual(preview[0]['values']['price']['typed']['value'], {'amount': '1234.50', 'currency': 'RUB'})
        self.assertEqual(preview[0]['values']['quantity']['typed']['value'], 0)
        self.assertEqual(preview[0]['values']['available_on']['typed']['value'], '2026-11-06')
        self.assertEqual([cell['typed']['status'] for cell in preview[1]['values'].values()], ['empty', 'missing', 'invalid'])
        self.assertEqual(old_preview[1]['values']['quantity'], {'raw': '', 'normalized': ''})
        self.assertEqual({item['field_path'] for item in old_preview[1]['evidence']}, {'price', 'quantity', 'available_on'})
        self.assertEqual({item['field_path'] for item in preview[1]['evidence']}, {'price', 'available_on'})
        self.assertEqual(preview[1]['evidence'][0]['locator'], 'css:.price#1.0')
        self.assertEqual(preview[1]['evidence'][0]['raw_value'], ' ')
        self.assertEqual(preview[1]['evidence'][0]['source_url'], url)
        self.assertEqual(client.post(path + 'approve/', {}, format='json').status_code, 200)
        self.assertEqual(client.post(path + 'start/', {}, format='json').status_code, 202)
        publish_ready(run_id)
        self.assertEqual(client.get(path, {'summary': 1}).data['status'], RUN_COMPLETED)
        first = client.get(path + 'records/', {**format_param, 'limit': 2}).data
        second = client.get(path + 'records/', {**format_param, 'limit': 2, 'cursor': first['next_cursor']}).data
        self.assertIsNone(second['next_cursor'])
        typed_records = first['records'] + second['records']
        self.assertEqual(len(typed_records), 3)
        for row, example in zip(typed_records, preview):
            self.assertEqual(row['values'], example['values'])
            self.assertCountEqual(row['evidence'], example['evidence'])
        old_records = client.get(path + 'records/').data['records']
        self.assertEqual(old_records[1]['values'], old_preview[1]['values'])
        self.assertTrue(all('snapshot_public_id' not in item for item in old_records[1]['evidence']))
        self.assertEqual(client.get(path + 'records/', {'value_format': 'future'}).data['code'], 'invalid_value_format')

        stored = {}

        def commit_local(local_path, storage_path):
            stored[storage_path] = Path(local_path).read_bytes()
            return storage_path

        with patch('modules.scout.api.services.exporting.get_media_client') as storage, patch(
            'modules.scout.api.services.exporting.get_signed_media_url', side_effect=lambda path, **_: path,
        ):
            storage.return_value.commit_local.side_effect = commit_local
            links = {}
            for typed in (True, False):
                for kind in ('json', 'csv'):
                    payload = {'kind': kind, **(format_param if typed else {})}
                    response = client.post(path + 'export/', payload, format='json')
                    self.assertEqual(response.status_code, 200, response.data)
                    links[typed, kind] = response.data['download_url']

        self.assertEqual(len(stored), 4)
        self.assertEqual(json.loads(stored[links[True, 'json']]), typed_records)
        self.assertEqual(json.loads(stored[links[False, 'json']]), old_records)
        csv_rows = list(csv.DictReader(io.StringIO(stored[links[True, 'csv']].decode('utf-8-sig'))))
        self.assertEqual([row['public_id'] for row in csv_rows], [row['public_id'] for row in typed_records])
        self.assertEqual([csv_rows[0][key] for key in ('price', 'price__currency', 'quantity', 'available_on')],
                         ['1234.50', 'RUB', '0', '2026-11-06'])
        self.assertEqual([csv_rows[1][key] for key in ('price__status', 'quantity__status', 'available_on__error')],
                         ['empty', 'missing', 'invalid_date'])
        self.assertEqual([csv_rows[2][key] for key in ('price__error', 'quantity__error', 'available_on__status')],
                         ['invalid_decimal', 'invalid_integer', 'empty'])
        old_csv = list(csv.DictReader(io.StringIO(stored[links[False, 'csv']].decode('utf-8-sig'))))
        self.assertEqual(set(old_csv[0]), {'public_id', 'price', 'quantity', 'available_on'})
        self.assertEqual(old_csv[0]['price'], '1 234,50 ₽')

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

    def test_job_writes_reject_invalid_json_shapes(self):
        client = APIClient()
        client.force_authenticate(user=self.user)
        job = self._job()
        valid = {'title': 'Каталог', 'seed_urls': [self.url], 'fields': FIELDS, 'record': RECORD}
        invalid = [
            ([], '/'),
            ({**valid, 'title': 17}, '/title'),
            ({**valid, 'seed_urls': 17}, '/seed_urls'),
            ({**valid, 'fields': 17}, '/fields'),
            ({**valid, 'crawl': {'allowed_hosts': 17}}, '/crawl/allowed_hosts'),
            ({**valid, 'crawl': {'mode': 'site', 'visit': 17}}, '/crawl/visit'),
        ]
        for write, url in ((client.post, '/api/scout/jobs/'), (client.patch, f'/api/scout/jobs/{job.public_id}/')):
            for body, path in invalid:
                with self.subTest(method=write.__name__, path=path):
                    response = write(url, body, format='json')
                    self.assertEqual(response.status_code, 400, response.data)
                    self.assertEqual((response.data['code'], response.data['path']), ('invalid', path))
        self.assertEqual(Job.objects.count(), 1)
        self.assertEqual(job.revisions.count(), 1)

    def test_manual_preview_selector_error_blocks_approval_and_start(self):
        client = APIClient()
        client.force_authenticate(user=self.user)
        job = create_job(
            user_public_id=self.user.public_id, title='Каталог', seed_urls=[self.url],
            fields=[{'name': 'price', 'kind': 'css', 'selector': '.price['}],
        )
        run = explore_job(job=job, user_public_id=self.user.public_id)
        publish_ready(str(run.public_id))
        url = f'/api/scout/runs/{run.public_id}/'
        self.assertEqual(client.get(url).data['preview']['error']['code'], 'selector')

        approval = client.post(url + 'approve/')
        self.assertEqual(approval.status_code, 400, approval.data)
        self.assertEqual(approval.data['code'], 'selector')
        revision = job.revisions.get()
        self.assertFalse(revision.approved_plan_hash)

        # A revision approved before the validation was added must be checked on start too.
        revision.approved_plan_hash = revision.plan_hash
        revision.save(update_fields=['approved_plan_hash'])
        started = client.post(url + 'start/')
        self.assertEqual(started.status_code, 400, started.data)
        self.assertEqual(started.data['code'], 'selector')
        run.refresh_from_db()
        self.assertEqual(run.status, RUN_AWAITING)
        self.assertFalse(run.tasks.filter(kind='extract').exists())

    def test_empty_card_preview_still_checks_field_selector_syntax(self):
        client = APIClient()
        client.force_authenticate(user=self.user)
        for kind, selector in (('css', '.price['), ('xpath', '//*[')):
            with self.subTest(kind=kind):
                job = create_job(
                    user_public_id=self.user.public_id, title='Каталог', seed_urls=[self.url],
                    record={'kind': 'css', 'selector': '.missing-card'},
                    fields=[{'name': 'price', 'kind': kind, 'selector': selector}],
                )
                run = explore_job(job=job, user_public_id=self.user.public_id)
                publish_ready(str(run.public_id))
                url = f'/api/scout/runs/{run.public_id}/'
                self.assertEqual(client.get(url).data['preview']['error']['code'], 'selector')
                approval = client.post(url + 'approve/')
                self.assertEqual(approval.status_code, 400, approval.data)
                self.assertEqual(approval.data['code'], 'selector')
                self.assertEqual(client.post(url + 'cancel/').status_code, 200)

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
            self.enterContext(patch.dict(os.environ, {
                'SCOUT_TASK_RUNNER': 'inline',
                'SCOUT_TRUSTED_FETCH_ENDPOINTS': f'{host}:{port}',
            }))
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
