"""Связь элементов безопасного представления с исходным HTML снимка."""

import re

from lxml import etree, html

from .errors import ScoutError
from .html_extract import _css_to_xpath, _nodes, _parse, cell_from_matches
from .plan import normalize_plan

_SAFE_TAGS = frozenset('''
    a abbr address article aside b blockquote br caption cite code col colgroup dd div dl dt em
    figcaption figure footer h1 h2 h3 h4 h5 h6 header hr i li main mark nav ol p pre q s section
    small span strong sub sup table tbody td tfoot th thead time tr u ul
'''.split())
_DROP_TAGS = frozenset('head script style iframe frame frameset object embed template noscript svg math'.split())
_CSS_NAME = re.compile(r'[a-zA-Z_][\w-]*\Z')
_MAX_ELEMENTS = 10_000
_SAMPLE_LIMIT = 20


class VisualDocument:
    def __init__(self, body, charset):
        self.root = _parse(body, '', charset)
        self.nodes = {}
        self.ids = {}
        self.document = etree.Element('html')
        head = etree.SubElement(self.document, 'head')
        etree.SubElement(head, 'meta', {
            'http-equiv': 'Content-Security-Policy',
            'content': "default-src 'none'; base-uri 'none'; form-action 'none'",
        })
        target = etree.SubElement(self.document, 'body')
        if self.root.tag == 'html':
            source = self.root.find('body')
            if source is not None:
                self._copy_children(source, target)
        else:
            self._copy(self.root, target)

    def _copy_children(self, source, target):
        text = [source.text] if source.text else []
        previous = None
        for child in source:
            copied = self._copy(child, target)
            if copied is not None:
                self._write_text(target, previous, text)
                previous = copied
                text = []
            if child.tail:
                text.append(child.tail)
        self._write_text(target, previous, text)

    @staticmethod
    def _write_text(parent, previous, parts):
        if parts:
            if previous is None:
                parent.text = ''.join(parts)
            else:
                previous.tail = ''.join(parts)

    def _copy(self, node, parent):
        if not isinstance(node.tag, str) or node.tag in _DROP_TAGS:
            return
        if len(self.nodes) >= _MAX_ELEMENTS:
            raise ScoutError('too_large', 'На странице слишком много элементов для редактора')
        if node.tag == 'img':
            # В снимке показываем подпись вместо загрузки внешнего изображения.
            tag = 'span'
        elif node.tag in _SAFE_TAGS:
            tag = node.tag
        else:
            tag = 'div'
        node_id = f'n{len(self.nodes) + 1}'
        self.nodes[node_id] = node
        self.ids[node] = node_id
        target = etree.SubElement(parent, tag, {'data-scout-node': node_id})
        self._copy_children(node, target)
        if node.tag == 'img':
            target.text = node.get('alt')
        return target

    def editor_html(self):
        return html.tostring(self.document, encoding='unicode', doctype='<!doctype html>')

    def check(self, *, source_url, record, field):
        if record is None and field is None:
            raise ScoutError('invalid', 'Укажите правило карточки или поля')

        selected_card = None
        if record is not None:
            record, selected_card = self._rule(record, self.root, relative=False, is_record=True)
            cards = _nodes(self.root, record, relative=False)
        else:
            cards = [None]

        field_rule = None
        if field is not None:
            if not isinstance(field, dict) or field.get('scope') not in ('page', 'record'):
                raise ScoutError('invalid', 'Укажите область действия поля')
            if not isinstance(field.get('many'), bool) or not isinstance(field.get('name'), str):
                raise ScoutError('invalid', 'Укажите имя поля и режим выбора значений')
            relative = field['scope'] == 'record'
            if relative and record is None:
                raise ScoutError('invalid', 'Для поля карточки нужен селектор карточки')

            context = self.root
            if relative and 'node_id' in field:
                node = self._selected_node(field['node_id'])
                card_set = set(cards)
                selected_card = next((ancestor for ancestor in node.iterancestors() if ancestor in card_set), None)
                if selected_card is None:
                    raise ScoutError('node_outside_record', 'Выберите элемент внутри найденной карточки')
                context = selected_card
            field_rule, _ = self._rule(field, context, relative=relative)
            field_rule = normalize_plan([source_url], [field_rule], record=record)['fields'][0]

        sample = cards[:_SAMPLE_LIMIT]
        if selected_card is not None and selected_card not in sample:
            sample[-1:] = [selected_card]
        rows = [{'node_id': self.ids.get(card)} for card in sample]
        if field_rule is not None:
            if field_rule['scope'] == 'page':
                matches = _nodes(self.root, field_rule, relative=False)
                result = self._field_result(matches, field_rule)
                for row in rows:
                    row.update(result)
            else:
                select = etree.XPath(_css_to_xpath(field_rule['selector'], relative=True))
                for row, card in zip(rows, sample):
                    row.update(self._field_result(select(card), field_rule))
                if len(cards) > len(sample) and not any(row['match_count'] for row in rows):
                    sampled = set(sample)
                    for card in cards:
                        if card in sampled:
                            continue
                        matches = select(card)
                        if matches:
                            # Keep a card explicitly selected in the editor.
                            del rows[-2 if sample[-1] is selected_card else -1]
                            rows.append({'node_id': self.ids.get(card), **self._field_result(matches, field_rule)})
                            break

        return {'record': record, 'field': field_rule, 'row_count': len(cards), 'rows': rows}

    def _selected_node(self, node_id):
        if not isinstance(node_id, str) or node_id not in self.nodes:
            raise ScoutError('unknown_node', 'Элемент отсутствует в снимке')
        return self.nodes[node_id]

    def _rule(self, data, context, *, relative, is_record=False):
        if not isinstance(data, dict):
            raise ScoutError('invalid', 'Правило задано неверно')
        node = None
        if 'node_id' in data:
            if 'selector' in data or 'kind' in data:
                raise ScoutError('invalid', 'Передайте элемент или готовый селектор')
            node = self._selected_node(data['node_id'])
            selector = self._suggest(node, context, relative=relative, is_record=is_record)
        else:
            selector = data.get('selector')
            if data.get('kind') != 'css' or not isinstance(selector, str):
                raise ScoutError('selector', 'В визуальном редакторе поддерживаются CSS-селекторы')
            selector = selector.strip()
        if not selector or len(selector) > 500:
            raise ScoutError('selector', 'Селектор пустой или слишком длинный')
        xpath = _css_to_xpath(selector, relative=relative)
        if xpath.count('/') > 16:
            raise ScoutError('selector', 'Слишком сложный селектор для визуального редактора')
        try:
            etree.XPath(xpath)
        except etree.XPathSyntaxError as exc:
            raise ScoutError('selector', 'Селектор не применился') from exc
        result = {'kind': 'css', 'selector': selector}
        if not is_record:
            result.update(name=data['name'], scope=data['scope'], many=data['many'])
        return result, node

    def _suggest(self, node, context, *, relative, is_record):
        tag = node.tag if _CSS_NAME.fullmatch(node.tag) else ''
        classes = [name for name in node.get('class', '').split() if _CSS_NAME.fullmatch(name)]
        candidates = [f'{tag if is_record else ""}.{name}' for name in classes[:8]]
        name = node.get('id', '')
        if _CSS_NAME.fullmatch(name):
            candidates.append(f'#{name}')
        if tag:
            candidates.append(tag)

        fallback = None
        for selector in candidates:
            if len(selector) > 500:
                continue
            matches = _nodes(context, {'kind': 'css', 'selector': selector}, relative=relative)
            if node in matches:
                if is_record or len(matches) == 1:
                    return selector
                fallback = fallback or selector
        if fallback:
            return fallback
        raise ScoutError('selector_not_suggested', 'Для этого элемента не удалось предложить селектор')

    def _field_result(self, matches, rule):
        ids = [self.ids[node] for node in matches if node in self.ids][:_SAMPLE_LIMIT]
        value = None
        if matches:
            cell = cell_from_matches(matches, many=rule['many'])
            value = {'raw': cell['raw'], 'normalized': cell['normalized']}
        return {'match_count': len(matches), 'match_node_ids': ids, 'value': value}
