"""План задания: проверка полей и хеш, от которого зависит подтверждение."""

import hashlib
import json
import re

from django.utils.translation import gettext as _

from .constants import DEFAULT_LIMITS
from .errors import ScoutError

_NAME = re.compile(r'^[a-z][a-z0-9_]{0,40}$')
_KINDS = {'css', 'xpath'}


def normalize_plan(seed_urls, fields, limits=None) -> dict:
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
    if len(urls) > max_pages:
        raise ScoutError('invalid', _('В первой версии не больше %(count)s адресов') % {'count': max_pages})

    clean_fields = []
    names = set()
    for item in fields or []:
        if not isinstance(item, dict):
            raise ScoutError('invalid', _('Поле задано неверно'))
        name = str(item.get('name') or '').strip()
        kind = str(item.get('kind') or '').strip()
        selector = str(item.get('selector') or '').strip()
        if not _NAME.match(name):
            raise ScoutError('invalid', _('Имя поля: латиница, цифры и подчёркивание'))
        if name in names:
            raise ScoutError('invalid', _('Имена полей не должны повторяться'))
        if kind not in _KINDS:
            raise ScoutError('invalid', _('Тип правила: css или xpath'))
        if not selector or len(selector) > 500:
            raise ScoutError('invalid', _('Селектор пустой или слишком длинный'))
        names.add(name)
        clean_fields.append({
            'name': name,
            'kind': kind,
            'selector': selector,
            'many': bool(item.get('many')),
        })
    if not clean_fields:
        raise ScoutError('invalid', _('Нужно хотя бы одно поле'))
    if len(clean_fields) > 30:
        raise ScoutError('invalid', _('Слишком много полей'))

    return {
        'seed_urls': urls,
        'fields': clean_fields,
        'limits': merged_limits,
    }


def plan_hash(plan: dict) -> str:
    payload = json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()
