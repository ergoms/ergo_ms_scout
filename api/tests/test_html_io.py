"""Регрессии HTTP-загрузки, кодировки и разбора ссылок."""

import io
import socket
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from modules.scout.api.services.errors import FetchError
from modules.scout.api.services.html_extract import detected_charset, extract_html
from modules.scout.api.services.page_links import has_crawl_links, iter_hrefs
from modules.scout.api.services.public_http import fetch_public_html
from modules.scout.api.services.visual_rules import VisualDocument

_URL = 'http://example.com/catalog/index.html'
_BODY = b'<h1>Catalog</h1>'
_FIELDS = [{'name': 'title', 'kind': 'css', 'selector': 'h1', 'scope': 'page', 'many': False}]


def _connection(headers: bytes, body: bytes = _BODY):
    socket = Mock()
    socket.makefile.return_value = io.BytesIO(
        b'HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n' + headers + b'\r\n' + body,
    )
    return socket


class HTTPBodyTests(SimpleTestCase):
    def test_whole_timeout_stops_waiting_for_blocked_dns(self):
        resolving = Event()
        release_dns = Event()
        resolved = Event()

        def resolve(*args, **kwargs):
            resolving.set()
            release_dns.wait()
            resolved.set()
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('93.184.216.34', 80))]

        with (
            patch.object(socket, 'getaddrinfo', side_effect=resolve),
            patch.object(socket, 'socket', side_effect=AssertionError('TCP connection attempted')) as connect,
            ThreadPoolExecutor(max_workers=1) as executor,
        ):
            attempt = executor.submit(fetch_public_html, _URL, timeout_seconds=0.5, max_bytes=4096, whole_timeout=True)
            try:
                self.assertTrue(resolving.wait(timeout=3))
                with self.assertRaises(FetchError) as error:
                    attempt.result(timeout=3)
                self.assertEqual(error.exception.code, 'network')
                self.assertTrue(error.exception.retryable)
                self.assertFalse(resolved.is_set())
            finally:
                release_dns.set()

            self.assertTrue(resolved.wait(timeout=3))
            connect.assert_not_called()

    def test_premature_eof_is_a_retryable_network_error(self):
        socket = _connection(b'Content-Length: 1000\r\n')
        with (
            patch('modules.scout.api.services.public_http._connect_public', return_value=socket),
            self.assertRaises(FetchError) as error,
        ):
            fetch_public_html(_URL, timeout_seconds=1, max_bytes=4096)
        self.assertEqual(error.exception.code, 'network')
        self.assertTrue(error.exception.retryable)

    def test_complete_content_length_and_chunked_body_are_accepted(self):
        cases = [
            _connection(f'Content-Length: {len(_BODY)}\r\n'.encode('ascii')),
            _connection(
                b'Transfer-Encoding: chunked\r\nContent-Length: 1000\r\n',
                f'{len(_BODY):x}\r\n'.encode('ascii') + _BODY + b'\r\n0\r\n\r\n',
            ),
        ]
        for socket in cases:
            with self.subTest(socket=socket), patch(
                'modules.scout.api.services.public_http._connect_public', return_value=socket,
            ):
                result = fetch_public_html(_URL, timeout_seconds=1, max_bytes=4096)
                self.assertEqual(result['body'], _BODY)

    def test_unicode_host_path_and_query_are_encoded_only_for_http(self):
        url = 'http://пример.рф/каталог/a%2Fb?q=ёж+ok&q=%20'
        socket = _connection(f'Content-Length: {len(_BODY)}\r\n'.encode('ascii'))
        with patch('modules.scout.api.services.public_http._connect_public', return_value=socket) as connect:
            result = fetch_public_html(url, timeout_seconds=1, max_bytes=4096)

        self.assertEqual(connect.call_args.args[0], ('xn--e1afmkfd.xn--p1ai', 80))
        request = socket.sendall.call_args.args[0]
        self.assertIn(b'Host: xn--e1afmkfd.xn--p1ai\r\n', request)
        self.assertIn(b'GET /%D0%BA%D0%B0%D1%82%D0%B0%D0%BB%D0%BE%D0%B3/a%2Fb?q=%D1%91%D0%B6+ok&q=%20 HTTP/', request)
        self.assertEqual(result['final_url'], url)


class HTMLParsingTests(SimpleTestCase):
    def test_charset_mentions_in_scripts_and_other_attributes_are_ignored(self):
        body = (
            '<meta data-charset="windows-1251">'
            '<script>const charset = "windows-1251"; const example = \'<meta charset="windows-1251">\';</script>'
            '<h1>Каталог</h1>'
        ).encode('utf-8')
        self.assertEqual(detected_charset(body), 'utf-8')
        self.assertEqual(extract_html(body, _FIELDS)[0]['values']['title']['normalized'], 'Каталог')

    def test_meta_http_equiv_still_sets_the_encoding(self):
        body = '<meta content="text/html; charset=ISO-8859-1" http-equiv="Content-Type"><h1>Café</h1>'.encode('latin-1')
        self.assertEqual(extract_html(body, _FIELDS)[0]['values']['title']['normalized'], 'Café')

    def test_xhtml_encoding_declaration_works_for_extract_visual_and_links(self):
        body = (
            '<?xml version="1.0" encoding="ISO-8859-1"?>'
            '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
            '<h1>Café</h1><a href="next.html">Next</a></body></html>'
        ).encode('latin-1')
        content_type = 'application/xhtml+xml'
        charset = detected_charset(body, content_type)
        self.assertEqual(charset, 'ISO-8859-1')
        rows = extract_html(body, _FIELDS, content_type=content_type)
        self.assertEqual(rows[0]['values']['title']['normalized'], 'Café')
        self.assertIn('Café', VisualDocument(body, charset).editor_html())
        self.assertEqual(iter_hrefs(body, content_type, charset, _URL), (['next.html'], _URL))

    def test_invalid_base_keeps_links_and_the_page_url(self):
        for base in ('http://[broken', 'http://example.com:invalid/', 'data:text/plain,x'):
            with self.subTest(base=base):
                body = f'<base href="{base}"><a href="next.html">Next</a>'.encode('ascii')
                self.assertTrue(has_crawl_links(body, 'text/html', '', _URL))
                self.assertEqual(iter_hrefs(body, 'text/html', '', _URL), (['next.html'], _URL))

    def test_valid_relative_base_is_kept(self):
        body = b'<base href="../archive/"><a href="next.html">Next</a>'
        self.assertEqual(iter_hrefs(body, 'text/html', '', _URL), (['next.html'], 'http://example.com/archive/'))
