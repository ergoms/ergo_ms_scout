"""Сетевые границы загрузчика Scout и проверка адреса фактического соединения."""

import http.client
import ipaddress
import os
import socket
from time import monotonic
from urllib.parse import urlsplit
from urllib.request import HTTPHandler, HTTPSHandler, ProxyHandler

from .errors import FetchError
from .http_fetch import fetch_html, fetch_xml, fetch_text

_IPV6_UNICAST = ipaddress.ip_network('2000::/3')


def _trusted_ip(host, port):
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return None
    endpoint = f'[{ip}]:{port}' if ip.version == 6 else f'{ip}:{port}'
    configured = os.environ.get('SCOUT_TRUSTED_FETCH_ENDPOINTS', '').split(',')
    if any(endpoint == value.strip() for value in configured):
        return ip
    return None


def public_url_allowed(url):
    try:
        parts = urlsplit(url)
        default_port = 443 if parts.scheme == 'https' else 80
        port = parts.port
        if (
            parts.scheme in ('http', 'https')
            and parts.hostname
            and parts.username is None
            and '%' not in parts.netloc
            and not any(char.isspace() or ord(char) < 32 or char == '\\' for char in url)
            and (port in (None, default_port) or _trusted_ip(parts.hostname, port) is not None)
        ):
            return True
    except ValueError:
        pass
    raise FetchError('url_blocked', 'Нужен публичный HTTP(S)-адрес страницы', retryable=False)


def fetch_public_html(url, *, timeout_seconds, max_bytes, url_allowed=None):
    return _fetch_public(
        fetch_html, url, timeout_seconds=timeout_seconds, max_bytes=max_bytes, url_allowed=url_allowed,
    )


def fetch_public_xml(url, *, timeout_seconds, max_bytes, url_allowed=None):
    return _fetch_public(
        fetch_xml, url, timeout_seconds=timeout_seconds, max_bytes=max_bytes, url_allowed=url_allowed,
    )


def fetch_public_text(url, *, timeout_seconds, max_bytes, url_allowed=None):
    return _fetch_public(
        fetch_text, url, timeout_seconds=timeout_seconds, max_bytes=max_bytes, url_allowed=url_allowed,
    )


def _fetch_public(fetch, url, *, timeout_seconds, max_bytes, url_allowed):
    def target_allowed(target):
        return (url_allowed is None or url_allowed(target)) and public_url_allowed(target)

    return fetch(
        url, timeout_seconds=timeout_seconds, max_bytes=max_bytes, url_allowed=target_allowed,
        handlers=(ProxyHandler({}), _PublicHTTPHandler(), _PublicHTTPSHandler()),
    )


def _public_address(address):
    ip = ipaddress.ip_address(address)
    if ip.version == 6:
        if ip.ipv4_mapped is not None:
            return _public_address(str(ip.ipv4_mapped))
        if ip not in _IPV6_UNICAST or ip.sixtofour is not None or ip.teredo is not None:
            return False
    return ip.is_global and not (ip.is_multicast or ip.is_reserved)


def _connect_public(address, timeout, source_address=None):
    deadline = monotonic() + timeout
    trusted_ip = _trusted_ip(*address)
    addresses = socket.getaddrinfo(*address, type=socket.SOCK_STREAM)
    if not addresses or any(
        ipaddress.ip_address(info[4][0]) != trusted_ip and not _public_address(info[4][0])
        for info in addresses
    ):
        raise FetchError('url_blocked', 'Адрес ведёт в закрытую сеть', retryable=False)

    # Подключаемся к уже проверенному IP без повторного разрешения имени.
    last_error = None
    for family, kind, protocol, _, sockaddr in addresses:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError('HTTP connection timed out')
        connection = socket.socket(family, kind, protocol)
        try:
            connection.settimeout(remaining)
            if source_address is not None:
                connection.bind(source_address)
            connection.connect(sockaddr)
            return connection
        except OSError as exc:
            connection.close()
            last_error = exc
    raise last_error


class _PublicHTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _connect_public


class _PublicHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _connect_public


class _PublicHTTPHandler(HTTPHandler):
    def http_open(self, request):
        return self.do_open(_PublicHTTPConnection, request)


class _PublicHTTPSHandler(HTTPSHandler):
    def https_open(self, request):
        return self.do_open(_PublicHTTPSConnection, request, context=self._context)
