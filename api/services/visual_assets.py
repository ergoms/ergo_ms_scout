"""Ограниченная загрузка оформления для изолированного снимка редактора."""

import ipaddress
from base64 import b64encode
from email.message import Message
from io import BytesIO
from time import monotonic
from urllib.parse import urldefrag, urljoin, urlsplit

from PIL import Image
from tinycss2 import parse_blocks_contents, parse_stylesheet, serialize
from tinycss2.ast import IdentToken, URLToken
from tinycss2.bytes import decode_stylesheet_bytes

from .errors import ScoutError
from .proxy_http import fetch_proxy_css, fetch_proxy_image
from .public_http import _public_address, fetch_public_css, fetch_public_image

_MAX_REQUESTS = 36
_MAX_BYTES = 2 * 1024 * 1024
_MAX_SECONDS = 12
_REQUEST_SECONDS = 2
_MAX_CSS_BYTES = 768 * 1024
_MAX_IMAGE_BYTES = 384 * 1024
_MAX_IMAGE_PIXELS = 4_000_000
_MAX_ANIMATION_PIXELS = 16_000_000
_MAX_CSS_CHARS = 1024 * 1024
_MAX_CSS_IMAGES_CHARS = 512 * 1024
_MAX_IMPORTS = 8
_MAX_TOKEN_DEPTH = 32
_NON_RASTER_SUFFIXES = ('.svg', '.css', '.js', '.woff', '.woff2', '.ttf', '.otf', '.eot')
_IMAGE_FORMATS = {'image/jpeg': 'JPEG', 'image/png': 'PNG', 'image/gif': 'GIF', 'image/webp': 'WEBP'}


def _asset_url(reference, base):
    reference = (reference or '').strip()
    if not reference or reference.startswith('#') or len(reference) > 4000:
        return None
    try:
        url = urldefrag(urljoin(base, reference)).url
        parts = urlsplit(url)
        if (
            parts.scheme in ('http', 'https') and parts.hostname and parts.username is None
            and len(url) <= 4000 and not any(char.isspace() or ord(char) < 32 or char == '\\' for char in url)
        ):
            return url
    except ValueError:
        return None
    return None


def _origin(parts):
    return parts.scheme, parts.hostname, parts.port or (443 if parts.scheme == 'https' else 80)


def _css_text(file):
    headers = Message()
    headers['Content-Type'] = file['content_type']
    charset = headers.get_content_charset()
    text, _encoding = decode_stylesheet_bytes(file['body'], protocol_encoding=charset)
    return text


def _image_data(file):
    media = file['content_type']
    image_format = _IMAGE_FORMATS.get(media)
    if image_format is None:
        return None
    body = file['body']
    try:
        with Image.open(BytesIO(body), formats=(image_format,)) as image:
            pixels = image.width * image.height
            if pixels > _MAX_IMAGE_PIXELS or pixels * getattr(image, 'n_frames', 1) > _MAX_ANIMATION_PIXELS:
                return None
            image.load()
    except (Image.DecompressionBombError, OSError, SyntaxError, ValueError):
        return None
    return f'data:{media};base64,{b64encode(body).decode("ascii")}'


def _css_url(token, *, allow_string=False):
    if token.type == 'url' or (allow_string and token.type == 'string'):
        return token.value
    if token.type == 'function' and token.lower_name == 'url':
        args = [value for value in token.arguments if value.type != 'whitespace']
        if len(args) == 1 and args[0].type == 'string':
            return args[0].value
    return None


class VisualAssets:
    def __init__(self, snapshot, base_url):
        self._base = base_url
        self._origin = _origin(urlsplit(snapshot.source_url))
        self._proxy = snapshot.proxy_profile_revision_public_id
        self._owner = snapshot.user_public_id
        self._cache = {}
        self._requests = _MAX_REQUESTS
        self._bytes = _MAX_BYTES
        self._deadline = monotonic() + _MAX_SECONDS

    def stylesheet(self, href, base_url=None):
        url = _asset_url(href, base_url or self._base)
        if url is None:
            return None
        key = ('css', url)
        if key not in self._cache:
            file = self._fetch(url, _MAX_CSS_BYTES, fetch_public_css, fetch_proxy_css)
            self._cache[key] = (_css_text(file), file['final_url']) if file is not None else None
        return self._cache[key]

    def image(self, href, base_url=None):
        url = _asset_url(href, base_url or self._base)
        if url is None or urlsplit(url).path.lower().endswith(_NON_RASTER_SUFFIXES):
            return None
        key = ('image', url)
        if key not in self._cache:
            file = self._fetch(url, _MAX_IMAGE_BYTES, fetch_public_image, fetch_proxy_image)
            self._cache[key] = _image_data(file) if file is not None else None
        return self._cache[key]

    def _fetch(self, url, limit, public_fetch, proxy_fetch):
        timeout = min(self._deadline - monotonic(), _REQUEST_SECONDS)
        if self._requests <= 0 or self._bytes <= 0 or timeout <= 0 or not self._target_allowed(url):
            return None
        max_bytes = min(self._bytes, limit)
        self._requests -= 1
        options = {'timeout_seconds': timeout, 'max_bytes': max_bytes, 'url_allowed': self._target_allowed}
        try:
            if self._proxy:
                file = proxy_fetch(url, revision_id=self._proxy, owner=self._owner, **options)
            else:
                file = public_fetch(url, **options)
        except ScoutError:
            # Загрузчик не возвращает число прочитанных байтов при частичном ответе.
            self._bytes -= max_bytes
            return None
        self._bytes -= len(file['body'])
        return file

    def _target_allowed(self, url):
        try:
            parts = urlsplit(url)
            requested = _origin(parts)
            host = (parts.hostname or '').rstrip('.')
            if not host:
                return False
            try:
                ipaddress.ip_address(host)
            except ValueError:
                return requested == self._origin or (host != 'localhost' and not host.endswith('.localhost'))
            # Явно настроенный закрытый адрес доступен для ресурса только на исходном узле.
            return requested == self._origin or _public_address(host)
        except ValueError:
            return False

    def style(self, text, base_url=None, *, declarations=False):
        if len(text) > _MAX_CSS_BYTES:
            return ''
        base = base_url or self._base
        image_chars = _MAX_CSS_IMAGES_CHARS

        def embed(href, source):
            nonlocal image_chars
            if image_chars <= 0:
                return None
            uri = self.image(href, source)
            if uri is None or len(uri) > image_chars:
                return None
            image_chars -= len(uri)
            return uri

        if declarations:
            rules = [
                rule for rule in parse_blocks_contents(text, skip_comments=True) if rule.type == 'declaration'
            ]
            for rule in rules:
                rule.value = _rewrite_tokens(rule.value, base, embed)
            return serialize(rules)
        return self._style_rules(text, base, embed, {base})

    def _style_rules(self, text, base, embed, visited):
        chunks = []
        chars = 0
        for rule in parse_stylesheet(text, skip_comments=True):
            if rule.type == 'error':
                continue
            if rule.type == 'at-rule' and rule.lower_at_keyword in ('charset', 'font-face'):
                continue
            if rule.type == 'at-rule' and rule.lower_at_keyword == 'import':
                chunk = self._import_style(rule, base, embed, visited)
            else:
                if rule.type in ('at-rule', 'qualified-rule') and rule.content is not None:
                    rule.content = _rewrite_tokens(rule.content, base, embed)
                chunk = rule.serialize()
            chars += len(chunk)
            if chars > _MAX_CSS_CHARS:
                break
            chunks.append(chunk)
        return ''.join(chunks)

    def _import_style(self, rule, base, embed, visited):
        if len(visited) > _MAX_IMPORTS:
            return ''
        prelude = iter(rule.prelude)
        first = next((token for token in prelude if token.type != 'whitespace'), None)
        href = _css_url(first, allow_string=True) if first is not None else None
        if href is None:
            return ''
        condition = serialize(prelude).strip()
        if condition.lower().startswith(('layer', 'supports(')):
            return ''
        imported = self.stylesheet(href, base)
        if imported is None:
            return ''
        text, url = imported
        if url in visited:
            return ''
        visited.add(url)
        css = self._style_rules(text, url, embed, visited)
        return f'@media {condition} {{{css}}}' if condition else css


def _rewrite_tokens(tokens, base, embed, depth=0):
    if depth >= _MAX_TOKEN_DEPTH:
        return []
    rewritten = []
    for token in tokens:
        token_type = token.type
        if token_type == 'error':
            continue
        if token_type == 'url' or (token_type == 'function' and token.lower_name == 'url'):
            href = _css_url(token)
            uri = embed(href, base) if href is not None else None
            line, column = token.source_line, token.source_column
            token = URLToken(line, column, uri, f'url("{uri}")') if uri else IdentToken(line, column, 'none')
        elif token_type == 'function':
            token.arguments = _rewrite_tokens(token.arguments, base, embed, depth + 1)
        elif token_type in ('() block', '[] block', '{} block'):
            token.content = _rewrite_tokens(token.content, base, embed, depth + 1)
        rewritten.append(token)
    return rewritten
