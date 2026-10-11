"""Извлечение полей из сохранённого HTML. Загрузку и запись в базу не делает."""

import hashlib
import json
import re

from lxml import html as lxml_html
from lxml.etree import XPathError

from ..contracts.envelope import PROTOCOL_VERSION, build_extract_request
from .constants import EXTRACTOR_ID, EXTRACTOR_VERSION, FIELD_TEXT_LIMIT
from .errors import ScoutError

_SPACE = re.compile(r'\s+')
_SIMPLE = re.compile(r'^(?P<tag>[\w-]+|\*)?(?P<rest>(?:[.#][\w-]+)*)$')
_CLASS = re.compile(r'[.#][\w-]+')
_CHARSET = re.compile(br'charset\s*=\s*["\']?([A-Za-z0-9._-]+)', re.I)
_HEADER_CHARSET = re.compile(r'charset\s*=\s*["\']?([A-Za-z0-9._-]+)', re.I)


def extract_html(body: bytes, rules: list[dict], record=None, content_type: str = '', charset: str = '') -> list[dict]:
    request = build_extract_request(rules)
    if request['protocol_version'] != PROTOCOL_VERSION:
        raise ScoutError('bad_contract', 'protocol')
    root = _parse(body, content_type, charset)
    containers = _containers(root, record)
    if containers is None:
        page_rules = list(rules)
        record_rules = []
        targets = [None]
    elif not containers:
        return []
    else:
        page_rules = [rule for rule in rules if rule.get('scope') != 'record']
        record_rules = [rule for rule in rules if rule.get('scope') == 'record']
        targets = containers
    shared = [_cell(root, rule, relative=False) for rule in page_rules]
    rows = []
    for index, node in enumerate(targets):
        values = {}
        evidence = []
        for rule, cell in zip(page_rules, shared):
            _put(values, evidence, rule, cell, index)
        for rule in record_rules:
            _put(values, evidence, rule, _cell(node, rule, relative=True), index)
        identity = hashlib.sha256(
            json.dumps({'index': index, 'values': values}, ensure_ascii=False, sort_keys=True).encode('utf-8')
        ).hexdigest()[:64]
        rows.append({'identity_key': identity, 'values': values, 'evidence': evidence})
    return rows


def selector_match_count(body: bytes, rule: dict, content_type: str = '', charset: str = '') -> int:
    root = _parse(body, content_type, charset)
    return len(_texts(root, rule, relative=False))


def decode_html(body: bytes, content_type: str = '', charset: str = '') -> str:
    if body.startswith(b'\xef\xbb\xbf'):
        return body.decode('utf-8-sig', errors='replace')
    chosen = (charset or _header_charset(content_type) or _meta_charset(body)).strip()
    if chosen:
        try:
            return body.decode(chosen, errors='replace')
        except LookupError:
            pass
    try:
        return body.decode('utf-8')
    except UnicodeDecodeError:
        return body.decode('cp1251', errors='replace')


def detected_charset(body: bytes, content_type: str = '') -> str:
    if body.startswith(b'\xef\xbb\xbf'):
        return 'utf-8'
    declared = (_header_charset(content_type) or _meta_charset(body)).strip()
    if declared:
        return declared[:40]
    try:
        body.decode('utf-8')
    except UnicodeDecodeError:
        return 'cp1251'
    return 'utf-8'


def _parse(body: bytes, content_type: str, charset: str):
    try:
        return lxml_html.fromstring(decode_html(body, content_type, charset))
    except ScoutError:
        raise
    except Exception as exc:
        raise ScoutError('parse', 'HTML не разобран') from exc


def _containers(root, record):
    if not record or not record.get('selector'):
        return None
    nodes = _nodes(root, {'kind': record.get('kind') or 'css', 'selector': record['selector']}, relative=False)
    return [node for node in nodes if hasattr(node, 'xpath')]


def _cell(node, rule: dict, *, relative: bool) -> dict:
    texts = _texts(node, rule, relative=relative)
    if rule.get('many'):
        chosen = texts or ['']
    else:
        chosen = [texts[0]] if texts else ['']
    raw_parts = [item[:FIELD_TEXT_LIMIT] for item in chosen]
    raw = '\n'.join(raw_parts)
    normalized = ' | '.join(part for part in (_SPACE.sub(' ', item).strip() for item in raw_parts) if part)
    return {'raw': raw[:FIELD_TEXT_LIMIT], 'normalized': normalized[:FIELD_TEXT_LIMIT], 'parts': raw_parts}


def _put(values, evidence, rule, cell, index: int) -> None:
    values[rule['name']] = {'raw': cell['raw'], 'normalized': cell['normalized']}
    for part_index, part in enumerate(cell['parts']):
        normalized = _SPACE.sub(' ', part).strip()
        evidence.append({
            'field_path': rule['name'],
            'locator': f"{rule['kind']}:{rule['selector']}#{index}.{part_index}",
            'raw_value': part,
            'normalized_value': normalized,
            'extractor_id': EXTRACTOR_ID,
            'extractor_version': EXTRACTOR_VERSION,
        })


def _texts(root, rule: dict, *, relative: bool) -> list[str]:
    return [_node_text(node) for node in _nodes(root, rule, relative=relative)]


def _nodes(root, rule: dict, *, relative: bool):
    kind = rule['kind']
    selector = rule['selector']
    try:
        if kind == 'css':
            found = root.xpath(_css_to_xpath(selector, relative=relative))
        else:
            found = root.xpath(_bind_xpath(selector, relative))
    except (XPathError, ScoutError, ValueError, TypeError) as exc:
        if isinstance(exc, ScoutError):
            raise
        raise ScoutError('selector', 'Селектор не применился') from exc
    return _coerce(found)


def _coerce(found) -> list:
    if isinstance(found, str):
        return [found]
    if isinstance(found, bool):
        return ['true' if found else 'false']
    if isinstance(found, float):
        if found.is_integer():
            return [str(int(found))]
        return [str(found)]
    if isinstance(found, int):
        return [str(found)]
    return list(found)


def _node_text(node) -> str:
    if isinstance(node, str):
        return node
    if isinstance(node, (int, float, bool)):
        return _coerce(node)[0]
    return node.text_content() or ''


def _bind_xpath(selector: str, relative: bool) -> str:
    text = (selector or '').strip()
    if not relative:
        return text
    if text.startswith('./'):
        return text
    if text.startswith('//'):
        return f'.{text}'
    if text.startswith('/'):
        raise ScoutError('selector', 'Абсолютный XPath выходит за карточку')
    return f'.//{text}'


def _header_charset(content_type: str) -> str:
    match = _HEADER_CHARSET.search(content_type or '')
    return match.group(1) if match else ''


def _meta_charset(body: bytes) -> str:
    match = _CHARSET.search(body[:4096])
    if match is None:
        return ''
    return match.group(1).decode('ascii', errors='ignore')


def _css_to_xpath(selector: str, *, relative: bool = False) -> str:
    """CSS без отдельного пакета: тег, класс, id, потомок и прямой ребёнок."""
    text = re.sub(r'\s*>\s*', '>', (selector or '').strip())
    text = re.sub(r'\s+', ' ', text).strip()
    if not text or any(char in text for char in '[]:+~(),'):
        raise ScoutError('selector', 'Этот CSS-селектор пока не поддерживается')
    pieces = []
    buffer = []
    for char in text:
        if char == '>':
            piece = ''.join(buffer).strip()
            if piece:
                pieces.append(piece)
            pieces.append('>')
            buffer = []
            continue
        if char.isspace():
            piece = ''.join(buffer).strip()
            if piece:
                pieces.append(piece)
                pieces.append(' ')
            buffer = []
            continue
        buffer.append(char)
    tail = ''.join(buffer).strip()
    if tail:
        pieces.append(tail)
    if not pieces or pieces[0] in {'>', ' '}:
        raise ScoutError('selector', 'Селектор не применился')
    xpath = ''
    expect_selector = True
    for piece in pieces:
        if piece in {'>', ' '}:
            if expect_selector:
                raise ScoutError('selector', 'Селектор не применился')
            expect_selector = True
            xpath += '/' if piece == '>' else '//'
            continue
        if not expect_selector:
            xpath += '//'
        else:
            expect_selector = False
            if not xpath:
                xpath = './/' if relative else '//'
        xpath += _simple_xpath(piece)
    if expect_selector:
        raise ScoutError('selector', 'Селектор не применился')
    return xpath


def _simple_xpath(piece: str) -> str:
    match = _SIMPLE.match(piece)
    if match is None or not (match.group('tag') or match.group('rest')):
        raise ScoutError('selector', 'Селектор не применился')
    tag = match.group('tag') or '*'
    xpath = tag
    for token in _CLASS.findall(match.group('rest') or ''):
        if token.startswith('#'):
            xpath += f"[@id='{_xml_literal(token[1:])}']"
        else:
            name = _xml_literal(token[1:])
            xpath += f"[contains(concat(' ', normalize-space(@class), ' '), ' {name} ')]"
    return xpath


def _xml_literal(value: str) -> str:
    return value.replace("'", '')
