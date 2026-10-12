"""Нормализация адреса для обхода. Длинный URL не становится ключом задачи."""

import hashlib
from urllib.parse import unquote_plus, urljoin, urlparse, urlunparse

_SKIP_SUFFIXES = {
    'pdf', 'jpg', 'jpeg', 'png', 'gif', 'webp', 'svg', 'ico',
    'css', 'js', 'map', 'woff', 'woff2', 'ttf', 'eot',
    'zip', 'gz', 'rar', '7z', 'mp3', 'mp4', 'avi', 'mov',
    'doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx',
}


def url_key(normalized: str) -> str:
    return hashlib.sha256(normalized.encode('utf-8')).hexdigest()


def resolve_url(href: str, base: str, drop_query) -> tuple[str | None, str]:
    raw = (href or '').strip()
    if not raw or raw.startswith('#'):
        return None, 'fragment'
    lowered = raw.lower()
    if lowered.startswith(('mailto:', 'javascript:', 'data:', 'tel:')):
        return None, 'scheme'
    try:
        absolute = urljoin(base or '', raw)
        parts = urlparse(absolute)
        port = parts.port
        host = (parts.hostname or '').lower()
    except ValueError:
        return None, 'invalid_url'
    if parts.scheme not in ('http', 'https') or not parts.hostname:
        return None, 'scheme'
    if parts.username or parts.password:
        return None, 'credentials'
    try:
        host = f'[{host}]' if ':' in host else host.encode('idna').decode('ascii')
    except UnicodeError:
        return None, 'invalid_url'
    if port and not (
        (parts.scheme == 'http' and port == 80)
        or (parts.scheme == 'https' and port == 443)
    ):
        netloc = f'{host}:{port}'
    else:
        netloc = host
    path = parts.path or '/'
    suffix = ''
    leaf = path.rsplit('/', 1)[-1]
    if '.' in leaf:
        suffix = leaf.rsplit('.', 1)[-1].lower()
    if suffix in _SKIP_SUFFIXES:
        return None, 'document'
    drop = set(drop_query or [])
    # Re-encoding query can change signed URLs, plus/space and repeated parameters.
    query = '&'.join(piece for piece in parts.query.split('&')
                     if unquote_plus(piece.split('=', 1)[0]) not in drop)
    return urlunparse((parts.scheme, netloc, path, parts.params, query, '')), ''


def host_allowed(url: str, allowed_hosts) -> bool:
    host = (urlparse(url).hostname or '').lower()
    allowed = [item.lower() for item in allowed_hosts or [] if item]
    if not allowed:
        return False
    return any(host == item or host.endswith(f'.{item}') for item in allowed)


def rules_match(url: str, rules) -> bool:
    if not rules:
        return True
    path = urlparse(url).path or '/'
    for rule in rules:
        kind = rule.get('kind')
        pattern = rule.get('pattern') or ''
        if kind == 'prefix' and path.startswith(pattern):
            return True
        if kind == 'contains' and pattern and pattern in url:
            return True
    return False
