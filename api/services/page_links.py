"""Ссылки со сохранённой страницы. Скачивание и записи здесь не делаются."""

from urllib.parse import urljoin, urlsplit

from lxml import html as lxml_html
from lxml.etree import LxmlError

from .html_extract import decode_html


def has_crawl_links(body: bytes, content_type: str, charset: str, page_url: str) -> bool | None:
    try:
        hrefs, _base = iter_hrefs(body, content_type, charset, page_url)
    except (LxmlError, ValueError):
        return None
    return bool(hrefs)


def iter_hrefs(body: bytes, content_type: str, charset: str, page_url: str) -> tuple[list[str], str]:
    root = lxml_html.fromstring(decode_html(body, content_type, charset))
    base = page_url
    bases = root.xpath('//base[@href]')
    if bases and (href := bases[0].get('href')):
        try:
            candidate = urljoin(page_url, href)
            target = urlsplit(candidate)
            if target.scheme in ('http', 'https') and target.hostname and target.port != 0:
                base = candidate
        except ValueError:
            pass
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
