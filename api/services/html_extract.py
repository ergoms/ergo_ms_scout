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


def extract_html(body: bytes, rules: list[dict]) -> list[dict]:
    request = build_extract_request(rules)
    if request['protocol_version'] != PROTOCOL_VERSION:
        raise ScoutError('bad_contract', 'protocol')
    try:
        root = lxml_html.fromstring(_decode_html(body))
    except ScoutError:
        raise
    except Exception as exc:
        raise ScoutError('parse', 'HTML не разобран') from exc

    columns = []
    for rule in rules:
        columns.append(_texts(root, rule))

    many = any(rule.get('many') for rule in rules)
    width = 1
    if many:
        width = max((len(col) for rule, col in zip(rules, columns) if rule.get('many')), default=0)
    rows = []
    for index in range(width):
        values = {}
        evidence = []
        for rule, column in zip(rules, columns):
            if rule.get('many'):
                raw = column[index] if index < len(column) else ''
            else:
                raw = column[0] if column else ''
            raw = raw[:FIELD_TEXT_LIMIT]
            normalized = _SPACE.sub(' ', raw).strip()
            values[rule['name']] = {'raw': raw, 'normalized': normalized}
            evidence.append({
                'field_path': rule['name'],
                'locator': f"{rule['kind']}:{rule['selector']}#{index}",
                'raw_value': raw,
                'normalized_value': normalized,
                'extractor_id': EXTRACTOR_ID,
                'extractor_version': EXTRACTOR_VERSION,
            })
        identity = hashlib.sha256(
            json.dumps({'index': index, 'values': values}, ensure_ascii=False, sort_keys=True).encode('utf-8')
        ).hexdigest()[:64]
        rows.append({'identity_key': identity, 'values': values, 'evidence': evidence})
    return rows


def selector_match_count(body: bytes, rule: dict) -> int:
    root = lxml_html.fromstring(_decode_html(body))
    return len(_texts(root, rule))


def _decode_html(body: bytes) -> str:
    payload = body[3:] if body.startswith(b'\xef\xbb\xbf') else body
    try:
        return payload.decode('utf-8')
    except UnicodeDecodeError:
        return payload.decode('cp1251', errors='replace')


def _texts(root, rule: dict) -> list[str]:
    kind = rule['kind']
    selector = rule['selector']
    try:
        if kind == 'css':
            nodes = root.xpath(_css_to_xpath(selector))
        else:
            nodes = root.xpath(selector)
    except (XPathError, ScoutError, ValueError, TypeError) as exc:
        if isinstance(exc, ScoutError):
            raise
        raise ScoutError('selector', 'Селектор не применился') from exc
    texts = []
    for node in nodes:
        if isinstance(node, str):
            texts.append(node)
        else:
            texts.append(node.text_content() or '')
    return texts


def _css_to_xpath(selector: str) -> str:
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
                xpath = '//'
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
