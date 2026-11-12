"""Извлечение полей из сохранённого HTML. Загрузку и запись в базу не делает."""

import hashlib
import json
import re
from html.parser import HTMLParser

from lxml import html as lxml_html
from lxml.etree import XPathError, _Comment, _ProcessingInstruction

from ..contracts.envelope import PROTOCOL_VERSION, build_extract_request
from .constants import EXTRACTOR_ID, EXTRACTOR_VERSION, FIELD_TEXT_LIMIT
from .errors import ScoutError
from .value_types import legacy_values, typed_cell

_SPACE = re.compile(r'\s+')
_SIMPLE = re.compile(r'^(?P<tag>[\w-]+|\*)?(?P<rest>(?:[.#][\w-]+)*)$')
_CLASS = re.compile(r'[.#][\w-]+')
_CHARSET_LABEL = re.compile(r'[A-Za-z0-9._-]+')
_HEADER_CHARSET = re.compile(r'charset\s*=\s*["\']?([A-Za-z0-9._-]+)', re.I)
_XML_DECLARATION = re.compile(r'^\s*<\?xml(?=\s)(.*?)\?>', re.S)
_XML_ENCODING = re.compile(r'\bencoding\s*=\s*["\']([A-Za-z0-9._-]+)["\']')


def extract_html(body: bytes, rules: list[dict], record=None, content_type: str = '', charset: str = '',
                 *, require_match: bool = False, sku_field: str | None = None,
                 identity_url: str | None = None) -> list[dict]:
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
        empty = lxml_html.Element('div')
        for rule in rules:
            _nodes(empty, rule, relative=rule.get('scope') == 'record')
        return []
    else:
        page_rules = [rule for rule in rules if rule.get('scope') != 'record']
        record_rules = [rule for rule in rules if rule.get('scope') == 'record']
        targets = containers
    keyed = sku_field is not None or identity_url is not None
    actual_evidence = require_match or keyed
    shared = [_cell(root, rule, relative=False, actual_evidence=actual_evidence, sku_field=sku_field, keyed=keyed)
              for rule in page_rules]
    has_conversion = any('conversion' in rule for rule in rules)
    rows = []
    for index, node in enumerate(targets):
        values = {}
        evidence = []
        relative_cells = [_cell(node, rule, relative=True, actual_evidence=actual_evidence, sku_field=sku_field, keyed=keyed)
                          for rule in record_rules]
        for rule, cell in zip(page_rules + record_rules, shared + relative_cells):
            _put(values, evidence, rule, cell, index, typed_evidence=keyed)
        if require_match and not evidence:
            continue
        if keyed:
            sku_match = (('resolved', identity_url) if identity_url else ('invalid', None)) if identity_url is not None else next(
                cell['sku_match'] for cell in shared + relative_cells if 'sku_match' in cell)
            rows.append({'observation_index': index, 'sku_match': sku_match, 'values': values, 'evidence': evidence})
            continue
        identity_values = legacy_values(values) if has_conversion else values
        identity = hashlib.sha256(
            json.dumps({'index': index, 'values': identity_values}, ensure_ascii=False, sort_keys=True).encode('utf-8')
        ).hexdigest()[:64]
        rows.append({'identity_key': identity, 'values': values, 'evidence': evidence})
    return rows


def check_plan_selectors(body: bytes, rules: list[dict], record, *, charset: str = '') -> tuple[str, str] | None:
    """Первая ошибка или правило без совпадений на всём примере."""
    root = _parse(body, '', charset)
    try:
        containers = _containers(root, record)
    except ScoutError:
        return 'selector', '/record/selector'
    if containers == []:
        return 'selector_no_match', '/record/selector'

    unmatched = {}
    for index, rule in enumerate(rules):
        if rule['scope'] == 'record':
            unmatched[index] = rule
            continue
        path = f'/fields/{index}/selector'
        try:
            matches = _nodes(root, rule, relative=False)
        except ScoutError:
            return 'selector', path
        if not matches:
            return 'selector_no_match', path

    for node in containers or []:
        if not unmatched:
            break
        target = _isolate(node)
        for index, rule in list(unmatched.items()):
            try:
                matches = _nodes(target, rule, relative=True)
            except ScoutError:
                return 'selector', f'/fields/{index}/selector'
            if matches:
                del unmatched[index]
    if unmatched:
        return 'selector_no_match', f'/fields/{next(iter(unmatched))}/selector'
    return None


def decode_html(body: bytes, content_type: str = '', charset: str = '') -> str:
    text = _decode_bom(body)
    if text is None:
        text = _decode_declared(body, (charset or _declared_charset(body, content_type)).strip())
    # Парсер получает Unicode, поэтому исходная декларация кодировки ему не нужна.
    return _XML_DECLARATION.sub('', text, count=1)


def _decode_declared(body: bytes, chosen: str) -> str:
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
    bom_name = _bom_name(body)
    if bom_name:
        return bom_name
    declared = _declared_charset(body, content_type).strip()
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
    elements = []
    for node in nodes:
        if not isinstance(getattr(node, 'tag', None), str):
            raise ScoutError('selector', 'Селектор карточки должен находить элементы страницы')
        elements.append(node)
    return elements


def _cell(node, rule: dict, *, relative: bool, actual_evidence: bool, sku_field: str | None, keyed=False) -> dict:
    target = _isolate(node) if relative else node
    is_sku = rule['name'] == sku_field
    found = _nodes(target, rule, relative=relative, raw_result=is_sku)
    matches = _coerce(found) if is_sku else found
    cell = cell_from_matches(
        matches, many=rule.get('many'), conversion=rule.get('conversion'), collect_evidence=keyed,
    )
    if actual_evidence and not matches:
        cell['parts'] = []
    if is_sku:
        cell['sku_match'] = _sku_match(found, matches)
    return cell


def _sku_match(found, matches) -> tuple[str, str | None]:
    if len(matches) > 1:
        return 'ambiguous', None
    if isinstance(found, (int, float, bool)):
        return 'invalid', None
    sku = _node_text(matches[0]).strip() if matches else ''
    if not sku:
        return 'missing', None
    if len(sku) > 200:
        return 'invalid', None
    return 'resolved', sku


def cell_from_matches(matches, *, many, conversion=None, collect_evidence=False) -> dict:
    texts = [_node_text(node) for node in matches]
    chosen = (texts if many else texts[:1]) or ['']
    raw_parts = [text[:FIELD_TEXT_LIMIT] for text in chosen]
    raw = '\n'.join(raw_parts)
    normalized = ' | '.join(part for part in (_SPACE.sub(' ', item).strip() for item in raw_parts) if part)
    cell = {'raw': raw[:FIELD_TEXT_LIMIT], 'normalized': normalized[:FIELD_TEXT_LIMIT], 'parts': raw_parts}
    if conversion is not None:
        cell['typed'] = typed_cell(texts[0] if texts else None, conversion)
    if collect_evidence:
        cell['evidence_texts'] = texts
    return cell


def _put(values, evidence, rule, cell, index: int, *, typed_evidence: bool) -> None:
    values[rule['name']] = {'raw': cell['raw'], 'normalized': cell['normalized']}
    if 'typed' in cell:
        values[rule['name']]['typed'] = cell['typed']
    parts = cell['evidence_texts'] if typed_evidence else cell['parts']
    for part_index, text in enumerate(parts):
        part = text[:FIELD_TEXT_LIMIT]
        normalized = _SPACE.sub(' ', part).strip()
        evidence.append({
            'field_path': rule['name'],
            'locator': f"{rule['kind']}:{rule['selector']}#{index}.{part_index}",
            'raw_value': part,
            'normalized_value': normalized,
            'extractor_id': EXTRACTOR_ID,
            'extractor_version': EXTRACTOR_VERSION,
            **({'typed': typed_cell(text, rule['conversion'])} if typed_evidence and 'typed' in cell else {}),
        })


def _nodes(root, rule: dict, *, relative: bool, raw_result: bool = False):
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
    return found if raw_result else _coerce(found)


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
    if isinstance(node, (_Comment, _ProcessingInstruction)):
        return node.text or ''
    if isinstance(node, tuple):
        # XPath возвращает узел пространства имён как (префикс, URI).
        return node[1]
    return node.text_content() or ''


def _bind_xpath(selector: str, relative: bool) -> str:
    """В карточке выражение выполняется на отдельном дереве, без переписывания формулы."""
    return (selector or '').strip()


def _isolate(node):
    """Отдельное дерево карточки: абсолютный XPath и union не видят соседние карточки."""
    from lxml import etree
    raw = etree.tostring(node, with_tail=False)
    return lxml_html.fragment_fromstring(raw)


def _decode_bom(body: bytes):
    name = _bom_name(body)
    if not name:
        return None
    if name == 'utf-8':
        return body.decode('utf-8-sig', errors='replace')
    return body.decode(name, errors='replace')


def _bom_name(body: bytes) -> str:
    if body.startswith((b'\xff\xfe\x00\x00', b'\x00\x00\xfe\xff')):
        return 'utf-32'
    if body.startswith((b'\xff\xfe', b'\xfe\xff')):
        return 'utf-16'
    if body.startswith(b'\xef\xbb\xbf'):
        return 'utf-8'
    return ''


def _header_charset(content_type: str) -> str:
    match = _HEADER_CHARSET.search(content_type or '')
    return match.group(1) if match else ''


def _declared_charset(body: bytes, content_type: str) -> str:
    if charset := _header_charset(content_type):
        return charset
    prefix = body[:4096].decode('latin-1')
    if declaration := _XML_DECLARATION.match(prefix):
        if encoding := _XML_ENCODING.search(declaration[1]):
            return encoding[1]
    parser = _MetaCharsetParser()
    try:
        parser.feed(prefix)
    except (AssertionError, ValueError):
        # Некорректная разметка не должна срывать распознавание по байтам.
        pass
    return parser.charset


class _MetaCharsetParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.charset = ''

    def handle_starttag(self, tag, attrs):
        if tag != 'meta' or self.charset:
            return
        attributes = dict(attrs)
        charset = (attributes.get('charset') or '').strip()
        if not charset and (attributes.get('http-equiv') or '').strip().lower() == 'content-type':
            charset = _header_charset(attributes.get('content') or '')
        if _CHARSET_LABEL.fullmatch(charset):
            self.charset = charset


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
