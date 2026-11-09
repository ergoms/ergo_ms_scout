"""Распознавание страниц проверки без исполнения чужого JavaScript."""

from urllib.parse import urlsplit

from lxml import etree, html

from .errors import FetchError
from .html_extract import decode_html

CHALLENGE_FAILURES = frozenset({'cloudflare_challenge', 'yandex_captcha', 'captcha_required'})
_SAMPLE_BYTES = 64 * 1024
_GATE_TITLES = (
    'just a moment', 'attention required', 'checking your browser', 'security verification',
    'verify you are human', 'are you a robot', 'проверка браузера',
    'подтвердите, что', 'подтвердите что', 'вы не робот', 'доступ ограничен',
)
_YANDEX_HOSTS = frozenset({'yandex.ru', 'yandex.com', 'yandex.by', 'yandex.kz', 'yandex.uz', 'yandex.com.tr'})


def raise_for_challenge(*, headers, url: str, body: bytes = b'', content_type: str = '') -> None:
    # Документированный признак надёжнее HTML и действует также при HTTP 200/403/503.
    if str(headers.get('cf-mitigated', '')).strip().lower() == 'challenge':
        _raise('cloudflare_challenge')
    media = content_type.split(';', 1)[0].strip().lower()
    if not body or media not in ('', 'text/html', 'application/xhtml+xml'):
        return
    try:
        root = html.fromstring(decode_html(body[:_SAMPLE_BYTES], content_type))
    except (etree.ParserError, ValueError):
        return
    title = ' '.join(root.xpath('//title/text()')).strip().lower()
    gate = title in {'captcha', 'капча', 'smartcaptcha'} or any(marker in title for marker in _GATE_TITLES)
    scripts = root.xpath('//script')
    if gate and any(
        '/cdn-cgi/challenge-platform/' in node.get('src', '')
        or '_cf_chl_opt' in (node.text or '')
        for node in scripts
    ):
        _raise('cloudflare_challenge')

    # Обычный виджет на странице входа/каталога сам по себе не означает заглушку.
    if gate and any(_host(node.get('src', '')) == 'smartcaptcha.cloud.yandex.ru' for node in scripts):
        _raise('yandex_captcha')
    source_host = _host(url)
    for form in root.xpath('//form'):
        action = form.get('action', '')
        action_host = _host(action)
        try:
            path = urlsplit(action).path.rstrip('/')
            source_path = urlsplit(url).path.rstrip('/')
        except ValueError:
            continue
        if (
            source_host in _YANDEX_HOSTS and action_host in ('', source_host)
            and path == '/checkcaptcha'
            and form.xpath('.//input[@name="rep" or @name="answer"]')
            and (gate or source_path == '/showcaptcha')
        ):
            _raise('yandex_captcha')
        if gate and form.xpath('.//input[@name="captcha" or @name="captcha_code"]') and form.xpath('.//img'):
            _raise('captcha_required')


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or '').lower().rstrip('.')
    except ValueError:
        return ''


def _raise(code: str) -> None:
    messages = {
        'cloudflare_challenge': 'Источник требует проверку Cloudflare. Автоматическое прохождение пока не поддержано.',
        'yandex_captcha': 'Источник требует CAPTCHA Яндекса. Автоматическое прохождение пока не поддержано.',
        'captcha_required': 'Источник требует CAPTCHA. Для этой формы автоматическое прохождение пока не поддержано.',
    }
    raise FetchError(code, messages[code], retryable=False)
