"""Ссылки со сохранённой страницы. Скачивание и записи здесь не делаются."""

from urllib.parse import urljoin

from lxml import html as lxml_html

from .html_extract import decode_html


def iter_hrefs(body: bytes, content_type: str, charset: str, page_url: str) -> tuple[list[str], str]:
    root = lxml_html.fromstring(decode_html(body, content_type, charset))
    base = page_url
    bases = root.xpath('//base[@href]')
    if bases and bases[0].get('href'):
        base = urljoin(page_url, bases[0].get('href'))
    hrefs = []
    for element in root.xpath('//a[@href]'):
        hrefs.append(element.get('href') or '')
    for element in root.xpath('//link[@href]'):
        rel = element.get('rel') or ''
        if not isinstance(rel, str):
            rel = ' '.join(rel)
        if 'next' in rel.lower().split():
            hrefs.append(element.get('href') or '')
    return hrefs, base
