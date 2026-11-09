"""Прокси CONNECT с TLS на обеих сторонах и реальная выдача Scout."""

import base64
import json
import os
import select
import socket
import socketserver
import ssl
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep
from unittest.mock import patch

from cryptography import x509
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from kombu import Connection, Producer
from celery.signals import before_task_publish
from rest_framework.test import APIClient

from modules.scout.api.models import ProxyProfileRevision, Run
from modules.scout.api.services import proxy_http
from modules.scout.api.services.errors import FetchError
from modules.scout.api.services.proxy_profiles import load_proxy
from modules.scout.api.services.public_http import _public_address
from modules.scout.api.services.tasks_exec import publish_ready
from modules.scout.api.tasks import execute_scout_task, load_visual_snapshot

_LOGIN = 'sentinel-scout-user'
_PASSWORD = 'sentinel-scout-pass:!'
_AUTH = b'Basic ' + base64.b64encode(f'{_LOGIN}:{_PASSWORD}'.encode())
_DESTINATION = '8.8.8.8'
_ORIGIN = 'https://origin.test/catalog'


def _certificate(name, key, *, issuer=None, issuer_key=None):
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.now(timezone.utc)
    return (x509.CertificateBuilder().subject_name(subject).issuer_name(issuer.subject if issuer else subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=issuer is None, path_length=None), critical=True)
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False)
            .sign(issuer_key or key, hashes.SHA256()))


def _server_context(directory, name, ca, ca_key):
    key = ec.generate_private_key(ec.SECP256R1())
    cert_path = directory / f'{name}.pem'
    key_path = directory / f'{name}.key'
    cert_path.write_bytes(_certificate(name, key, issuer=ca, issuer_key=ca_key).public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    return context


class _TLSServer(socketserver.ThreadingTCPServer):
    daemon_threads = True

    def get_request(self):
        conn, address = super().get_request()
        try:
            conn.settimeout(5)
            return self.context.wrap_socket(conn, server_side=True), address
        except OSError:
            conn.close()
            raise


def _request(stream):
    request_line = stream.readline(8192).strip()
    headers = {}
    while line := stream.readline(8192).strip():
        name, value = line.split(b':', 1)
        headers[name.lower()] = value.strip()
    return request_line, headers


class _Origin(socketserver.StreamRequestHandler):
    def handle(self):
        request, headers = _request(self.rfile)
        self.server.requests.append((request, headers))
        if request.startswith(b'GET /redirect '):
            self.wfile.write(b'HTTP/1.1 302 Found\r\nLocation: https://169.254.169.254/\r\nContent-Length: 0\r\n\r\n')
            return
        if request.startswith(b'GET /challenge '):
            self.wfile.write(b'HTTP/1.1 200 OK\r\nCF-Mitigated: challenge\r\nContent-Type: text/html\r\n'
                             b'Content-Length: 0\r\nConnection: close\r\n\r\n')
            return
        body = b'<h1>Catalog</h1><a href="/item">Item</a>' if b'/catalog ' in request else b'<h1>Item</h1>'
        self.wfile.write(b'HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: '
                         + str(len(body)).encode() + b'\r\nConnection: close\r\n\r\n' + body)


class _Proxy(socketserver.StreamRequestHandler):
    def handle(self):
        request, headers = _request(self.rfile)
        self.server.requests.append((request, headers))
        if self.server.reject or headers.get(b'proxy-authorization') != self.server.auth:
            self.wfile.write(b'HTTP/1.1 407 denied ' + _AUTH + b'\r\nContent-Length: 0\r\n\r\n')
            return
        if getattr(self.server, 'slow_headers', False):
            try:
                for byte in b'HTTP/1.1 200 Connection Established\r\n\r\n':
                    self.wfile.write(bytes((byte,)))
                    sleep(0.04)
            except OSError:
                pass
            return
        if not request.startswith(f'CONNECT {_DESTINATION}:443 '.encode()):
            self.wfile.write(b'HTTP/1.1 403 forbidden\r\nContent-Length: 0\r\n\r\n')
            return
        self.wfile.write(b'HTTP/1.1 200 Connection Established\r\n\r\n')
        with socket.create_connection(self.server.origin, timeout=5) as upstream:
            while True:
                ready, _, _ = select.select([self.connection, upstream], [], [], 5)
                if not ready:
                    break
                for reader in ready:
                    data = reader.recv(65536)
                    if not data:
                        return
                    (upstream if reader is self.connection else self.connection).sendall(data)


class ProxyTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser('proxy-owner', 'proxy@example.com', 'fixture-only')
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.path = '/api/scout/proxy-profiles/'
        directory = Path(self.enterContext(TemporaryDirectory()))
        ca_key = ec.generate_private_key(ec.SECP256R1())
        ca = _certificate('Scout test CA', ca_key)
        ca_file = directory / 'ca.pem'
        ca_file.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
        self.origin = self._server(_Origin, _server_context(directory, 'origin.test', ca, ca_key))
        self.proxy = self._server(_Proxy, _server_context(directory, 'proxy.test', ca, ca_key))
        self.proxy.origin = self.origin.server_address
        self.proxy.reject = False
        self.proxy.auth = _AUTH
        self.port = self.proxy.server_address[1]
        self.enterContext(override_settings(
            SCOUT_PROXY_ENABLED=True,
            SCOUT_PROXY_ENDPOINTS={f'127.0.0.1:{self.port}': 'proxy.test'},
            SCOUT_PROXY_KEYS={'fixture': Fernet.generate_key().decode()}, SCOUT_PROXY_ACTIVE_KEY='fixture',
            SCOUT_PROXY_CHECK_URL=_ORIGIN,
        ))
        self.enterContext(patch.dict(os.environ, {'SCOUT_TASK_RUNNER': 'inline'}))
        self.enterContext(patch('modules.scout.api.services.proxy_profiles._public_address',
                                side_effect=lambda ip: ip == '127.0.0.1'))
        default_context = ssl.create_default_context
        self.enterContext(patch.object(proxy_http.ssl, 'create_default_context',
                                       side_effect=lambda: default_context(cafile=ca_file)))
        resolve = socket.getaddrinfo
        self.enterContext(patch.object(proxy_http.socket, 'getaddrinfo', side_effect=lambda host, port, *args, **kwargs:
                                       resolve(_DESTINATION if host == 'origin.test' else host, port, *args, **kwargs)))

    def _server(self, handler, context):
        server = _TLSServer(('127.0.0.1', 0), handler)
        server.context = context
        server.requests = []
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def _profile(self, anonymous=False):
        data = {'ip': '127.0.0.1', 'port': self.port}
        data.update({'auth_type': 'none'} if anonymous else {'username': _LOGIN, 'password': _PASSWORD})
        result = self.client.post(self.path, data, format='json', secure=True)
        self.assertEqual(result.status_code, 201, result.data)
        return result.data

    def test_anonymous_proxy_preview_collection_has_no_authorization_headers(self):
        self.proxy.auth = None
        profile = self._profile(anonymous=True)
        self.assertEqual((profile['auth_type'], profile['credentials_configured']), ('none', False))
        self.assertEqual(self.client.post(self.path + profile['public_id'] + '/check/', {}).data, {'status': 'ready'})
        _, run = self._job(profile)
        publish_ready(run['public_id'])
        path = f"/api/scout/runs/{run['public_id']}/"
        self.assertEqual(self.client.get(path).data['status'], 'awaiting_confirmation')
        self.assertEqual(self.client.post(path + 'approve/', {}).status_code, 200)
        self.assertEqual(self.client.post(path + 'start/', {}).status_code, 202)
        publish_ready(run['public_id'])
        db_run = Run.objects.get(public_id=run['public_id'])
        self.assertEqual(db_run.status, 'completed')
        self.assertEqual(db_run.records.count(), 2)
        self.assertTrue(self.proxy.requests)
        self.assertTrue(all(b'proxy-authorization' not in headers for _, headers in self.proxy.requests))
        self.assertTrue(all(b'proxy-authorization' not in headers for _, headers in self.origin.requests))
        connection = load_proxy(profile['revision_public_id'], self.user.public_id)
        self.assertIsNone(connection.username)
        self.assertIsNone(connection.password)

    def test_auth_type_is_explicit_and_mixed_credentials_are_rejected(self):
        endpoint = {'ip': '127.0.0.1', 'port': self.port}
        for extra in ({}, {'auth_type': None}, {'auth_type': []}, {'auth_type': 'socks5'},
                      {'auth_type': 'none', 'username': '', 'password': ''},
                      {'auth_type': 'none', 'username': _LOGIN, 'password': _PASSWORD},
                      {'auth_type': 'basic', 'username': '', 'password': ''},
                      {'auth_type': 'none', 'password': None}):
            with self.subTest(extra=extra):
                response = self.client.post(self.path, {**endpoint, **extra}, format='json', secure=True)
                self.assertEqual(response.status_code, 400, response.data)
                self._assert_no_secret(response.data)
        self.assertEqual(self._profile()['auth_type'], 'basic')

    def test_anonymous_authentication_cannot_be_downgraded_or_forged_in_database(self):
        for anonymous in (False, True):
            with self.subTest(anonymous=anonymous):
                profile = self._profile(anonymous)
                revision = ProxyProfileRevision.objects.get(public_id=profile['revision_public_id'])
                self.assertTrue(revision.ciphertext)
                revision.auth_type = 'basic' if anonymous else 'none'
                revision.save(update_fields=['auth_type'])
                with self.assertRaises(FetchError) as corrupt:
                    load_proxy(revision.public_id, self.user.public_id)
                self.assertEqual(corrupt.exception.code, 'proxy_credentials_unavailable')

    def test_anonymous_407_does_not_retry_or_fall_back_to_direct(self):
        self.proxy.auth = None
        profile = self._profile(True)
        self.proxy.reject = True
        _, run = self._job(profile)
        publish_ready(run['public_id'])
        db_run = Run.objects.get(public_id=run['public_id'])
        self.assertEqual((db_run.status, db_run.stop_reason), ('failed', 'proxy_auth_failed'))
        self.assertEqual(db_run.tasks.get().attempt_count, 1)
        self.assertEqual(len(self.proxy.requests), 1)
        self.assertNotIn(b'proxy-authorization', self.proxy.requests[0][1])
        self.assertFalse(self.origin.requests)

    def test_anonymous_profile_switching_pruning_rotation_revocation_and_https(self):
        endpoint = {'ip': '127.0.0.1', 'port': self.port, 'auth_type': 'none'}
        self.assertEqual(self.client.post(self.path, endpoint, format='json').data['code'], 'https_required')
        profile = self._profile()
        path = self.path + profile['public_id'] + '/'
        anonymous = self.client.patch(path, endpoint, format='json', secure=True)
        self.assertEqual(anonymous.status_code, 200, anonymous.data)
        self.assertFalse(anonymous.data['credentials_configured'])
        self.assertEqual(ProxyProfileRevision.objects.get(public_id=profile['revision_public_id']).ciphertext, '')
        revision_id = anonymous.data['revision_public_id']
        from django.conf import settings
        new_key = Fernet.generate_key().decode()
        with self.settings(SCOUT_PROXY_KEYS={**settings.SCOUT_PROXY_KEYS, 'rotated': new_key},
                           SCOUT_PROXY_ACTIVE_KEY='rotated'):
            call_command('scout_rotate_proxy_keys')
            self.assertIsNone(load_proxy(revision_id, self.user.public_id).username)
        with self.settings(SCOUT_PROXY_KEYS={}):
            with self.assertRaises(FetchError):
                load_proxy(revision_id, self.user.public_id)
        self.assertEqual(self.client.delete(path).status_code, 204)
        with self.assertRaises(FetchError):
            load_proxy(revision_id, self.user.public_id)

    def _job(self, profile, url=_ORIGIN, allowed_hosts=None):
        response = self.client.post('/api/scout/jobs/', {
            'seed_urls': [url], 'fields': [{'name': 'title', 'kind': 'css', 'selector': 'h1'}],
            'crawl': {'mode': 'site', 'allowed_hosts': allowed_hosts or []},
            'proxy_profile_revision_public_id': profile['revision_public_id'],
        }, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        job = response.data
        run = self.client.post(f"/api/scout/jobs/{job['public_id']}/explore/", {}, format='json').data
        return job, run

    def _assert_no_secret(self, value):
        serialized = json.dumps(value, default=str)
        for marker in (_LOGIN, _PASSWORD, _AUTH.split()[1].decode()):
            self.assertNotIn(marker, serialized)

    def test_proxy_https_preview_run_visual_and_serialized_celery_messages(self):
        with self.assertLogs('modules.scout', level='INFO') as logs:
            profile = self._profile()
            checked = self.client.post(self.path + profile['public_id'] + '/check/', {}, format='json')
        self.assertEqual(checked.data, {'status': 'ready'})
        self._assert_no_secret(logs.output)
        self._assert_no_secret(profile)
        self._assert_no_secret(self.client.get(self.path).data)
        stored = ProxyProfileRevision.objects.get(public_id=profile['revision_public_id'])
        self._assert_no_secret(stored.ciphertext)
        with patch('modules.scout.api.views.schedule_publish'):
            job, run = self._job(profile)
        path = f"/api/scout/runs/{run['public_id']}/"
        messages = []

        def record(sender=None, body=None, headers=None, **kwargs):
            messages.append((sender, body, headers))

        before_task_publish.connect(record, weak=False)
        self.addCleanup(before_task_publish.disconnect, record)
        with Connection('memory://') as broker, patch.dict(os.environ, {'SCOUT_TASK_RUNNER': 'celery'}):
            producer = Producer(broker)
            apply = execute_scout_task.apply_async
            with patch.object(execute_scout_task, 'delay', side_effect=lambda *args: apply(args=args, producer=producer)):
                publish_ready(run['public_id'])
            visual_apply = load_visual_snapshot.apply_async
            with patch.object(load_visual_snapshot, 'apply_async',
                              side_effect=lambda **kwargs: visual_apply(producer=producer, **kwargs)):
                with self.captureOnCommitCallbacks(execute=True):
                    visual = self.client.post('/api/scout/visual-snapshots/', {
                        'url': _ORIGIN, 'proxy_profile_revision_public_id': profile['revision_public_id'],
                    }, format='json').data
        self.assertEqual(len(messages), 2)
        self._assert_no_secret(messages)
        load_visual_snapshot.apply(args=[visual['public_id']], throw=True)
        visual_result = self.client.get(f"/api/scout/visual-snapshots/{visual['public_id']}/").data
        self.assertEqual(visual_result['status'], 'ready', visual_result)
        # Повторная публикация после перехвата сообщения тестовым брокером.
        Run.objects.get(public_id=run['public_id']).tasks.update(published_at=None)
        publish_ready(run['public_id'])
        preview = self.client.get(path).data
        self.assertEqual(preview['status'], 'awaiting_confirmation', preview)
        self.assertEqual(self.client.post(path + 'approve/', {}).status_code, 200)
        self.assertEqual(self.client.post(path + 'start/', {}).status_code, 202)
        publish_ready(run['public_id'])
        final = self.client.get(path).data
        self.assertEqual(final['status'], 'completed', final)
        self.assertEqual(final['snapshot_count'], 2)
        self._assert_no_secret((job, preview, final, visual_result, self.client.get(path + 'records/').data))
        self.assertTrue(all(headers.get(b'host') == b'origin.test' and b'proxy-authorization' not in headers
                            for _, headers in self.origin.requests))
        self.assertTrue(all(request.startswith(f'CONNECT {_DESTINATION}:443 '.encode()) and
                            headers.get(b'proxy-authorization') == _AUTH for request, headers in self.proxy.requests))

    def test_407_is_terminal_for_preview_and_no_direct_fallback(self):
        profile = self._profile()
        self.proxy.reject = True
        with self.assertLogs('modules.scout', level='INFO') as logs:
            check = self.client.post(self.path + profile['public_id'] + '/check/', {}, format='json')
        self.assertEqual(check.data, {'status': 'failed', 'code': 'proxy_auth_failed'})
        self._assert_no_secret((check.data, logs.output))
        _, run = self._job(profile)
        publish_ready(run['public_id'])
        db_run = Run.objects.get(public_id=run['public_id'])
        self.assertEqual((db_run.status, db_run.stop_reason), ('failed', 'proxy_auth_failed'))
        self.assertEqual(db_run.tasks.get().attempt_count, 1)
        self.assertEqual(len(self.proxy.requests), 2)
        self.assertFalse(self.origin.requests)

    def test_unavailable_proxy_retries_and_blocks_remaining_fetches(self):
        profile = self._profile()
        _, run = self._job(profile)
        publish_ready(run['public_id'])
        path = f"/api/scout/runs/{run['public_id']}/"
        self.assertEqual(self.client.post(path + 'approve/', {}).status_code, 200)
        self.proxy.shutdown()
        self.proxy.server_close()
        self.assertEqual(self.client.post(path + 'start/', {}).status_code, 202)
        publish_ready(run['public_id'])
        db_run = Run.objects.get(public_id=run['public_id'])
        self.assertEqual((db_run.status, db_run.stop_reason), ('completed_with_errors', 'proxy_unavailable'))
        self.assertEqual(db_run.tasks.get(error_code='proxy_unavailable').attempt_count, 3)
        self.assertEqual(len(self.origin.requests), 1)

    def test_internal_destination_and_redirect_blocked_before_connect(self):
        profile = self._profile()
        with patch('modules.scout.api.services.proxy_profiles._public_address', _public_address):
            check = self.client.post(self.path + profile['public_id'] + '/check/', {}, format='json')
        self.assertEqual(check.data, {'status': 'failed', 'code': 'proxy_blocked'})
        _, run = self._job(profile, 'https://127.0.0.1/')
        publish_ready(run['public_id'])
        self.assertFalse(self.proxy.requests)
        self.assertEqual(Run.objects.get(public_id=run['public_id']).tasks.get().error_code, 'url_blocked')
        _, run = self._job(profile, 'https://origin.test/redirect', ['origin.test', '169.254.169.254'])
        publish_ready(run['public_id'])
        self.assertEqual(len(self.proxy.requests), 1)
        self.assertEqual(Run.objects.get(public_id=run['public_id']).tasks.get().error_code, 'url_blocked')
        with self.settings(SCOUT_PROXY_ENDPOINTS={}):
            self.assertEqual(self.client.post(self.path + profile['public_id'] + '/check/', {}, format='json').data,
                             {'status': 'failed', 'code': 'proxy_blocked'})

    def test_credentials_require_https_owner_and_strict_ciphertext(self):
        rejected = self.client.post(self.path, {'ip': '127.0.0.1', 'port': self.port, 'username': _LOGIN,
                                                 'password': _PASSWORD}, format='json')
        self.assertEqual(rejected.data['code'], 'https_required')
        self._assert_no_secret(rejected.data)
        with patch('modules.scout.api.proxy_views.save_profile', side_effect=RuntimeError(_PASSWORD)), \
                self.assertLogs('modules.scout', level='ERROR') as logs:
            error = self.client.post(self.path, {}, format='json', secure=True)
        self.assertEqual(error.status_code, 503)
        self._assert_no_secret((error.data, logs.output))
        profile = self._profile()
        other = get_user_model().objects.create_superuser('other-proxy-owner', 'other-proxy@example.com', 'fixture')
        self.client.force_authenticate(other)
        self.assertEqual(self.client.get(self.path + profile['public_id'] + '/').status_code, 404)
        revision_id = profile['revision_public_id']
        with self.assertRaises(FetchError) as forbidden:
            load_proxy(revision_id, other.public_id)
        self.assertEqual(forbidden.exception.code, 'proxy_credentials_unavailable')
        ProxyProfileRevision.objects.filter(public_id=revision_id).update(ciphertext='plain-text')
        with self.assertRaises(FetchError) as corrupt:
            load_proxy(revision_id, self.user.public_id)
        self.assertEqual(corrupt.exception.code, 'proxy_credentials_unavailable')

    def test_key_rotation_and_profile_revocation(self):
        profile = self._profile()
        revision_id = profile['revision_public_id']
        from django.conf import settings

        new_key = Fernet.generate_key().decode()
        with self.settings(SCOUT_PROXY_KEYS={**settings.SCOUT_PROXY_KEYS, 'rotated': new_key},
                           SCOUT_PROXY_ACTIVE_KEY='rotated'):
            call_command('scout_rotate_proxy_keys')
        self.assertEqual(ProxyProfileRevision.objects.get(public_id=revision_id).key_id, 'rotated')
        with self.settings(SCOUT_PROXY_KEYS={'rotated': new_key}):
            self.assertEqual(load_proxy(revision_id, self.user.public_id).password, _PASSWORD)
        self.assertEqual(self.client.delete(self.path + profile['public_id'] + '/').status_code, 204)
        self.assertEqual(ProxyProfileRevision.objects.get(public_id=revision_id).ciphertext, '')
        with self.assertRaises(FetchError):
            load_proxy(revision_id, self.user.public_id)

    def test_connect_deadline_and_invalid_proxy_certificate(self):
        profile = self._profile()
        self.proxy.slow_headers = True
        started = monotonic()
        with self.assertRaises(FetchError) as timeout:
            proxy_http.fetch_proxy_html(_ORIGIN, revision_id=profile['revision_public_id'],
                                        owner=self.user.public_id, timeout_seconds=0.5, max_bytes=1024)
        self.assertEqual(timeout.exception.code, 'proxy_unavailable')
        self.assertLess(monotonic() - started, 1.25)
        with self.settings(SCOUT_PROXY_ENDPOINTS={f'127.0.0.1:{self.port}': 'wrong.test'}):
            profile = self._profile()
            seen = len(self.proxy.requests)
            checked = self.client.post(self.path + profile['public_id'] + '/check/', {}, format='json')
        self.assertEqual(checked.data, {'status': 'failed', 'code': 'proxy_tls_error'})
        self.assertEqual(len(self.proxy.requests), seen)

    def test_challenge_through_https_proxy_is_not_a_snapshot_or_retry(self):
        from modules.scout.api.models import VisualSnapshot
        from modules.scout.api.services.visual_snapshots import load_snapshot

        profile = self._profile()
        with patch('modules.scout.api.views.schedule_publish'):
            _, state = self._job(profile, url='https://origin.test/challenge')
        publish_ready(state['public_id'])
        run = Run.objects.get(public_id=state['public_id'])
        self.assertEqual((run.status, run.stop_reason), ('failed', 'cloudflare_challenge'))
        self.assertFalse(run.snapshots.exists())
        self.assertFalse(run.records.exists())
        self.assertEqual(run.tasks.get().attempt_count, 1)
        snapshot = VisualSnapshot.objects.create(user_public_id=self.user.public_id,
                                                source_url='https://origin.test/challenge',
                                                proxy_profile_revision_public_id=profile['revision_public_id'])
        load_snapshot(snapshot.public_id)
        snapshot.refresh_from_db()
        self.assertEqual((snapshot.status, snapshot.error_code, bytes(snapshot.body)),
                         ('failed', 'cloudflare_challenge', b''))
        result = self.client.get(f'/api/scout/visual-snapshots/{snapshot.public_id}/').data
        self.assertEqual(result['error']['code'], 'cloudflare_challenge')
        self.assertNotIn('html', result)
        self._assert_no_secret(result)
