"""Проверки аудита: карточки, XPath, кодировка, журнал, сжатие и снимок."""

import zlib
from concurrent.futures import ThreadPoolExecutor

from django.test import SimpleTestCase, TestCase

from modules.scout.api.services.errors import FetchError, ScoutError
from modules.scout.api.services.html_extract import extract_html
from modules.scout.api.services.http_fetch import _unwrap_content
from modules.scout.api.services.journal import describe_extract
from modules.scout.api.services.runs import create_job, explore_job
from modules.scout.api.services.snapshot_store import store_snapshot_bytes

RECORD = {'kind': 'css', 'selector': '.card'}
GAP = '''
<div class="card"><b class="title">A</b></div>
<div class="card"><b class="title">B</b><span class="price">200</span></div>
<div class="card"><b class="title">C</b></div>
'''.encode()
RULES = [
    {'name': 'title', 'kind': 'css', 'selector': '.title', 'scope': 'record', 'many': False},
    {'name': 'price', 'kind': 'css', 'selector': '.price', 'scope': 'record', 'many': False},
]


class CardExtractTests(SimpleTestCase):
    def test_missing_price_stays_on_its_card(self):
        rows = extract_html(GAP, RULES, record=RECORD)
        self.assertEqual(
            [(row['values']['title']['normalized'], row['values']['price']['normalized']) for row in rows],
            [('A', ''), ('B', '200'), ('C', '')],
        )

    def test_two_prices_stay_on_one_card(self):
        body = b'<div class="card"><b class="title">A</b><span class="price">1</span><span class="price">2</span></div>'
        rules = [
            {'name': 'title', 'kind': 'css', 'selector': '.title', 'scope': 'record', 'many': False},
            {'name': 'price', 'kind': 'css', 'selector': '.price', 'scope': 'record', 'many': True},
        ]
        rows = extract_html(body, rules, record=RECORD)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['values']['price']['normalized'], '1 | 2')

    def test_xpath_string_and_count_are_scalars(self):
        body = b'<h1>Machine</h1>'
        string_row = extract_html(body, [{
            'name': 'title', 'kind': 'xpath', 'selector': 'string(//h1)', 'scope': 'page', 'many': False,
        }])
        self.assertEqual(string_row[0]['values']['title']['normalized'], 'Machine')
        count_row = extract_html(body, [{
            'name': 'total', 'kind': 'xpath', 'selector': 'count(//h1)', 'scope': 'page', 'many': False,
        }])
        self.assertEqual(count_row[0]['values']['total']['normalized'], '1')

    def test_declared_charset_is_used(self):
        body = '<meta charset="iso-8859-1"><h1>Café</h1>'.encode('iso-8859-1')
        rows = extract_html(body, [{
            'name': 'title', 'kind': 'xpath', 'selector': '//h1', 'scope': 'page', 'many': False,
        }])
        self.assertEqual(rows[0]['values']['title']['normalized'], 'Café')

    def test_absolute_xpath_does_not_leave_the_card(self):
        with self.assertRaises(ScoutError):
            extract_html(GAP, [{
                'name': 'title', 'kind': 'xpath', 'selector': '/html/body', 'scope': 'record', 'many': False,
            }], record=RECORD)


class JournalTests(SimpleTestCase):
    def test_preview_asks_to_confirm_instead_of_reloading(self):
        lines = describe_extract('<h1>Мышь</h1>'.encode('utf-8'), [{
            'name': 'title', 'kind': 'xpath', 'selector': '//h1', 'scope': 'page', 'many': False,
        }], 0, status='awaiting_confirmation', approved=False)
        text = '\n'.join(lines)
        self.assertIn('Подтвердите план', text)
        self.assertNotIn('Загрузите страницу', text)


class FetchLimitTests(SimpleTestCase):
    def test_deflate_stops_at_the_size_limit(self):
        raw = b'<html>' + (b'a' * 5000) + b'</html>'
        packed = zlib.compress(raw)
        self.assertEqual(_unwrap_content(packed, 'deflate', 20000), raw)
        with self.assertRaises(FetchError) as caught:
            _unwrap_content(packed, 'deflate', 100)
        self.assertEqual(caught.exception.code, 'too_large')


class SnapshotStoreTests(SimpleTestCase):
    def test_parallel_writes_of_the_same_body_succeed(self):
        body = b'<html>same-body</html>'
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _index: store_snapshot_bytes(body), range(8)))
        self.assertEqual({item[0] for item in results}, {results[0][0]})


class FetchKeyTests(TestCase):
    def test_long_url_does_not_become_the_task_key(self):
        from django.contrib.auth import get_user_model

        user = get_user_model().objects.create_superuser(
            username='scout-long-url',
            email='scout-long-url@example.com',
            password='test-pass-123',
        )
        url = 'https://example.com/' + ('a' * 220)
        job = create_job(
            user_public_id=user.public_id,
            title='Длинный адрес',
            seed_urls=[url],
            fields=[{'name': 'title', 'kind': 'css', 'selector': 'h1', 'scope': 'page', 'many': False}],
        )
        run = explore_job(job=job, user_public_id=user.public_id)
        task = run.tasks.get()
        self.assertLess(len(task.idempotency_key), 80)
        self.assertEqual(task.payload['url'], url)
        self.assertNotIn(url, task.idempotency_key)
