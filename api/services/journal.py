"""Журнал запуска: что скачано и сколько раз сработал селектор."""

import re

from django.db import transaction
from django.utils import timezone

from ..models import Run
from .constants import RUN_AWAITING, RUN_EXPLORING, RUN_RUNNING
from .errors import ScoutError
from .html_extract import _containers, _nodes, _parse, decode_html
from .snapshot_store import read_snapshot_bytes

_ID = re.compile(r'''@id\s*=\s*["']([^"']+)["']''')


def append_event(run_id, message: str) -> None:
    append_events(run_id, (message,))


def append_events(run_id, messages) -> None:
    timestamp = timezone.now().isoformat(timespec='seconds')
    entries = [
        {'at': timestamp, 'message': text[:500]}
        for message in messages if (text := (message or '').strip())
    ]
    if not entries:
        return
    with transaction.atomic():
        run = Run.objects.select_for_update().get(pk=run_id)
        events = list(run.events or [])
        run.events = (events + entries)[-100:]
        run.save(update_fields=['events'])


def journal_lines(run: Run) -> list[str]:
    if run.status == RUN_AWAITING and not (run.job_revision.crawl or {}).get('page_types'):
        return _diagnose(run)
    stored = stored_messages(run)
    if stored:
        return stored
    if run.status == RUN_EXPLORING:
        return ['Проверяем страницу.']
    return ['Записей журнала пока нет.']


def stored_messages(run: Run) -> list[str]:
    stored = []
    for item in run.events or []:
        if isinstance(item, dict) and item.get('message'):
            stored.append(str(item['message']))
        elif item:
            stored.append(str(item))
    return stored


def describe_extract(
    body: bytes,
    rules: list[dict],
    row_count: int,
    *,
    record=None,
    status: str = '',
    approved: bool = False,
    content_type: str = '',
    charset: str = '',
) -> list[str]:
    text = decode_html(body, content_type, charset)
    outcome = _outcome(status, approved, row_count)
    try:
        root = _parse(body, content_type, charset)
        cards = _containers(root, record) or []
    except ScoutError as exc:
        return [f'Снимок не разобран: {exc}.', outcome]
    lines = []
    for rule in rules or []:
        name = rule.get('name') or 'field'
        kind = rule.get('kind') or 'css'
        selector = rule.get('selector') or ''
        relative = rule.get('scope') == 'record'
        scope = 'в карточке' if relative else 'на странице'
        try:
            count = sum(len(_nodes(card, rule, relative=True)) for card in cards) if relative else len(
                _nodes(root, rule, relative=False),
            )
        except ScoutError as exc:
            lines.append(f'Поле {name}: селектор не применился ({exc}).')
            continue
        repeat = 'несколько значений' if rule.get('many') else 'одно значение'
        lines.append(f'Поле {name}: {kind} {selector} — совпадений {count}, {scope}, {repeat}.')
        for element_id in _ID.findall(selector):
            if element_id not in text:
                lines.append(
                    f'В сохранённой странице нет id="{element_id}". '
                    'Такой блок часто появляется только после скриптов, а скачан исходный HTML.'
                )
    lines.append(outcome)
    return lines


def _outcome(status: str, approved: bool, row_count: int) -> str:
    if status == RUN_AWAITING and not approved:
        return 'Предпросмотр готов. Подтвердите план.'
    if status == RUN_AWAITING and approved:
        return 'План подтверждён. Начните сбор.'
    if status == RUN_RUNNING:
        return f'Сбор идёт. Записано строк по этой странице: {row_count}.'
    if row_count:
        return f'Записано строк: {row_count}.'
    return 'Строк результата нет.'


def _diagnose(run: Run) -> list[str]:
    lines = []
    rules = run.job_revision.fields or []
    approved = _approved(run)
    snapshots = list(run.snapshots.all())
    if not snapshots:
        lines.append('Сохранённой страницы нет.')
    for snapshot in snapshots:
        lines.append(
            f'Страница скачана: HTTP {snapshot.http_status}, {snapshot.byte_size} байт.'
        )
        try:
            if snapshot.html_availability != 'available':
                lines.append('HTML снимка недоступен; метаданные и Evidence сохранены.')
                continue
            body = read_snapshot_bytes(snapshot.content_path)
        except (OSError, ValueError):
            lines.append('Файл снимка не прочитан.')
            continue
        if len(body) != snapshot.byte_size:
            lines.append(f'После распаковки {len(body)} байт.')
        lines.extend(describe_extract(
            body,
            rules,
            0,
            record=run.job_revision.record or None,
            status=run.status,
            approved=approved,
            content_type=snapshot.content_type,
            charset=snapshot.charset,
        ))
    if not lines:
        lines.append('Записей журнала пока нет.')
    return lines


def _approved(run: Run) -> bool:
    revision = run.job_revision
    return bool(revision.approved_plan_hash) and revision.approved_plan_hash == revision.plan_hash
