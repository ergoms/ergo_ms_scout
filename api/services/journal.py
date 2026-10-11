"""Журнал запуска: что скачано и сколько раз сработал селектор."""

import re

from django.db import transaction
from django.utils import timezone

from ..models import Run
from .html_extract import selector_match_count
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
    stored = _stored_messages(run)
    return stored or ['Записей журнала пока нет.']


def _stored_messages(run: Run) -> list[str]:
    stored = []
    for item in run.events or []:
        if isinstance(item, dict) and item.get('message'):
            stored.append(str(item['message']))
        elif item:
            stored.append(str(item))
    return stored


def describe_extract(body: bytes, rules: list[dict], row_count: int) -> list[str]:
    text = body.decode('utf-8', errors='replace')
    lines = []
    for rule in rules or []:
        name = rule.get('name') or 'field'
        kind = rule.get('kind') or 'css'
        selector = rule.get('selector') or ''
        try:
            count = selector_match_count(body, rule)
        except Exception as exc:
            lines.append(f'Поле {name}: селектор не применился ({exc}).')
            continue
        repeat = 'повтор в карточках включён' if rule.get('many') else 'одно значение на страницу'
        lines.append(f'Поле {name}: {kind} {selector} — совпадений {count}, {repeat}.')
        for element_id in _ID.findall(selector):
            if element_id not in text:
                lines.append(
                    f'В сохранённой странице нет id="{element_id}". '
                    'Такой блок часто появляется только после скриптов, а скачан исходный HTML.'
                )
    if row_count:
        lines.append(f'Записано строк: {row_count}.')
    elif any('совпадений 0' not in line for line in lines if line.startswith('Поле ')):
        lines.append('Селектор нашёл текст, но строки ещё не записаны. Загрузите страницу ещё раз.')
    else:
        lines.append('Строк не записано: селектор ничего не нашёл в сохранённой странице.')
    return lines


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
        except OSError:
            lines.append('Файл снимка не прочитан.')
            continue
        if len(body) != snapshot.byte_size:
            lines.append(f'После распаковки {len(body)} байт.')
        lines.extend(describe_extract(body, rules, run.records.count()))
    if not lines:
        lines.append('Записей журнала пока нет.')
    return lines
