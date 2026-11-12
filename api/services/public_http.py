"""Сетевые границы загрузчика Scout и проверка адреса фактического соединения."""

import http.client
import ipaddress
import os
import socket
from concurrent.futures import Future
from contextlib import nullcontext
from functools import partial
from threading import BoundedSemaphore, Lock, Thread, Timer
from time import monotonic
from urllib.parse import urlsplit
from urllib.request import HTTPHandler, HTTPSHandler, ProxyHandler

from .errors import FetchError
from .http_fetch import fetch_html, fetch_xml, fetch_text

_IPV6_UNICAST = ipaddress.ip_network('2000::/3')
_DNS_SLOTS = BoundedSemaphore(8)


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


def fetch_public_html(url, *, timeout_seconds, max_bytes, url_allowed=None, whole_timeout=False):
    return _fetch_public(
        fetch_html, url, timeout_seconds=timeout_seconds, max_bytes=max_bytes, url_allowed=url_allowed,
        whole_timeout=whole_timeout,
    )


def fetch_public_xml(url, *, timeout_seconds, max_bytes, url_allowed=None, whole_timeout=False):
    return _fetch_public(
        fetch_xml, url, timeout_seconds=timeout_seconds, max_bytes=max_bytes, url_allowed=url_allowed,
        whole_timeout=whole_timeout,
    )


def fetch_public_text(url, *, timeout_seconds, max_bytes, url_allowed=None, whole_timeout=False):
    return _fetch_public(
        fetch_text, url, timeout_seconds=timeout_seconds, max_bytes=max_bytes, url_allowed=url_allowed,
        whole_timeout=whole_timeout,
    )


def _fetch_public(fetch, url, *, timeout_seconds, max_bytes, url_allowed, whole_timeout):
    def target_allowed(target):
        return (url_allowed is None or url_allowed(target)) and public_url_allowed(target)

    guard = SocketDeadline(timeout_seconds) if whole_timeout else nullcontext()
    with guard as deadline:
        return fetch(
            url, timeout_seconds=timeout_seconds, max_bytes=max_bytes, url_allowed=target_allowed,
            handlers=(ProxyHandler({}), _PublicHTTPHandler(deadline), _PublicHTTPSHandler(deadline)),
        )


class SocketDeadline:
    """Один срок для соединения, ответа и всех HTTP-переходов попытки."""

    def __init__(self, timeout):
        self.ends_at = monotonic() + timeout
        self._lock = Lock()
        self._socket = None
        self._timer = Timer(timeout, self._expire)
        self._timer.daemon = True

    def __enter__(self):
        self._timer.start()
        return self

    def __exit__(self, *_exc):
        self._timer.cancel()
        with self._lock:
            if self._socket is not None:
                self._socket.close()
                self._socket = None

    def remaining(self):
        remaining = self.ends_at - monotonic()
        if remaining <= 0:
            raise TimeoutError('HTTP request timed out')
        return remaining

    def watch(self, connection):
        with self._lock:
            self.remaining()
            # TLS забирает дескриптор исходного сокета. Дубликат прерывает то же соединение.
            watched = connection.dup()
            if self._socket is not None:
                self._socket.close()
            self._socket = watched

    def connect(self, address, timeout, source_address=None):
        connection = _connect_public(address, timeout, source_address, deadline=self.ends_at)
        try:
            self.watch(connection)
        except OSError:
            connection.close()
            raise
        return connection

    def _expire(self):
        with self._lock:
            if self._socket is not None:
                try:
                    self._socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass


def _public_address(address):
    ip = ipaddress.ip_address(address)
    if ip.version == 6:
        if ip.ipv4_mapped is not None:
            return _public_address(str(ip.ipv4_mapped))
        if ip not in _IPV6_UNICAST or ip.sixtofour is not None or ip.teredo is not None:
            return False
    return ip.is_global and not (ip.is_multicast or ip.is_reserved)


def resolve_tcp_addresses(host, port, *, deadline):
    """Ждём DNS до дедлайна, удерживая место до выхода системного вызова."""
    if monotonic() >= deadline:
        raise TimeoutError('DNS lookup timed out')
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, flags=socket.AI_NUMERICHOST)
    if not _DNS_SLOTS.acquire(blocking=False):
        raise OSError('DNS resolver is busy')
    result = Future()

    def resolve():
        try:
            result.set_result(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
        except Exception as exc:
            result.set_exception(exc)
        finally:
            _DNS_SLOTS.release()

    try:
        Thread(target=resolve, daemon=True).start()
    except RuntimeError as exc:
        _DNS_SLOTS.release()
        raise OSError('Could not start DNS resolver') from exc
    addresses = result.result(timeout=max(0, deadline - monotonic()))
    if monotonic() >= deadline:
        raise TimeoutError('DNS lookup timed out')
    return addresses


def _connect_public(address, timeout, source_address=None, *, deadline=None):
    connect_until = monotonic() + timeout
    trusted_ip = _trusted_ip(*address)
    if deadline is None:
        addresses = socket.getaddrinfo(*address, type=socket.SOCK_STREAM)
    else:
        connect_until = min(connect_until, deadline)
        addresses = resolve_tcp_addresses(*address, deadline=connect_until)
    if not addresses or any(
        ipaddress.ip_address(info[4][0]) != trusted_ip and not _public_address(info[4][0])
        for info in addresses
    ):
        raise FetchError('url_blocked', 'Адрес ведёт в закрытую сеть', retryable=False)

    # Подключаемся к уже проверенному IP без повторного разрешения имени.
    last_error = None
    for family, kind, protocol, _, sockaddr in addresses:
        remaining = connect_until - monotonic()
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
    def __init__(self, *args, deadline=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _connect_public if deadline is None else deadline.connect


class _PublicHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, deadline=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _connect_public if deadline is None else deadline.connect


class _PublicHTTPHandler(HTTPHandler):
    def __init__(self, deadline=None):
        super().__init__()
        self._connect = partial(_PublicHTTPConnection, deadline=deadline)

    def http_open(self, request):
        return self.do_open(self._connect, request)


class _PublicHTTPSHandler(HTTPSHandler):
    def __init__(self, deadline=None):
        super().__init__()
        self._connect = partial(_PublicHTTPSConnection, deadline=deadline)

    def https_open(self, request):
        return self.do_open(self._connect, request, context=self._context)
