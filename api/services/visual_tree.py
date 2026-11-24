"""Ограниченные метаданные тех же узлов, которые показывает API-002."""

import hashlib
import json
import re
import unicodedata

from django.core import signing

from .errors import ScoutError

TREE_SCHEMA_VERSION = b'scout-visual-map-v1:dom-metadata-v2'
CURSOR_SALT = 'scout.dom-tree.v1'
CURSOR_AGE = 15 * 60
MAX_NODES = 10_000
MAX_DEPTH = 64
MAX_RESPONSE_BYTES = 256 * 1024
_NODE = re.compile(r'n[1-9][0-9]{0,4}\Z')
_VERSION = re.compile(r'dom-v1:[a-f0-9]{64}\Z')
_TAG = re.compile(r'[a-z][a-z0-9:-]{0,63}\Z')
_ID = re.compile(r'[A-Za-z_][A-Za-z0-9_-]{0,127}\Z')
_CLASS = re.compile(r'[A-Za-z_][A-Za-z0-9_-]{0,63}\Z')
_SPACE = re.compile(r'\s+')
MISSING = object()


def invalid(path='/'):
    return ScoutError('invalid_query', 'Проверьте параметры запроса дерева', path=path)


def tree_version(snapshot):
    digest = hashlib.sha256(TREE_SCHEMA_VERSION)
    for part in (str(snapshot.public_id).encode(), (snapshot.charset or '').encode(), bytes(snapshot.body)):
        digest.update(len(part).to_bytes(8, 'big'))
        digest.update(part)
    return 'dom-v1:' + digest.hexdigest()


def check_tree_version(snapshot, value=MISSING, *, required=False):
    current = tree_version(snapshot)
    if value is MISSING and not required:
        return current
    if not isinstance(value, str) or not _VERSION.fullmatch(value):
        raise invalid('/tree_version')
    if value != current:
        raise ScoutError('tree_version_changed', 'Представление снимка изменилось. Загрузите его заново.')
    return current


def validate_keys(data, allowed):
    if not isinstance(data, dict) or set(data) - set(allowed):
        raise invalid()


def page_limit(value, *, query=False):
    if query:
        if not isinstance(value, str) or not re.fullmatch(r'[0-9]{1,3}', value):
            raise invalid('/limit')
        value = int(value)
    if type(value) is not int or not 1 <= value <= 100:
        raise invalid('/limit')
    return value


def node_id(value, path='/node_id'):
    if not isinstance(value, str) or not _NODE.fullmatch(value):
        raise invalid(path)
    return value


def search_terms(data):
    validate_keys(data, ('q', 'tag', 'limit', 'cursor', 'tree_version'))
    query = data.get('q')
    if not isinstance(query, str):
        raise invalid('/q')
    query = _SPACE.sub(' ', unicodedata.normalize('NFC', query)).strip()
    if not 2 <= len(query) <= 100 or any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in query):
        raise invalid('/q')
    tag = data.get('tag', MISSING)
    if tag is not MISSING:
        if not isinstance(tag, str) or not _TAG.fullmatch(tag.lower()):
            raise invalid('/tag')
        tag = tag.lower()
    return query.casefold(), None if tag is MISSING else tag


def _normalized(value):
    return _SPACE.sub(' ', unicodedata.normalize('NFC', value)).strip().casefold()


def _prefix(element, prefixes):
    """Не более 122 нормализованных символов на поддерево, без повторных itertext."""
    result = ''
    complete = True

    def append(text, whole=True):
        nonlocal result, complete
        if result.endswith(' ') and text.startswith(' '):
            text = text[1:]
        result += text
        if len(result) > 122:
            result, complete = result[:122], False
        if not whole:
            complete = False

    append(_SPACE.sub(' ', element.text or ''))
    for child in element:
        if not complete:
            break
        prefix, whole = prefixes[child]
        append(prefix, whole)
        if complete:
            append(_SPACE.sub(' ', child.tail or ''))
    return result, complete


class VisualTree:
    def __init__(self, document, snapshot):
        self.snapshot = snapshot
        self.version = tree_version(snapshot)
        self.nodes = {}
        self.children = {None: []}
        editor_tags = snapshot.editor_tags
        rendered = [el for el in document.document.iter() if el.get('data-scout-node')]
        if len(rendered) > MAX_NODES:
            raise ScoutError('tree_too_large', 'Слишком много узлов для дерева')
        self.max_depth = None
        for element in rendered:
            uid = element.get('data-scout-node')
            parent = element.getparent().get('data-scout-node')
            depth = self.nodes[parent]['depth'] + 1 if parent else 0
            if depth > MAX_DEPTH:
                raise ScoutError('tree_depth_exceeded', 'Дерево глубже допустимого предела')
            siblings = self.children[parent]
            original = document.nodes[uid]
            tag = original.tag if _TAG.fullmatch(original.tag) else None
            source_id = original.get('id')
            element_id = source_id if source_id is not None and _ID.fullmatch(source_id) else None
            limited = tag is None or (source_id is not None and element_id is None)
            classes = []
            for match in re.finditer(r'\S+', original.get('class', '')):
                value = match.group()
                if len(value) > 64 or not _CLASS.fullmatch(value):
                    limited = True
                elif value not in classes:
                    if len(classes) == 8:
                        limited = True
                        break
                    classes.append(value)
            self.nodes[uid] = {
                'node_id': uid, 'parent_id': parent, 'child_index': len(siblings), 'depth': depth,
                'tag': tag, 'rendered_tag': editor_tags.get(uid, element.tag),
                'element_id': element_id, 'classes': classes,
                'text_preview': '', 'children_count': 0, 'metadata_limited': limited,
            }
            siblings.append(uid)
            self.children[uid] = []
            self.max_depth = max(self.max_depth or 0, depth)
        prefixes = {}
        for element in reversed(rendered):
            prefix, complete = _prefix(element, prefixes)
            prefixes[element] = (prefix, complete)
            item = self.nodes[element.get('data-scout-node')]
            text = prefix.strip()
            item['text_preview'] = text[:120]
            item['metadata_limited'] |= not complete or len(text) > 120
            item['children_count'] = len(self.children[item['node_id']])

    def envelope(self):
        return {'snapshot_public_id': str(self.snapshot.public_id), 'tree_version': self.version}

    def get_node(self, uid, path='/node_id'):
        uid = node_id(uid, path)
        if uid not in self.nodes:
            raise ScoutError('node_not_found', 'Узел отсутствует в снимке')
        return self.nodes[uid]

    def detail(self, uid):
        item = self.get_node(uid)
        ancestors = []
        parent = item['parent_id']
        while parent:
            ancestors.append(self.nodes[parent])
            parent = self.nodes[parent]['parent_id']
        return {**self.envelope(), 'node': item, 'ancestors': list(reversed(ancestors))}

    def branch(self, data):
        validate_keys(data, ('parent_id', 'around_node_id', 'limit', 'cursor', 'tree_version'))
        limit = page_limit(data.get('limit', '50'), query=True)
        parent = data.get('parent_id')
        if 'parent_id' in data:
            self.get_node(parent, '/parent_id')
        around = data.get('around_node_id', MISSING)
        start = 0
        if around is not MISSING:
            if 'cursor' in data:
                raise invalid('/around_node_id')
            selected = self.get_node(around, '/around_node_id')
            if selected['parent_id'] != parent:
                raise ScoutError('node_not_child', 'Узел не является ребёнком указанного родителя')
            start = selected['child_index'] // limit * limit
        result = self._page(self.children[parent], data, limit, ['branch', parent], start)
        result['parent_id'] = parent
        if parent is None:
            result.update(scope='selectable_snapshot_nodes', node_count=len(self.nodes), max_depth=self.max_depth)
        return result

    def search(self, data, query, tag):
        limit = page_limit(data.get('limit', 20))
        matches = []
        for uid, item in self.nodes.items():
            if tag is not None and item['tag'] != tag:
                continue
            values = [item['tag'] or '', item['element_id'] or '', *item['classes'], item['text_preview']]
            if any(query in _normalized(value) for value in values):
                matches.append(uid)
        fingerprint = hashlib.sha256(json.dumps([query, tag], ensure_ascii=False).encode('utf-8')).hexdigest()
        result = self._page(matches, data, limit, ['search', fingerprint])
        result.update(scope='all_selectable_nodes', search_fields=['tag', 'element_id', 'classes', 'text_preview'])
        return result

    def _page(self, ids, data, limit, operation, start=0):
        binding = [str(self.snapshot.user_public_id), str(self.snapshot.public_id), *operation, limit]
        cursor = data.get('cursor', MISSING)
        if cursor is not MISSING:
            if not isinstance(cursor, str) or not cursor:
                raise invalid('/cursor')
            if not cursor.isascii() or len(cursor) > 2048:
                raise ScoutError('invalid_cursor', 'Курсор недоступен')
            try:
                payload = signing.loads(cursor, salt=CURSOR_SALT, max_age=CURSOR_AGE)
            except signing.SignatureExpired:
                raise ScoutError('cursor_expired', 'Срок курсора истёк. Начните чтение заново.') from None
            except (signing.BadSignature, ValueError, TypeError, UnicodeError):
                raise ScoutError('invalid_cursor', 'Курсор недоступен') from None
            if not isinstance(payload, dict) or payload.get('binding') != binding:
                raise ScoutError('invalid_cursor', 'Курсор не соответствует запросу')
            if payload.get('version') != self.version:
                raise ScoutError('tree_version_changed', 'Представление снимка изменилось. Загрузите его заново.')
            start = payload.get('start')
            if type(start) is not int or start < 0 or start >= max(1, len(ids)):
                raise ScoutError('invalid_cursor', 'Курсор недоступен')

        def token(position):
            return signing.dumps({'binding': binding, 'version': self.version, 'start': position}, salt=CURSOR_SALT)

        return {
            **self.envelope(), 'total_count': len(ids), 'items': [self.nodes[uid] for uid in ids[start:start + limit]],
            'previous_cursor': token(max(0, start - limit)) if start else None,
            'next_cursor': token(start + limit) if start + limit < len(ids) else None,
        }
