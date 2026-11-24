"""Оформление безопасного снимка с сохранением идентификаторов узлов."""

import re

from lxml import etree

from .image_sources import image_source

_CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'"
_STYLE_END = re.compile(r'</style', re.IGNORECASE)
_GLOBAL_ATTRIBUTES = ('class', 'id', 'dir', 'lang', 'title', 'role', 'hidden', 'aria-hidden')
_LAYOUT_ATTRIBUTES = {
    'img': ('alt', 'align', 'width', 'height'),
    'table': ('align', 'border', 'cellpadding', 'cellspacing', 'width', 'height'),
    'col': ('span', 'width'),
    'colgroup': ('span', 'width'),
    'tr': ('align', 'valign', 'height'),
    'td': ('align', 'valign', 'colspan', 'rowspan', 'width', 'height'),
    'th': ('align', 'valign', 'colspan', 'rowspan', 'width', 'height'),
    'input': ('placeholder', 'checked', 'size'),
    'option': ('selected', 'label'),
    'select': ('size', 'multiple'),
    'textarea': ('placeholder', 'cols', 'rows'),
}
_FORM_TAGS = frozenset(('button', 'fieldset', 'form', 'input', 'label', 'legend', 'option',
                        'optgroup', 'select', 'textarea'))
_VISIBLE_INPUT_TYPES = frozenset(('button', 'checkbox', 'hidden', 'number', 'radio', 'search', 'text'))
_DROPPED_STYLE_ANCESTORS = frozenset(('svg', 'math', 'template', 'noscript', 'iframe', 'object'))
_MAX_IMAGE_BYTES = 3 * 1024 * 1024
_MAX_CSS_BYTES = 1024 * 1024


def styled_editor(document, assets, page_url):
    """Встраивает оформление и растровые изображения, загруженные сервером."""
    head, body = document.document
    source_root = document.root if document.root.tag == 'html' else None
    source_body = source_root.find('body') if source_root is not None else None
    head[0].set('content', _CSP)
    _copy_attributes(source_root, document.document)
    _copy_attributes(source_body, body)

    # The snapshot tree and rule checks continue to use the original map.
    targets = [
        (document.nodes[target.get('data-scout-node')], target)
        for target in body.iterdescendants() if 'data-scout-node' in target.attrib
    ]
    stylesheets = _stylesheets(document.root, assets, page_url)
    for source, target in targets:
        if source.tag != 'img':
            _preserve_control(source, target)
            _copy_attributes(source, target)

    _embed_images(targets, assets, page_url)
    styled_nodes = [(source_root, document.document), (source_body, body), *targets]
    _embed_styles(head, styled_nodes, stylesheets, assets, page_url)
    tags = {
        target.get('data-scout-node'): target.tag for _, target in targets
        if target.tag in _FORM_TAGS or target.tag == 'img'
    }
    return document.editor_html(), tags


def _preserve_control(source, target):
    if source.tag not in _FORM_TAGS:
        return
    target.tag = source.tag
    if source.tag == 'button':
        target.set('type', 'button')
    elif source.tag == 'input':
        kind = (source.get('type') or '').lower()
        target.set('type', kind if kind in _VISIBLE_INPUT_TYPES else 'text')
        if target.get('type') not in ('checkbox', 'radio', 'hidden', 'button'):
            target.set('readonly', '')
    elif source.tag == 'textarea':
        target.set('readonly', '')


def _embed_images(targets, assets, page_url):
    remaining = _MAX_IMAGE_BYTES
    images = [(source, target) for source, target in targets if source.tag == 'img']
    for source, target in sorted(images, key=lambda pair: _image_priority(pair[0])):
        address = image_source(source, page_url)
        preview = assets.image(address) if address and remaining else None
        if not preview or len(preview) > remaining:
            _copy_attributes(source, target, layout=False)
            continue
        remaining -= len(preview)
        target.tag = 'img'
        target.text = None
        _copy_attributes(source, target)
        target.set('src', preview)
        target.set('data-scout-image', 'preview')


def _embed_styles(head, styled_nodes, stylesheets, assets, page_url):
    remaining = _MAX_CSS_BYTES
    for text, base_url, media in stylesheets:
        if not remaining:
            break
        css = assets.style(text, base_url)
        if not css:
            continue
        # An HTML style element has raw text content. Escape its HTML end marker.
        css = _STYLE_END.sub(lambda _: r'\3c /style', css)
        remaining, accepted = _reserve(css, remaining)
        if accepted:
            tag = etree.SubElement(head, 'style')
            if media:
                tag.set('media', media)
            tag.text = css

    for source, target in styled_nodes:
        if not remaining:
            break
        if source is None or not source.get('style'):
            continue
        css = assets.style(source.get('style'), page_url, declarations=True)
        remaining, accepted = _reserve(css, remaining)
        if accepted:
            target.set('style', css)


def _copy_attributes(source, target, *, layout=True):
    if source is None:
        return
    names = _GLOBAL_ATTRIBUTES + (_LAYOUT_ATTRIBUTES.get(source.tag, ()) if layout else ())
    for name in names:
        value = source.get(name)
        if value is not None:
            target.set(name, value)


def _stylesheets(root, assets, page_url):
    stylesheets = []
    if root.tag != 'html':
        return stylesheets
    for node in root.xpath('head/style | head/link | body//style | body//link'):
        if any(ancestor.tag in _DROPPED_STYLE_ANCESTORS for ancestor in node.iterancestors()):
            continue
        if (node.get('type') or 'text/css').lower() != 'text/css':
            continue
        if node.tag == 'style':
            stylesheets.append((node.text or '', page_url, node.get('media')))
        else:
            rel = (node.get('rel') or '').lower().split()
            if 'stylesheet' not in rel or 'alternate' in rel or 'disabled' in node.attrib:
                continue
            href = node.get('href')
            fetched = assets.stylesheet(href, page_url) if href else None
            if fetched:
                text, final_url = fetched
                stylesheets.append((text, final_url, node.get('media')))
    return stylesheets


def _image_priority(node):
    size = [node.get(key, '') for key in ('width', 'height')]
    substantial = any(re.fullmatch(r'[1-9]\d{2,4}', value) for value in size)
    return (
        node.get('fetchpriority', '').lower() != 'high',
        not (substantial or 'image' in node.get('itemprop', '').split()),
        node.get('loading', '').lower() == 'lazy',
    )


def _reserve(css, remaining):
    size = len(css.encode('utf-8')) if css else 0
    if not size or size > remaining:
        return remaining, False
    return remaining - size, True
