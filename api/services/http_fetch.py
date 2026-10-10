"""Загрузка HTML с потолком размера. Локальный адрес разрешён для проверочного стенда."""

import logging
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
            body = _read_limited(response, max_bytes)
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
