"""План задания: проверка полей и хеш, от которого зависит подтверждение."""

import hashlib
import json
import re

from urllib.parse import urldefrag, urlparse

from django.utils.translation import gettext as _

from .constants import CRAWL_LIMITS, DEFAULT_LIMITS, DROP_QUERY
from .errors import ScoutError
from .public_http import public_url_allowed
from .url_norm import canonical_host, resolve_url, host_allowed
from .value_types import clean_conversion, typed_csv_columns

_NAME = re.compile(r'^[a-z][a-z0-9_]{0,40}$')
_KINDS = {'css', 'xpath'}
_IDENTITY_NAMESPACE = re.compile(r'[a-z][a-z0-9_-]{0,63}')
IDENTITY_UNSET = object()


def normalize_plan(seed_urls, fields, limits=None, record=None, crawl=None, *, identity=IDENTITY_UNSET) -> dict:
    urls = []
    seen = set()
    for raw in _plan_list(seed_urls, '/seed_urls'):
        if not isinstance(raw, str):
            raise ScoutError('invalid', _('Адрес должен быть строкой'), path='/seed_urls')
        url = raw.strip()
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
    for item in _plan_list(fields, '/fields'):
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
        field = {
            'name': name,
            'kind': kind,
            'selector': selector,
            'scope': scope,
            'many': bool(item.get('many')),
        }
        if 'conversion' in item:
            field['conversion'] = clean_conversion(item['conversion'], many=field['many'])
        clean_fields.append(field)
    if not clean_fields:
        raise ScoutError('invalid', _('Нужно хотя бы одно поле'))
    if len(clean_fields) > 30:
        raise ScoutError('invalid', _('Слишком много полей'))
    if any('conversion' in field for field in clean_fields):
        columns = typed_csv_columns(clean_fields)
        if len(columns) != len(set(columns)):
            raise ScoutError('invalid', _('Имена колонок типизированного CSV пересекаются'))

    if isinstance(crawl, dict) and 'page_types' in crawl:
        clean_crawl['page_types'] = _clean_page_types(crawl['page_types'], names)

    return {
        'seed_urls': urls,
        'record': clean_record,
        'fields': clean_fields,
        'limits': merged_limits,
        'crawl': clean_crawl,
        **({'identity': _clean_identity(identity, clean_fields, clean_record)} if identity is not IDENTITY_UNSET else {}),
    }


def _clean_identity(raw, fields: list[dict], record=None) -> dict:
    if not isinstance(raw, dict):
        raise ScoutError('invalid_identity', _('Ожидается объект настройки SKU'), path='/identity')
    kind = raw.get('kind')
    if kind not in ('sku', 'field', 'url'):
        raise ScoutError('invalid_identity', _('Ключ записи: sku, field или url'), path='/identity/kind')
    namespace = raw.get('namespace')
    if not isinstance(namespace, str) or not _IDENTITY_NAMESPACE.fullmatch(namespace):
        raise ScoutError('invalid_identity', _('Укажите допустимое имя каталога'), path='/identity/namespace')
    if kind == 'url':
        if record is not None or set(raw) != {'kind','namespace'}:
            raise ScoutError('invalid_identity', _('Ключ URL требует одну запись на страницу и не принимает field'),
                             path='/identity')
        return {'kind':'url','namespace':namespace}
    name = raw.get('field')
    field = next((field for field in fields if field['name'] == name), None)
    if not field or field['many'] or 'conversion' in field:
        raise ScoutError('invalid_identity', _('Укажите одиночное текстовое поле SKU'), path='/identity/field')
    return {'kind': kind, 'namespace': namespace, 'field': name}


def _clean_crawl(crawl, seed_urls) -> dict:
    if crawl is not None and not isinstance(crawl, dict):
        raise ScoutError('invalid', _('Настройки обхода должны быть объектом'), path='/crawl')
    raw = crawl or {}
    drop_query = raw.get('drop_query', DROP_QUERY)
    if not isinstance(drop_query, (list, tuple)) or any(not isinstance(key, str) for key in drop_query):
        raise ScoutError('invalid', _('Правило адреса задано неверно'))
    mode = str(raw.get('mode') or 'pages')
    if mode not in ('pages', 'site'):
        raise ScoutError('invalid', _('Режим обхода: pages или site'))
    if 'page_types' in raw:
        if mode != 'site':
            raise ScoutError('invalid_page_types', _('Типы страниц требуют режима обхода сайта'), path='/crawl/page_types')
        if raw.get('extract_on'):
            raise ScoutError('invalid_page_types', _('Используйте только типы страниц или фильтр извлечения'),
                             path='/crawl/extract_on')
    hosts = []
    for item in _plan_list(raw.get('allowed_hosts'), '/crawl/allowed_hosts'):
        text = str(item or '').strip()
        if not text:
            continue
        host = canonical_host(text)
        if host is None:
            raise ScoutError('invalid', _('Имя сайта задано неверно'))
        if host not in hosts:
            hosts.append(host)
    if mode == 'site' and not hosts:
        found = []
        for url in seed_urls:
            try:
                host = canonical_host(urlparse(url).hostname or '')
            except ValueError as exc:
                raise ScoutError('invalid', _('Начальный адрес задан неверно'), path='/seed_urls') from exc
            if host and host not in found:
                found.append(host)
        hosts = sorted(found)
    result = {
        'mode': mode,
        'allowed_hosts': hosts,
        'visit': _clean_rules(raw.get('visit'), '/crawl/visit'),
        'extract_on': _clean_rules(raw.get('extract_on'), '/crawl/extract_on'),
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
    for url in _plan_list(raw.get('preview_urls'), '/crawl/preview_urls') if mode == 'site' else []:
        normalized, _reason = resolve_url(str(url), str(url), result['drop_query'])
        if not normalized or not host_allowed(normalized, hosts):
            raise ScoutError('invalid', _('Адрес предпросмотра выходит за пределы обхода'))
        if normalized not in result['preview_urls']:
            result['preview_urls'].append(normalized)
    if len(result['preview_urls']) > result['preview_pages']:
        raise ScoutError('invalid', _('Превышен лимит страниц предпросмотра'))
    sitemap = raw.get('sitemap_url')
    if sitemap is not None:
        if not isinstance(sitemap, str):
            raise ScoutError('invalid', _('Адрес карты сайта должен быть строкой'))
        sitemap = sitemap.strip()
        if sitemap:
            if mode != 'site' or len(sitemap) > 2000:
                raise ScoutError('invalid', _('Для карты сайта нужен режим обхода и адрес до 2000 символов'))
            public_url_allowed(sitemap)
            if not host_allowed(sitemap, hosts):
                raise ScoutError('invalid', _('Адрес карты сайта выходит за пределы обхода'))
            result['sitemap_url'] = urldefrag(sitemap).url
    robots = raw.get('sitemap_from_robots', False)
    if not isinstance(robots, bool):
        raise ScoutError('invalid', _('sitemap_from_robots должен быть логическим значением'))
    if robots:
        if mode != 'site' or result.get('sitemap_url'):
            raise ScoutError('invalid', _('Поиск через robots.txt требует режима site без явного sitemap_url'))
        result['sitemap_from_robots'] = True
    return result


def _clean_page_types(raw, field_names: set[str]) -> list[dict]:
    path = '/crawl/page_types'
    if not isinstance(raw, list) or not 1 <= len(raw) <= 5:
        raise ScoutError('invalid_page_types', _('Нужен список из одного–пяти типов страниц'), path=path)
    result = []
    ids = set()
    for index, item in enumerate(raw):
        entry = f'{path}/{index}'
        if not isinstance(item, dict):
            raise ScoutError('invalid_page_types', _('Тип страницы должен быть объектом'), path=entry)
        type_id = item.get('id')
        if not isinstance(type_id, str) or not _NAME.fullmatch(type_id) or type_id in ids:
            raise ScoutError('invalid_page_types', _('Укажите уникальное имя типа страницы'), path=f'{entry}/id')
        ids.add(type_id)
        action = item.get('action')
        if action not in ('discover', 'extract'):
            raise ScoutError('invalid_page_types', _('Укажите обход ссылок или извлечение'), path=f'{entry}/action')
        match = item.get('match')
        if not isinstance(match, list) or not match:
            raise ScoutError('invalid_page_types', _('Укажите хотя бы одно правило адреса'), path=f'{entry}/match')
        rules = []
        for rule_index, rule in enumerate(match):
            kind = rule.get('kind') if isinstance(rule, dict) else None
            pattern = rule.get('pattern') if isinstance(rule, dict) else None
            if kind not in ('prefix', 'contains') or not isinstance(pattern, str) or not 1 <= len(pattern.strip()) <= 300:
                raise ScoutError('invalid_page_types', _('Правило адреса задано неверно'), path=f'{entry}/match/{rule_index}')
            rules.append({'kind': kind, 'pattern': pattern.strip()})
        cleaned = {'id': type_id, 'action': action, 'match': rules}
        if 'selectors' in item:
            cleaned['selectors'] = _clean_type_selectors(item['selectors'], field_names, action, f'{entry}/selectors')
        result.append(cleaned)
    if not any(item['action'] == 'extract' for item in result):
        raise ScoutError('invalid_page_types', _('Нужен хотя бы один тип для извлечения'), path=path)
    return result


def _clean_type_selectors(raw, field_names, action, path) -> dict:
    if action != 'extract' or not isinstance(raw, dict):
        raise ScoutError('invalid_page_types', _('Селекторы можно задать только для извлекающего типа'), path=path)
    selectors = {}
    for name, rule in raw.items():
        pointer = f"{path}/{name.replace('~', '~0').replace('/', '~1')}"
        if name not in field_names:
            raise ScoutError('invalid_page_types', _('Поля нет в общей схеме'), path=pointer)
        kind = rule.get('kind') if isinstance(rule, dict) else None
        selector = rule.get('selector') if isinstance(rule, dict) else None
        if kind not in ('css', 'xpath') or not isinstance(selector, str) or not 1 <= len(selector.strip()) <= 500:
            raise ScoutError('invalid_page_types', _('Укажите вид и текст селектора'), path=pointer)
        selectors[name] = {'kind': kind, 'selector': selector.strip()}
    return selectors


def _plan_list(value, path):
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ScoutError('invalid', _('Ожидается список'), path=path)
    return value


def _clean_rules(rules, path) -> list:
    cleaned = []
    for item in _plan_list(rules, path):
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
    except (TypeError, ValueError, OverflowError) as exc:
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
