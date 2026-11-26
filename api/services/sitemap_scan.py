"""Один XML-документ на задачу, общий подтверждённый план и барьер перед HTML-обходом."""

from urllib.parse import urlsplit, urlunsplit

from ..models import Task
from .constants import OPEN_TASK_STATUSES, TASK_SITEMAP, TASK_PENDING, TASK_RETRY_WAIT
from .errors import FetchError, ScoutError
from .public_http import public_url_allowed
from .robots import MAX_ROBOTS_BYTES, robots_url
from .url_norm import canonical_host, host_allowed, resolve_url, rules_match, url_key

MAX_MAPS = 20
MAX_DEPTH = 3
MAX_XML_BYTES = 5_000_000
MAX_XML_DOCUMENT_BYTES = 1_000_000
MAX_URL_ENTRIES = 50_000
STATE_KEY = 'sitemap_scan'


def map_url(raw):
    public_url_allowed(raw)
    parts = urlsplit(raw)
    host = canonical_host(parts.hostname or '')
    if host is None:
        raise ScoutError('sitemap_invalid', 'Недопустимый адрес карты')
    netloc = f'[{host}]' if ':' in host else host
    port = parts.port
    if port and port != (443 if parts.scheme == 'https' else 80):
        netloc += f':{port}'
    return urlunsplit((parts.scheme, netloc, parts.path or '/', parts.query, ''))


def _root(run):
    return Task.objects.get(run=run, kind=TASK_SITEMAP, idempotency_key='sitemap')


def _state(root):
    return root.payload.get(STATE_KEY) or {
        'maps': [] if root.payload.get('source') == 'robots' else [map_url(root.payload['url'])],
        'aliases': [], 'urls': [],
        'charged_bytes': 0, 'url_entries': 0, 'processed': 0, 'sealed': False, 'edges': {},
    }


def _save(root, state):
    root.payload = {**root.payload, STATE_KEY: state}
    root.save(update_fields=['payload'])


def reserve_xml(run, task):
    """Вызывается под блокировкой Run до аренды. Неудачная попытка расходует резерв целиком."""
    root = _root(run)
    state = _state(root)
    maximum = min(MAX_XML_BYTES, run.job_revision.crawl['max_total_bytes'])
    available = maximum - state['charged_bytes']
    if available <= 0:
        # Ошибка будет записана обычной попыткой, без HTTP-запроса.
        allowance = 0
    else:
        allowance = min(run.job_revision.limits['max_bytes'], MAX_XML_DOCUMENT_BYTES, available)
        if task.payload.get('source') == 'robots':
            allowance = min(allowance, MAX_ROBOTS_BYTES)
        state['charged_bytes'] += allowance
    _save(root, state)
    # Корневая задача и состояние находятся в одной строке.
    if task.pk == root.pk:
        task.payload = root.payload
    task.payload = {**task.payload, 'xml_allowance': allowance}
    task.save(update_fields=['payload'])


def map_allowed(run, url):
    revision = run.job_revision
    source = revision.crawl.get('sitemap_url') or robots_url(revision.seed_urls[0])
    root_host = canonical_host(urlsplit(source).hostname or '')
    return (canonical_host(urlsplit(url).hostname or '') == root_host
            and host_allowed(url, run.job_revision.crawl['allowed_hosts']))


def finish_document(run, task, result, kind, urls, target_allowed):
    """Обновление состояния и дочерних задач входит в атомарное завершение попытки."""
    from .frontier import open_site

    root = _root(run)
    state = _state(root)
    requested, final = map_url(task.payload['url']), map_url(result['final_url'])
    ancestors = task.payload.get('ancestors', [])
    if final in ancestors:
        raise ScoutError('sitemap_cycle', 'Обнаружен цикл индексов или редирект на родительскую карту')
    graph = state.setdefault('edges', {})
    if final != requested:
        if _reaches(graph, final, requested):
            raise ScoutError('sitemap_cycle', 'Редирект замыкает цикл карт')
        graph.setdefault(requested, []).append(final)
    state['charged_bytes'] -= int(task.payload['xml_allowance']) - len(result['body'])
    if final in state['aliases']:
        # Разные URL могут привести к уже разобранному документу. Второй HTTP-переход неизбежен,
        # но его содержимое не регистрируется повторно.
        _save(root, state)
        _open_if_ready(run, task, root, state)
        return
    state['aliases'].append(final)
    if task.payload.get('source') == 'robots':
        state['robots_processed'] = True
    else:
        state['processed'] += 1
    if kind == 'index':
        state['index'] = True
        lineage = list(dict.fromkeys([*ancestors, requested, final]))
        children = []
        for raw in urls:
            normalized = map_url(raw)
            if not map_allowed(run, normalized):
                raise ScoutError('sitemap_scope', 'Дочерняя карта выходит за пределы сайта индекса')
            if normalized in lineage or _reaches(graph, normalized, final):
                raise ScoutError('sitemap_cycle', 'Индекс ссылается на себя или родительскую карту')
            if normalized not in graph.setdefault(final, []):
                graph[final].append(normalized)
            if normalized in state['maps'] or normalized in state['aliases']:
                continue
            depth = task.payload.get('depth', 0) + 1
            if depth > MAX_DEPTH or len(state['maps']) >= MAX_MAPS:
                raise ScoutError('sitemap_limit', 'Превышен предел количества или глубины XML-карт')
            state['maps'].append(normalized)
            children.append(Task(
                run=run, kind=TASK_SITEMAP, status=TASK_PENDING, fencing_token=1,
                idempotency_key='sitemap:' + url_key(normalized),
                payload={'url': normalized, 'depth': depth, 'ancestors': lineage},
            ))
        Task.objects.bulk_create(children)
    else:
        state['url_entries'] += len(urls)
        if state['url_entries'] > MAX_URL_ENTRIES:
            raise ScoutError('sitemap_limit', 'Превышен общий предел адресов XML-карт')
        valid = []
        for raw in urls:
            try:
                target_allowed(raw)
            except FetchError:
                continue
            valid.append(raw)
        if task.pk == root.pk and not state.get('index'):
            # Сохраняем порядок и прежние счётчики отсева для одиночного urlset.
            _save(root, state)
            open_site(run, sitemap_urls=valid)
            state['sealed'] = True
            _save(root, state)
            return
        known = set(state['urls'])
        capacity = run.job_revision.crawl['frontier_cap'] + 1
        for raw in valid:
            normalized, reason = resolve_url(raw, '', run.job_revision.crawl['drop_query'])
            if (reason or not host_allowed(normalized, run.job_revision.crawl['allowed_hosts'])
                    or not rules_match(normalized, run.job_revision.crawl['visit'])):
                continue
            if normalized not in known and len(state['urls']) < capacity:
                known.add(normalized)
                state['urls'].append(normalized)
    _save(root, state)
    _open_if_ready(run, task, root, state)


def _reaches(graph, start, target):
    pending, visited = [start], set()
    while pending:
        current = pending.pop()
        if current == target:
            return True
        if current not in visited:
            visited.add(current)
            pending.extend(graph.get(current, ()))
    return False


def _open_if_ready(run, task, root, state):
    from .frontier import open_site

    pending = run.tasks.filter(kind=TASK_SITEMAP, status__in=OPEN_TASK_STATUSES).exclude(pk=task.pk)
    if not state['sealed'] and not pending.exists() and not run.tasks.filter(kind=TASK_SITEMAP, status='failed').exists():
        open_site(run, sitemap_urls=state['urls'])
        state['sealed'] = True
        _save(root, state)


def cancel_pending_maps(run):
    run.tasks.filter(kind=TASK_SITEMAP, status__in=(TASK_PENDING, TASK_RETRY_WAIT)).update(
        status='cancelled', published_at=None, lease_until=None,
    )


def scan_payload(run):
    root = run.tasks.filter(kind=TASK_SITEMAP, idempotency_key='sitemap').first()
    if root is None:
        return None
    state = _state(root)
    result = {'maps_discovered': len(state['maps']), 'maps_processed': state['processed'],
            'url_entries': state['url_entries'], 'xml_bytes_charged': state['charged_bytes'],
            'ready': state['sealed'], 'max_maps': MAX_MAPS, 'max_depth': MAX_DEPTH,
            'max_xml_bytes': min(MAX_XML_BYTES, run.job_revision.crawl['max_total_bytes'])}
    if root.payload.get('source') == 'robots':
        result.update(discovery='robots', robots_url=root.payload['url'],
                      robots_processed=state.get('robots_processed', False), max_robots_bytes=MAX_ROBOTS_BYTES)
    return result
