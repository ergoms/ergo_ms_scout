"""HTTPS-прокси с проверкой фактического адреса назначения CONNECT."""

import base64
import ipaddress
import re
import socket
import ssl
from contextlib import nullcontext
from http.client import HTTPException
from threading import Timer
from time import monotonic
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPSHandler, ProxyHandler
from urllib.response import addinfourl

import urllib3
from django.views.decorators.debug import sensitive_variables
from urllib3 import exceptions as http_errors
from urllib3.connection import HTTPSConnection
from urllib3.connectionpool import HTTPSConnectionPool

from .errors import FetchError
from .http_fetch import fetch_html, fetch_xml, fetch_text
from .proxy_profiles import ip_authority, load_proxy
from .public_http import SocketDeadline, _public_address, public_url_allowed, resolve_tcp_addresses
from .url_norm import canonical_host

def _unavailable():
    return FetchError('proxy_unavailable', 'Прокси временно недоступен', retryable=True)


def _network():
    return FetchError('network', 'Не удалось загрузить страницу через прокси', retryable=True)


def _origin_ip(hostname, deadline):
    try:
        addresses = [str(ipaddress.ip_address(hostname))]
    except ValueError:
        try:
            addresses = [answer[4][0] for answer in resolve_tcp_addresses(hostname, 443, deadline=deadline)]
        except OSError:
            raise _network() from None
    if not addresses or any(not _public_address(address) for address in addresses):
        raise FetchError('url_blocked', 'Адрес сайта не разрешён', retryable=False)
    return addresses[0]


class _ProxyConnection(HTTPSConnection):
    def __init__(self, *args, scout_deadline, scout_proxy_name, scout_socket_deadline, **kwargs):
        super().__init__(*args, **kwargs)
        self.scout_deadline = scout_deadline
        self.scout_proxy_name = scout_proxy_name
        self.scout_socket_deadline = scout_socket_deadline
        self.watchdog = None

    def _new_conn(self):
        sock = super()._new_conn()
        if self.scout_socket_deadline is not None:
            try:
                self.scout_socket_deadline.watch(sock)
            except OSError:
                sock.close()
                raise
        return sock

    def _connect_tls_proxy(self, hostname, sock):
        remaining = self.scout_deadline - monotonic()
        if remaining <= 0:
            raise _unavailable()
        sock.settimeout(remaining)
        try:
            return super()._connect_tls_proxy(self.scout_proxy_name, sock)
        except (ssl.SSLError, ssl.CertificateError, http_errors.SSLError):
            raise FetchError('proxy_tls_error', 'Не удалось проверить TLS прокси', retryable=False) from None

    def _tunnel(self):
        sock = self.sock

        def expire():
            try:
                socket.socket.shutdown(sock, socket.SHUT_RDWR)
            except OSError:
                pass

        remaining = self.scout_deadline - monotonic()
        if remaining <= 0:
            raise _unavailable()
        if self.scout_socket_deadline is None:
            self.watchdog = Timer(remaining, expire)
            self.watchdog.daemon = True
            self.watchdog.start()
        try:
            super()._tunnel()
            if monotonic() >= self.scout_deadline:
                raise _unavailable()
        except OSError as exc:
            status = re.match(r'^Tunnel connection failed: (\d{3}) ', str(exc))
            code = status[1] if status else ''
            if code == '407':
                raise FetchError('proxy_auth_failed', 'Прокси отклонил реквизиты', retryable=False) from None
            if not code or code in ('502', '503', '504'):
                raise _unavailable() from None
            raise FetchError('proxy_blocked', 'Прокси отклонил подключение', retryable=False) from None
        except (http_errors.HTTPError, HTTPException):
            raise _unavailable() from None

    def close(self):
        if self.watchdog is not None:
            self.watchdog.cancel()
        super().close()


class _ProxyPool(HTTPSConnectionPool):
    ConnectionCls = _ProxyConnection


class _ProxyResponse(addinfourl):
    def __init__(self, response, url, manager):
        super().__init__(response, response.headers, url, response.status)
        self.msg = response.reason
        self.manager = manager

    def read(self, *args):
        try:
            return self.fp.read(*args)
        except (http_errors.HTTPError, OSError):
            raise _network() from None

    def close(self):
        try:
            super().close()
        finally:
            self.manager.clear()


class _ProxyHTTPSHandler(HTTPSHandler):
    @sensitive_variables()
    def __init__(self, revision_id, owner, timeout, socket_deadline):
        super().__init__()
        self.revision_id = revision_id
        self.owner = owner
        self.socket_deadline = socket_deadline
        self.deadline = monotonic() + timeout if socket_deadline is None else socket_deadline.ends_at

    @sensitive_variables()
    def https_open(self, request):
        parts = urlsplit(request.full_url)
        hostname = canonical_host(parts.hostname)
        if hostname is None:
            raise FetchError('url_blocked', 'Адрес сайта не разрешён', retryable=False)
        destination = _origin_ip(hostname, self.deadline)
        proxy = load_proxy(self.revision_id, self.owner)
        proxy_headers = {}
        if proxy.username is not None:
            credentials = base64.b64encode(f'{proxy.username}:{proxy.password}'.encode('ascii')).decode('ascii')
            proxy_headers['Proxy-Authorization'] = f'Basic {credentials}'
        manager = urllib3.ProxyManager(
            f'https://{ip_authority(proxy.ip, proxy.port)}',
            proxy_headers=proxy_headers,
            proxy_ssl_context=ssl.create_default_context(),
            ssl_context=ssl.create_default_context(), server_hostname=hostname,
        )
        manager.pool_classes_by_scheme = {'https': _ProxyPool}
        target = urlunsplit(('https', ip_authority(destination, 443), parts.path or '/', parts.query, ''))
        manager.connection_from_url(target).conn_kw.update(
            scout_deadline=self.deadline, scout_proxy_name=proxy.tls_name,
            scout_socket_deadline=self.socket_deadline,
        )
        headers = dict(request.header_items())
        headers['Host'] = f'[{hostname}]' if ':' in hostname else hostname
        remaining = self.deadline - monotonic()
        try:
            if remaining <= 0:
                raise _unavailable()
            response = manager.request(
                'GET', target, headers=headers, redirect=False, retries=False,
                preload_content=False, decode_content=False, timeout=urllib3.Timeout(total=remaining),
            )
        except (http_errors.ProxyError, http_errors.ConnectTimeoutError):
            manager.clear()
            raise _unavailable() from None
        except (http_errors.SSLError, ssl.SSLError):
            manager.clear()
            raise FetchError('network', 'Не удалось проверить TLS источника', retryable=False) from None
        except (http_errors.HTTPError, OSError):
            manager.clear()
            raise _network() from None
        except FetchError:
            manager.clear()
            raise
        return _ProxyResponse(response, request.full_url, manager)


def proxy_target_allowed(url):
    public_url_allowed(url)
    parts = urlsplit(url)
    if parts.scheme != 'https' or parts.port not in (None, 443):
        raise FetchError('url_blocked', 'Через прокси доступен HTTPS на порту 443', retryable=False)
    return True


def fetch_proxy_html(url, *, revision_id, owner, timeout_seconds, max_bytes, url_allowed=None, same_origin=False,
                     whole_timeout=False):
    return _fetch_proxy(
        fetch_html, url, revision_id=revision_id, owner=owner, timeout_seconds=timeout_seconds,
        max_bytes=max_bytes, url_allowed=url_allowed, same_origin=same_origin, whole_timeout=whole_timeout,
    )


def fetch_proxy_xml(url, *, revision_id, owner, timeout_seconds, max_bytes, url_allowed=None, whole_timeout=False):
    return _fetch_proxy(
        fetch_xml, url, revision_id=revision_id, owner=owner, timeout_seconds=timeout_seconds,
        max_bytes=max_bytes, url_allowed=url_allowed, same_origin=False, whole_timeout=whole_timeout,
    )


def fetch_proxy_text(url, *, revision_id, owner, timeout_seconds, max_bytes, url_allowed=None, whole_timeout=False):
    return _fetch_proxy(
        fetch_text, url, revision_id=revision_id, owner=owner, timeout_seconds=timeout_seconds,
        max_bytes=max_bytes, url_allowed=url_allowed, same_origin=False, whole_timeout=whole_timeout,
    )


def _fetch_proxy(fetch, url, *, revision_id, owner, timeout_seconds, max_bytes, url_allowed, same_origin, whole_timeout):
    def target_allowed(target):
        return (
            proxy_target_allowed(target)
            and (
                not same_origin
                or canonical_host(urlsplit(target).hostname) == canonical_host(urlsplit(url).hostname)
            )
            and (url_allowed is None or url_allowed(target))
        )

    try:
        with SocketDeadline(timeout_seconds) if whole_timeout else nullcontext() as deadline:
            result = fetch(
                url, timeout_seconds=timeout_seconds, max_bytes=max_bytes, url_allowed=target_allowed,
                handlers=(ProxyHandler({}), _ProxyHTTPSHandler(revision_id, owner, timeout_seconds, deadline)),
            )
            if deadline is not None:
                deadline.remaining()
            return result
    except FetchError:
        raise
    except TimeoutError:
        raise _network() from None
    except Exception:
        # Подробности стороннего транспорта могут содержать заголовок прокси.
        raise FetchError('network', 'Не удалось загрузить страницу через прокси', retryable=False) from None
