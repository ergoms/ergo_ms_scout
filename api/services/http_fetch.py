"""Загрузка HTML с потолком размера. Локальный адрес разрешён для проверочного стенда."""

import gzip
import io
import logging
import zlib
from urllib import error as urlerror
from urllib import request as urlrequest

from django.utils.translation import gettext as _

from .errors import FetchError

logger = logging.getLogger('modules.scout')

_HTML_TYPES = ('text/html', 'application/xhtml+xml')


class _LimitedRedirect(urlrequest.HTTPRedirectHandler):
    def __init__(self):
        super().__init__()
        self.redirects = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.redirects += 1
        if self.redirects > 5:
            raise FetchError('too_many_redirects', _('Слишком много переадресаций'), retryable=False)
        if not str(newurl).startswith(('http://', 'https://')):
            raise FetchError('bad_redirect', _('Переадресация ведёт не на http-адрес'), retryable=False)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_html(url: str, *, timeout_seconds: int, max_bytes: int) -> dict:
    opener = urlrequest.build_opener(_LimitedRedirect())
    req = urlrequest.Request(
        url,
        headers={'User-Agent': 'ERGO-SCOUT/0.1', 'Accept': 'text/html,application/xhtml+xml'},
    )
    try:
        with opener.open(req, timeout=timeout_seconds) as response:
            status = getattr(response, 'status', None) or response.getcode()
            final_url = response.geturl()
            content_type = response.headers.get('Content-Type', '')
            encoding = response.headers.get('Content-Encoding', '')
            body = _unwrap_content(_read_limited(response, max_bytes), encoding, max_bytes)
    except FetchError:
        raise
    except urlerror.HTTPError as exc:
        retryable = exc.code in (429, 500, 502, 503, 504)
        raise FetchError(
            f'http_{exc.code}',
            _('Страница ответила кодом %(code)s') % {'code': exc.code},
            retryable=retryable,
        ) from exc
    except (urlerror.URLError, TimeoutError, OSError) as exc:
        logger.warning('SCOUT не загрузил страницу: %s', type(exc).__name__)
        raise FetchError('network', _('Не удалось загрузить страницу'), retryable=True) from exc

    media = content_type.split(';', 1)[0].strip().lower()
    if media and media not in _HTML_TYPES:
        raise FetchError('not_html', _('Ответ не является HTML'), retryable=False)
    return {
        'status': int(status or 0),
        'final_url': final_url or url,
        'content_type': content_type[:120],
        'body': body,
    }


def _unwrap_content(body: bytes, content_encoding: str, max_bytes: int) -> bytes:
    """Сервер может отдать gzip, даже если распаковку не просили. В снимок кладём HTML."""
    encoding = (content_encoding or '').lower()
    if body.startswith(b'\x1f\x8b') or 'gzip' in encoding:
        return _read_capped(_gzip_stream(body), max_bytes)
    if 'deflate' in encoding:
        return _read_capped(_deflate_stream(body), max_bytes)
    if len(body) > max_bytes:
        raise FetchError('too_large', _('Страница больше допустимого размера'), retryable=False)
    return body


def _gzip_stream(body: bytes):
    try:
        return gzip.GzipFile(fileobj=io.BytesIO(body))
    except OSError as exc:
        raise FetchError('bad_encoding', _('Сжатый ответ не распаковывается'), retryable=False) from exc


def _deflate_stream(body: bytes):
    try:
        return io.BytesIO(zlib.decompress(body))
    except zlib.error:
        try:
            return io.BytesIO(zlib.decompress(body, -zlib.MAX_WBITS))
        except zlib.error as exc:
            raise FetchError('bad_encoding', _('Сжатый ответ не распаковывается'), retryable=False) from exc


def _read_capped(stream, max_bytes: int) -> bytes:
    try:
        body = stream.read(max_bytes + 1)
    except OSError as exc:
        raise FetchError('bad_encoding', _('Сжатый ответ не распаковывается'), retryable=False) from exc
    finally:
        close = getattr(stream, 'close', None)
        if close:
            close()
    if len(body) > max_bytes:
        raise FetchError('too_large', _('Страница больше допустимого размера'), retryable=False)
    return body


def _read_limited(response, max_bytes: int) -> bytes:
    chunks = []
    total = 0
    while True:
        piece = response.read(64 * 1024)
        if not piece:
            break
        total += len(piece)
        if total > max_bytes:
            raise FetchError('too_large', _('Страница больше допустимого размера'), retryable=False)
        chunks.append(piece)
    return b''.join(chunks)
