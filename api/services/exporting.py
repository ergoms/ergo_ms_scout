"""Выгрузка завершённого сбора в media_api."""

import csv
import io
import json

from django.utils.translation import gettext as _

from src.core.utils.media_client import get_media_client
from src.core.utils.media_signing import get_signed_media_url

from ..models import ExportArtifact
from .constants import RUN_COMPLETED, RUN_COMPLETED_WITH_ERRORS
from .errors import ScoutError


def export_run(*, run, kind: str):
    if run.status not in (RUN_COMPLETED, RUN_COMPLETED_WITH_ERRORS):
        raise ScoutError('invalid_state', _('Выгрузка доступна после завершения сбора'))
    if kind not in ('json', 'csv'):
        raise ScoutError('invalid', _('Формат выгрузки: json или csv'))
    records = list(run.records.prefetch_related('evidence').order_by('created_at'))
    if kind == 'json':
        body = json.dumps(_json_rows(records), ensure_ascii=False, indent=2).encode('utf-8')
        filename = 'records.json'
    else:
        body = _csv_bytes(records)
        filename = 'records.csv'
    storage_path = f'scout/exports/{run.public_id}/{filename}'
    try:
        stored = get_media_client().save(storage_path, body)
    except Exception as exc:
        raise ScoutError('media', _('Не удалось записать файл выгрузки')) from exc
    artifact = ExportArtifact.objects.create(run=run, kind=kind, storage_path=stored)
    url = get_signed_media_url(stored, as_attachment=True)
    return artifact, url, filename


def _json_rows(records) -> list[dict]:
    rows = []
    for record in records:
        rows.append({
            'public_id': str(record.public_id),
            'values': record.values,
            'evidence': [
                {
                    'field_path': item.field_path,
                    'source_url': item.source_url,
                    'locator': item.locator,
                    'raw_value': item.raw_value,
                    'normalized_value': item.normalized_value,
                    'extractor_id': item.extractor_id,
                    'extractor_version': item.extractor_version,
                }
                for item in record.evidence.all()
            ],
        })
    return rows


def _csv_bytes(records) -> bytes:
    names = []
    for record in records:
        for name in record.values or {}:
            if name not in names:
                names.append(name)
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=['public_id', *names])
    writer.writeheader()
    for record in records:
        row = {'public_id': str(record.public_id)}
        for name in names:
            cell = (record.values or {}).get(name) or {}
            row[name] = cell.get('normalized', '')
        writer.writerow(row)
    return buffer.getvalue().encode('utf-8-sig')
