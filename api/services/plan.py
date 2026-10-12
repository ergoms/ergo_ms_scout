"""План задания: проверка полей и хеш, от которого зависит подтверждение."""

import hashlib
import json
import re

from urllib.parse import urlparse

from django.utils.translation import gettext as _

from .constants import CRAWL_LIMITS, DEFAULT_LIMITS, DROP_QUERY
from .errors import ScoutError
from .url_norm import resolve_url, host_allowed

_NAME = re.compile(r'^[a-z][a-z0-9_]{0,40}$')
_KINDS = {'css', 'xpath'}


def normalize_plan(seed_urls, fields, limits=None, record=None, crawl=None) -> dict:
    urls = []
    seen = set()
    for raw in seed_urls or []:
        url = str(raw or '').strip()
        if not url or url in seen:
            continue
        if not url.startswith(('http://', 'https://')):
            raise ScoutError('invalid', _('Адрес должен начинаться с http:// или https://'))
        if len(url) > 2000:
            raise ScoutError('invalid', _('Адрес слишком длинный'))
        seen.add(url)
        urls.append(url)
    if not urls:
        raise ScoutError('invalid', _('Нужен хотя бы один адрес страницы'))

    merged_limits = dict(DEFAULT_LIMITS)
    if isinstance(limits, dict):
        for key in DEFAULT_LIMITS:
            if key in limits:
                merged_limits[key] = int(limits[key])
    max_pages = max(1, min(int(merged_limits['max_pages']), DEFAULT_LIMITS['max_pages']))
    merged_limits['max_pages'] = max_pages
    merged_limits['max_bytes'] = max(1, min(int(merged_limits['max_bytes']), DEFAULT_LIMITS['max_bytes']))
    merged_limits['timeout_seconds'] = max(1, min(int(merged_limits['timeout_seconds']), 60))
    clean_crawl = _clean_crawl(crawl, urls)
    if clean_crawl['mode'] == 'site':
        normalized_urls = []
        for url in urls:
            normalized, _reason = resolve_url(url, url, clean_crawl['drop_query'])
            if not normalized or not host_allowed(normalized, clean_crawl['allowed_hosts']):
                raise ScoutError('invalid', _('Начальный адрес выходит за пределы обхода'))
            if normalized not in normalized_urls:
                normalized_urls.append(normalized)
        urls = normalized_urls
        max_pages = min(clean_crawl['max_pages'], clean_crawl['frontier_cap'])
    if len(urls) > max_pages:
        raise ScoutError('invalid', _('В первой версии не больше %(count)s адресов') % {'count': max_pages})

    clean_record = _clean_record(record)
    clean_fields = []
    names = set()
    for item in fields or []:
        if not isinstance(item, dict):
            raise ScoutError('invalid', _('Поле задано неверно'))
        name = str(item.get('name') or '').strip()
        kind = str(item.get('kind') or '').strip()
        selector = str(item.get('selector') or '').strip()
        scope = str(item.get('scope') or '').strip()
        if scope not in ('page', 'record'):
            scope = 'record' if clean_record else 'page'
        if not _NAME.match(name):
            raise ScoutError('invalid', _('Имя поля: латиница, цифры и подчёркивание'))
        if name in names:
            raise ScoutError('invalid', _('Имена полей не должны повторяться'))
        if kind not in _KINDS:
            raise ScoutError('invalid', _('Тип правила: css или xpath'))
        if not selector or len(selector) > 500:
            raise ScoutError('invalid', _('Селектор пустой или слишком длинный'))
        if scope == 'record' and clean_record is None:
            raise ScoutError('invalid', _('Для поля карточки нужен селектор карточки'))
        names.add(name)
        clean_fields.append({
            'name': name,
            'kind': kind,
            'selector': selector,
            'scope': scope,
            'many': bool(item.get('many')),
        })
    if not clean_fields:
        raise ScoutError('invalid', _('Нужно хотя бы одно поле'))
    if len(clean_fields) > 30:
        raise ScoutError('invalid', _('Слишком много полей'))

    return {
        'seed_urls': urls,
        'record': clean_record,
        'fields': clean_fields,
        'limits': merged_limits,
        'crawl': clean_crawl,
    }


def _clean_crawl(crawl, seed_urls) -> dict:
    raw = crawl if isinstance(crawl, dict) else {}
    drop_query = raw.get('drop_query', DROP_QUERY)
    if not isinstance(drop_query, (list, tuple)) or any(not isinstance(key, str) for key in drop_query):
        raise ScoutError('invalid', _('Правило адреса задано неверно'))
    mode = str(raw.get('mode') or 'pages')
    if mode not in ('pages', 'site'):
        raise ScoutError('invalid', _('Режим обхода: pages или site'))
    hosts = []
    for item in raw.get('allowed_hosts') or []:
        host = str(item or '').strip().lower()
        if host and host not in hosts:
            hosts.append(host)
    if mode == 'site' and not hosts:
        hosts = sorted({
            (urlparse(url).hostname or '').lower()
            for url in seed_urls
            if urlparse(url).hostname
        })
    result = {
        'mode': mode,
        'allowed_hosts': hosts,
        'visit': _clean_rules(raw.get('visit')),
        'extract_on': _clean_rules(raw.get('extract_on')),
        'max_depth': _clamp(raw.get('max_depth'), 0, 5, CRAWL_LIMITS['max_depth']),
        'max_pages': _clamp(raw.get('max_pages'), 1, 200, CRAWL_LIMITS['max_pages']),
        'preview_pages': _clamp(raw.get('preview_pages'), 1, 5, CRAWL_LIMITS['preview_pages']),
        'max_seconds': _clamp(raw.get('max_seconds'), 1, 3600, CRAWL_LIMITS['max_seconds']),
        'frontier_cap': _clamp(raw.get('frontier_cap'), 1, 500, CRAWL_LIMITS['frontier_cap']),
        'fetch_delay_ms': _clamp(raw.get('fetch_delay_ms'), 0, 5000, CRAWL_LIMITS['fetch_delay_ms']),
        'parallel_fetch': _clamp(raw.get('parallel_fetch'), 1, 4, CRAWL_LIMITS['parallel_fetch']),
        'max_total_bytes': _clamp(
            raw.get('max_total_bytes'), 1, 50_000_000, CRAWL_LIMITS['max_total_bytes'],
        ),
        'drop_query': list(drop_query),
    }
    result['preview_urls'] = []
    for url in (raw.get('preview_urls') or []) if mode == 'site' else []:
        normalized, _reason = resolve_url(str(url), str(url), result['drop_query'])
        if not normalized or not host_allowed(normalized, hosts):
            raise ScoutError('invalid', _('Адрес предпросмотра выходит за пределы обхода'))
        if normalized not in result['preview_urls']:
            result['preview_urls'].append(normalized)
    if len(result['preview_urls']) > result['preview_pages']:
        raise ScoutError('invalid', _('Превышен лимит страниц предпросмотра'))
    return result


def _clean_rules(rules) -> list:
    cleaned = []
    for item in rules or []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get('kind') or '')
        pattern = str(item.get('pattern') or '').strip()
        if kind not in ('prefix', 'contains') or not pattern or len(pattern) > 300:
            raise ScoutError('invalid', _('Правило адреса задано неверно'))
        cleaned.append({'kind': kind, 'pattern': pattern})
    return cleaned


def _clamp(value, low, high, fallback) -> int:
    if value is None or value == '':
        return fallback
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ScoutError('invalid', _('Лимит обхода задан неверно')) from exc
    return max(low, min(number, high))


def _clean_record(record) -> dict | None:
    if not record:
        return None
    if not isinstance(record, dict):
        raise ScoutError('invalid', _('Карточка задана неверно'))
    selector = str(record.get('selector') or '').strip()
    if not selector:
        return None
    kind = str(record.get('kind') or 'css').strip()
    if kind not in _KINDS:
        raise ScoutError('invalid', _('Тип правила: css или xpath'))
    if len(selector) > 500:
        raise ScoutError('invalid', _('Селектор пустой или слишком длинный'))
    return {'kind': kind, 'selector': selector}


def plan_hash(plan: dict) -> str:
    payload = json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()
