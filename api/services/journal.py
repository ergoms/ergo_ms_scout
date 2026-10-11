"""Журнал запуска: что скачано и сколько раз сработал селектор."""

import re

from django.db import transaction
from django.utils import timezone

from ..models import Run
from .constants import RUN_AWAITING, RUN_RUNNING
from .html_extract import decode_html, selector_match_count
from .snapshot_store import read_snapshot_bytes

_ID = re.compile(r'''@id\s*=\s*["']([^"']+)["']''')


def append_event(run_id, message: str) -> None:
    text = (message or '').strip()
    if not text:
        return
    with transaction.atomic():
        run = Run.objects.select_for_update().get(pk=run_id)
        events = list(run.events or [])
        events.append({
            'at': timezone.now().isoformat(timespec='seconds'),
            'message': text[:500],
        })
        run.events = events[-100:]
        run.save(update_fields=['events'])


def journal_lines(run: Run) -> list[str]:
    if run.snapshots.exists():
        return _diagnose(run)
    return stored_messages(run) or ['Записей журнала пока нет.']


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
    status: str = '',
    approved: bool = False,
    content_type: str = '',
    charset: str = '',
) -> list[str]:
    text = decode_html(body, content_type, charset)
    lines = []
    for rule in rules or []:
        name = rule.get('name') or 'field'
        kind = rule.get('kind') or 'css'
        selector = rule.get('selector') or ''
        scope = 'в карточке' if rule.get('scope') == 'record' else 'на странице'
        try:
            count = selector_match_count(body, rule, content_type, charset)
        except Exception as exc:
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
    lines.append(_outcome(status, approved, row_count))
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
    snapshots = list(run.snapshots.all())
    if not snapshots:
        lines.append('Сохранённой страницы нет.')
    for snapshot in snapshots:
        lines.append(
            f'Страница скачана: HTTP {snapshot.http_status}, {snapshot.byte_size} байт.'
        )
        try:
            body = read_snapshot_bytes(snapshot.content_path)
        except (OSError, ValueError):
            lines.append('Файл снимка не прочитан.')
            continue
        if len(body) != snapshot.byte_size:
            lines.append(f'После распаковки {len(body)} байт.')
        approved = _approved(run)
        row_count = run.records.filter(snapshot=snapshot).count()
        lines.extend(describe_extract(
            body,
            rules,
            row_count,
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
