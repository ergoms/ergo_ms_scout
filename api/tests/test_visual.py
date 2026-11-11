import io
import socket
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.utils import timezone
from lxml import html
from rest_framework.test import APIClient

from modules.scout.api.models import VisualSnapshot
from modules.scout.api.permissions import CanConfigureScout
from modules.scout.api.services.errors import FetchError
from modules.scout.api.services.html_extract import extract_html
from modules.scout.api.services.public_http import _connect_public, fetch_public_html
from modules.scout.api.services.visual_rules import VisualDocument
from modules.scout.api.services.visual_snapshots import delete_expired_snapshots, load_snapshot

_URL = 'https://example.com/catalog'
_BASE = '/api/scout/visual-snapshots/'
_CATALOG = Path(__file__).with_name('fixtures').joinpath('visual_catalog.html').read_bytes()


def _address(ip, port):
    return socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', (ip, port)


class PublicFetchTests(SimpleTestCase):
    def test_refuses_mixed_public_and_private_dns_without_opening_socket(self):
        with (
            patch('socket.getaddrinfo', return_value=[_address('93.184.216.34', 80), _address('127.0.0.1', 80)]),
            patch('socket.socket') as create_socket,
            self.assertRaises(FetchError) as error,
        ):
            fetch_public_html('http://example.com/', timeout_seconds=5, max_bytes=100)
        self.assertEqual(error.exception.code, 'url_blocked')
        create_socket.assert_not_called()

    def test_connects_to_the_address_that_was_checked(self):
        with (
            patch('socket.getaddrinfo', return_value=[_address('93.184.216.34', 443)]) as resolve,
            patch('socket.socket') as create_socket,
        ):
            connection = _connect_public(('example.com', 443), 5)
        resolve.assert_called_once()
        connection.connect.assert_called_once_with(('93.184.216.34', 443))
        self.assertIs(connection, create_socket.return_value)

    def test_redirect_rechecks_the_address_before_connecting(self):
        public = Mock()
        public.makefile.return_value = io.BytesIO(
            b'HTTP/1.1 302 Found\r\nLocation: http://private.test/\r\nContent-Length: 10000000\r\n\r\n'
        )
        with (
            patch.dict('os.environ', {'SCOUT_TRUSTED_FETCH_ENDPOINTS': '127.0.0.1:80'}),
            patch('socket.getaddrinfo', side_effect=[[_address('93.184.216.34', 80)], [_address('127.0.0.1', 80)]]),
            patch('socket.socket', return_value=public) as create_socket,
            self.assertRaises(FetchError) as error,
        ):
            fetch_public_html('http://example.com/', timeout_seconds=5, max_bytes=100)
        self.assertEqual(error.exception.code, 'url_blocked')
        create_socket.assert_called_once()


class VisualSamplingTests(SimpleTestCase):
    def test_manual_field_check_samples_the_first_match_beyond_twenty_cards(self):
        body = b'<main>' + b'<article>No price</article>' * 20 + (
            b'<article><span class="price">42</span></article></main>'
        )
        record = {'kind': 'css', 'selector': 'article'}
        field = {'name': 'price', 'kind': 'css', 'selector': '.price', 'scope': 'record', 'many': False}
        checked = VisualDocument(body, 'utf-8').check(source_url=_URL, record=record, field=field)
        extracted = extract_html(body, [field], record=record)

        self.assertEqual(checked['row_count'], 21)
        self.assertEqual(len(checked['rows']), 20)
        self.assertEqual([row['match_count'] for row in checked['rows']], [0] * 19 + [1])
        self.assertEqual(checked['rows'][-1]['value'], extracted[-1]['values']['price'])


class VisualSnapshotTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.owner = user_model.objects.create_superuser(username='scout-visual-owner')
        self.other = user_model.objects.create_user(username='scout-visual-other')
        self.client = APIClient()
        self.client.force_authenticate(self.owner)

    def test_editor_uses_saved_html_and_matches_the_extractor(self):
        with (
            patch.dict('os.environ', {'SCOUT_TASK_RUNNER': 'inline'}),
            patch('modules.scout.api.services.visual_snapshots.fetch_public_html', return_value={
                'body': _CATALOG, 'charset': 'utf-8', 'final_url': _URL,
            }) as fetch,
            self.captureOnCommitCallbacks(execute=True),
        ):
            created = self.client.post(_BASE, {'url': _URL}, format='json')
        self.assertEqual(created.status_code, 202)
        path = f"{_BASE}{created.data['public_id']}/"
        ready = self.client.get(path)
        self.assertEqual(ready.status_code, 200)
        self.assertEqual(ready.data['status'], 'ready')
        self.assertEqual(ready['Cache-Control'], 'no-store')
        source = ready.data['html']
        self.assertNotIn('external.test', source)
        self.assertNotIn('alert(1)', source)
        self.assertNotIn('onclick', source)
        self.assertNotIn('spoof', source)
        self.assertIn("default-src 'none'", source)
        document = html.fromstring(source)
        card = document.xpath('//article')[0].get('data-scout-node')
        price = document.xpath('//article/span[text()="10"]')[0].get('data-scout-node')
        found_cards = self.client.post(path + 'check/', {'record': {'node_id': card}, 'field': None}, format='json')
        self.assertEqual(found_cards.status_code, 200)
        self.assertEqual(found_cards.data['row_count'], 3)
        record = found_cards.data['record']
        field = {'name': 'price', 'scope': 'record', 'many': False, 'node_id': price}
        checked = self.client.post(path + 'check/', {'record': record, 'field': field}, format='json')
        self.assertEqual(checked.status_code, 200)
        self.assertEqual([row['match_count'] for row in checked.data['rows']], [1, 0, 2])
        self.assertEqual(checked.data['rows'][0]['match_node_ids'], [price])
        self.assertIsNone(checked.data['rows'][1]['value'])
        expected = extract_html(_CATALOG, [checked.data['field']], record=record)
        self.assertEqual(checked.data['rows'][2]['value'], expected[2]['values']['price'])

        field = dict(checked.data['field'], many=True)
        edited = self.client.post(path + 'check/', {'record': record, 'field': field}, format='json')
        self.assertEqual(edited.data['rows'][2]['value']['normalized'], '20 | 30')
        load_snapshot(created.data['public_id'])
        fetch.assert_called_once()

        other_client = APIClient()
        other_client.force_authenticate(self.other)
        self.assertEqual(other_client.get(path).status_code, 403)
        with patch.object(CanConfigureScout, 'has_permission', return_value=True):
            foreign_snapshot = other_client.get(path)
            self.assertEqual(foreign_snapshot.status_code, 404, foreign_snapshot.content)
            self.assertEqual(other_client.post(path + 'check/', {'record': record}, format='json').status_code, 404)

    def test_waiting_failed_and_expired_snapshot(self):
        snapshot = VisualSnapshot.objects.create(user_public_id=self.owner.public_id, source_url=_URL)
        path = f'{_BASE}{snapshot.public_id}/'
        self.assertEqual(self.client.get(path).data['status'], 'loading')
        self.assertEqual(self.client.post(path + 'check/', {}, format='json').status_code, 409)
        with patch('modules.scout.api.services.visual_snapshots.fetch_public_html', side_effect=FetchError(
            'not_html', 'Ожидался HTML', retryable=False,
        )):
            load_snapshot(snapshot.public_id)
        failed = self.client.get(path)
        self.assertEqual(failed.data['status'], 'failed')
        self.assertEqual(failed.data['error']['code'], 'not_html')
        VisualSnapshot.objects.filter(pk=snapshot.pk).update(created_at=timezone.now() - timedelta(hours=2))
        self.assertEqual(self.client.get(path).status_code, 404)
        delete_expired_snapshots()
        self.assertFalse(VisualSnapshot.objects.filter(pk=snapshot.pk).exists())
