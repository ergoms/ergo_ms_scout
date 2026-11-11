"""Проверки аудита: карточки, XPath, кодировка, журнал, сжатие и снимок."""

import csv
import io
import zlib
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import UUID

from django.test import SimpleTestCase, TestCase

from modules.scout.api.services.constants import FIELD_TEXT_LIMIT
from modules.scout.api.services.errors import FetchError, ScoutError
from modules.scout.api.services.exporting import _write_csv
from modules.scout.api.services.html_extract import _parse, extract_html
from modules.scout.api.services.http_fetch import _unwrap_content
from modules.scout.api.services.journal import describe_extract
from modules.scout.api.services.plan import normalize_plan, plan_hash
from modules.scout.api.services.runs import create_job, explore_job
from modules.scout.api.services.snapshot_store import store_snapshot_bytes
from modules.scout.api.services.value_types import typed_cell

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

    def test_xpath_union_stays_inside_the_card(self):
        rows = extract_html(GAP, [{
            'name': 'bits', 'kind': 'xpath', 'selector': '//b | //span', 'scope': 'record', 'many': True,
        }], record=RECORD)
        self.assertEqual(rows[0]['values']['bits']['normalized'], 'A')
        self.assertEqual(rows[1]['values']['bits']['normalized'], 'B | 200')
        self.assertNotIn('200', rows[0]['values']['bits']['normalized'])

    def test_xpath_string_reads_each_card(self):
        rows = extract_html(GAP, [{
            'name': 'title', 'kind': 'xpath', 'selector': 'string(.//b)', 'scope': 'record', 'many': False,
        }], record=RECORD)
        self.assertEqual(
            [row['values']['title']['normalized'] for row in rows],
            ['A', 'B', 'C'],
        )

    def test_scalar_container_is_rejected(self):
        with self.assertRaises(ScoutError):
            extract_html(GAP, RULES, record={'kind': 'xpath', 'selector': 'string(//b)'})

    def test_utf16_bom_is_decoded(self):
        body = '<h1>Café</h1>'.encode('utf-16')
        rows = extract_html(body, [{
            'name': 'title', 'kind': 'xpath', 'selector': '//h1', 'scope': 'page', 'many': False,
        }])
        self.assertEqual(rows[0]['values']['title']['normalized'], 'Café')


class TypedValueTests(SimpleTestCase):
    def test_plan_checks_conversion_and_csv_column_collisions(self):
        url = ['https://example.com/catalog']
        field = {'name': 'price', 'kind': 'css', 'selector': '.price'}
        money = {'type': 'money', 'currency': 'RUB', 'decimal_separator': ','}
        legacy = normalize_plan(url, [field])
        typed = normalize_plan(url, [{**field, 'conversion': money}])
        self.assertEqual(typed['fields'][0]['conversion'], money)
        self.assertNotEqual(plan_hash(legacy), plan_hash(typed))
        for invalid in (
            [{**field, 'conversion': money, 'many': True}],
            [{**field, 'conversion': {**money, 'decimal_separator': ';'}}],
            [{**field, 'conversion': {'type': 'date', 'format': 'MM/DD/YYYY'}}],
            [{**field, 'conversion': money}, {**field, 'name': 'price__status'}],
            [{**field, 'conversion': money}, {**field, 'name': 'public_id'}],
        ):
            with self.subTest(fields=invalid), self.assertRaises(ScoutError):
                normalize_plan(url, invalid)

    def test_conversion_preserves_precision_and_validates_range_and_format(self):
        money = {'type': 'money', 'currency': 'RUB', 'decimal_separator': ','}
        integer = {'type': 'integer'}
        calendar_date = {'type': 'date', 'format': 'DD.MM.YYYY'}
        cases = (
            ('9\u00a0999\u202f999,5 RUB', money, {'amount': '9999999.50', 'currency': 'RUB'}, None),
            ('1,01 USD', money, None, 'currency_mismatch'),
            ('1,01 €', money, None, 'currency_mismatch'),
            ('1,001 ₽', money, None, 'invalid_decimal'),
            ('1 23,45 ₽', money, None, 'invalid_decimal'),
            ('2147483647', integer, 2147483647, None),
            ('2147483648', integer, None, 'value_out_of_range'),
            ('-1', integer, None, 'value_out_of_range'),
            ('29.02.2024', calendar_date, '2024-02-29', None),
            ('29.02.2026', calendar_date, None, 'invalid_date'),
            ('6.11.2026', calendar_date, None, 'invalid_date'),
        )
        for text, conversion, value, error in cases:
            with self.subTest(text=text):
                self.assertEqual(typed_cell(text, conversion), {
                    'type': conversion['type'], 'status': 'invalid' if error else 'ok',
                    'value': value, 'error': {'code': error} if error else None,
                })

    def test_xpath_empty_and_missing_and_truncated_keep_legacy_identity(self):
        body = ('<p>' + '0' * (FIELD_TEXT_LIMIT + 1) + '</p>').encode()
        rules = [
            {'name': 'missing', 'kind': 'xpath', 'selector': '//i'},
            {'name': 'empty', 'kind': 'xpath', 'selector': 'string(//i)'},
            {'name': 'large', 'kind': 'xpath', 'selector': '//p'},
        ]
        legacy = extract_html(body, rules)[0]
        typed = extract_html(body, [{**rule, 'conversion': {'type': 'integer'}} for rule in rules])[0]
        self.assertEqual(typed['identity_key'], legacy['identity_key'])
        self.assertEqual(typed['evidence'], legacy['evidence'])
        self.assertEqual(typed['values']['missing']['typed']['status'], 'missing')
        self.assertEqual(typed['values']['empty']['typed']['status'], 'empty')
        self.assertEqual(typed['values']['large']['typed']['error'], {'code': 'value_truncated'})


class JournalTests(SimpleTestCase):
    def test_preview_asks_to_confirm_instead_of_reloading(self):
        lines = describe_extract('<h1>Мышь</h1>'.encode('utf-8'), [{
            'name': 'title', 'kind': 'xpath', 'selector': '//h1', 'scope': 'page', 'many': False,
        }], 0, status='awaiting_confirmation', approved=False)
        text = '\n'.join(lines)
        self.assertIn('Подтвердите план', text)
        self.assertNotIn('Загрузите страницу', text)

    def test_diagnostics_parse_once_and_count_relative_xpath_within_cards(self):
        rules = [
            {'name': 'heading', 'kind': 'css', 'selector': 'h1', 'scope': 'page', 'many': False},
            {'name': 'price', 'kind': 'xpath', 'selector': './b', 'scope': 'record', 'many': True},
        ]
        body = b'<div><b>outside</b><h1>Shop</h1><article><b>1</b><b>2</b></article><article><b>3</b></article></div>'
        with patch('modules.scout.api.services.journal._parse', wraps=_parse) as parse:
            lines = describe_extract(body, rules, 2, record={'kind': 'css', 'selector': 'article'})
        parse.assert_called_once()
        self.assertIn('совпадений 1', lines[0])
        self.assertIn('совпадений 3', lines[1])


class CsvExportTests(SimpleTestCase):
    def test_source_public_id_keeps_its_value_and_the_record_id(self):
        values = {'public_id': {'normalized': 'article-123'}, 'name': {'normalized': 'A'}}
        record_id = UUID(int=1)
        records = Mock()
        records.values_list.return_value.iterator.return_value = iter([values])
        records.iterator.return_value = iter([SimpleNamespace(public_id=record_id, values=values)])
        output = io.StringIO()

        _write_csv(records, output)

        self.assertEqual(list(csv.reader(io.StringIO(output.getvalue()))), [
            ['public_id', 'field.public_id', 'name'],
            [str(record_id), 'article-123', 'A'],
        ])


class FetchLimitTests(SimpleTestCase):
    def test_deflate_stops_at_the_size_limit(self):
        raw = b'<html>' + (b'a' * 5000) + b'</html>'
        packed = zlib.compress(raw)
        self.assertEqual(_unwrap_content(packed, 'deflate', 20000), raw)
        with self.assertRaises(FetchError) as caught:
            _unwrap_content(packed, 'deflate', 100)
        self.assertEqual(caught.exception.code, 'too_large')

    def test_truncated_deflate_and_gzip_are_rejected(self):
        import gzip
        raw = b'<html>hello</html>'
        packed = zlib.compress(raw)
        with self.assertRaises(FetchError) as deflate_error:
            _unwrap_content(packed[:-5], 'deflate', 20000)
        self.assertEqual(deflate_error.exception.code, 'bad_encoding')
        gz = gzip.compress(raw)
        with self.assertRaises(FetchError) as gzip_error:
            _unwrap_content(gz[:-5], 'gzip', 20000)
        self.assertEqual(gzip_error.exception.code, 'bad_encoding')


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

    def test_revise_saves_the_title_and_preserves_existing_limits(self):
        from django.contrib.auth import get_user_model
        from modules.scout.api.services.runs import revise_job

        user = get_user_model().objects.create_superuser(
            username='scout-title',
            email='scout-title@example.com',
            password='test-pass-123',
        )
        url = 'https://example.com/item'
        fields = [{'name': 'title', 'kind': 'css', 'selector': 'h1', 'scope': 'page', 'many': False}]
        job = create_job(
            user_public_id=user.public_id,
            title='Старое',
            seed_urls=[url],
            fields=fields,
        )
        revision = job.revisions.get()
        revision.limits = {**revision.limits, 'max_bytes': 2048, 'timeout_seconds': 7}
        revision.save(update_fields=['limits'])
        revised = revise_job(job=job, seed_urls=[url], fields=fields, title='Новое')
        job.refresh_from_db()
        self.assertEqual(job.title, 'Новое')
        self.assertEqual(revised.limits, revision.limits)


class URLIdentityTests(SimpleTestCase):
    def test_query_encoding_repeats_order_and_path_params_are_preserved(self):
        from modules.scout.api.services.url_norm import resolve_url

        url = 'HTTP://EXAMPLE.com:80/p;variant?x=%20&x=+&id=9&utm_source=a&page=2#fragment'
        normalized, reason = resolve_url(url, url, ['utm_source'])
        self.assertEqual(reason, '')
        self.assertEqual(normalized, 'http://example.com/p;variant?x=%20&x=+&id=9&page=2')

    def test_invalid_port_is_a_skipped_link(self):
        from modules.scout.api.services.url_norm import resolve_url

        self.assertEqual(resolve_url('http://example.com:bad/', 'http://example.com/', []),
                         (None, 'invalid_url'))


class HostScopeTests(SimpleTestCase):
    def test_unicode_and_punycode_are_the_same_crawl_boundary(self):
        from modules.scout.api.services.plan import normalize_plan, plan_hash
        from modules.scout.api.services.url_norm import host_allowed

        fields = [{'name': 'title', 'kind': 'css', 'selector': 'h1', 'scope': 'page', 'many': False}]
        unicode_plan = normalize_plan(
            ['https://пример.рф/path'],
            fields,
            crawl={'mode': 'site'},
        )
        puny_plan = normalize_plan(
            ['https://xn--e1afmkfd.xn--p1ai/path'],
            fields,
            crawl={'mode': 'site', 'allowed_hosts': ['пример.рф']},
        )
        self.assertEqual(unicode_plan['seed_urls'], ['https://xn--e1afmkfd.xn--p1ai/path'])
        self.assertEqual(unicode_plan['crawl']['allowed_hosts'], ['xn--e1afmkfd.xn--p1ai'])
        self.assertEqual(plan_hash(unicode_plan), plan_hash(puny_plan))
        nested = normalize_plan(
            ['https://www.пример.рф/item'],
            fields,
            crawl={'mode': 'site', 'allowed_hosts': ['xn--e1afmkfd.xn--p1ai'], 'preview_urls': ['https://под.пример.рф/a']},
        )
        self.assertEqual(nested['seed_urls'], ['https://www.xn--e1afmkfd.xn--p1ai/item'])
        self.assertEqual(nested['crawl']['preview_urls'], ['https://xn--d1atc.xn--e1afmkfd.xn--p1ai/a'])
        self.assertTrue(host_allowed('https://xn--e1afmkfd.xn--p1ai/a', ['пример.рф']))
        self.assertTrue(host_allowed('https://www.xn--e1afmkfd.xn--p1ai/a', ['пример.рф']))
        self.assertFalse(host_allowed('https://evil.test/a', ['пример.рф']))

    def test_ipv6_host_is_accepted_and_a_different_address_is_not(self):
        from modules.scout.api.services.plan import normalize_plan
        from modules.scout.api.services.url_norm import host_allowed

        fields = [{'name': 'title', 'kind': 'css', 'selector': 'h1', 'scope': 'page', 'many': False}]
        plan = normalize_plan(['http://[0:0:0:0:0:0:0:1]/page'], fields, crawl={'mode': 'site'})
        self.assertEqual(plan['seed_urls'], ['http://[::1]/page'])
        self.assertEqual(plan['crawl']['allowed_hosts'], ['::1'])
        self.assertTrue(host_allowed('http://[::1]/page', ['[::1]']))
        self.assertFalse(host_allowed('http://[2001:db8::2]/page', ['::1']))
        with self.assertRaises(ScoutError):
            normalize_plan(
                ['https://пример.рф/'],
                fields,
                crawl={'mode': 'site', 'allowed_hosts': ['example.com']},
            )
