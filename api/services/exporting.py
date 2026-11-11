"""Выгрузка завершённого сбора в media_api."""

import csv
import json
from pathlib import Path
from uuid import uuid4
from tempfile import TemporaryDirectory
from textwrap import indent

from django.utils.translation import gettext as _

from src.core.utils.media_client import get_media_client
from src.core.utils.media_signing import get_signed_media_url

from ..models import ExportArtifact
from ..present import record_payload, records_with_evidence
from .constants import RUN_COMPLETED, RUN_COMPLETED_WITH_ERRORS
from .errors import ScoutError
from .value_types import TYPED_VALUE_FORMAT, typed_csv_columns
from .retention_locks import renew_read_leases

EXPORT_BATCH_SIZE = 200


def export_run(*, run, kind: str, typed: bool = False):
    from .retention_locks import read_results

    with read_results(run):
        return _export_run(run=run, kind=kind, typed=typed)


def _export_run(*, run, kind: str, typed: bool = False):
    if run.status not in (RUN_COMPLETED, RUN_COMPLETED_WITH_ERRORS):
        raise ScoutError('invalid_state', _('Выгрузка доступна после завершения сбора'))
    if kind not in ('json', 'csv'):
        raise ScoutError('invalid', _('Формат выгрузки: json или csv'))
    records = run.records.order_by('created_at', 'pk')
    filename = f'records.{kind}'
    artifact_id = uuid4()
    storage_dir = f'scout/exports/{run.public_id}/{artifact_id}'
    if typed:
        storage_dir = f'{storage_dir}/{TYPED_VALUE_FORMAT}'
    storage_path = f'{storage_dir}/{filename}'
    try:
        with TemporaryDirectory(prefix='scout-export-') as temporary:
            local_path = Path(temporary) / filename
            if kind == 'json':
                with local_path.open('w', encoding='utf-8', newline='') as output:
                    _write_json(records, output, typed=typed)
            else:
                with local_path.open('w', encoding='utf-8-sig', newline='') as output:
                    fields = run.job_revision.fields if typed else []
                    if any('conversion' in field for field in fields):
                        _write_typed_csv(records, fields, output)
                    else:
                        _write_csv(records, output)
            stored = get_media_client().commit_local(str(local_path), storage_path)
            byte_size = local_path.stat().st_size
    except Exception as exc:
        raise ScoutError('media', _('Не удалось записать файл выгрузки')) from exc
    artifact = ExportArtifact.objects.create(public_id=artifact_id, run=run, kind=kind,
                                            storage_path=stored, byte_size=byte_size)
    url = get_signed_media_url(stored, as_attachment=True)
    return artifact, url, filename


def _write_json(records, output, *, typed: bool) -> None:
    output.write('[')
    separator = '\n'
    for record in records_with_evidence(records, typed=typed).iterator(chunk_size=EXPORT_BATCH_SIZE):
        renew_read_leases()
        output.write(separator)
        output.write(indent(json.dumps(record_payload(record, typed=typed), ensure_ascii=False, indent=2), '  '))
        separator = ',\n'
    output.write('\n]' if separator == ',\n' else ']')


def _write_csv(records, output) -> None:
    names = {}
    for values in records.values_list('values', flat=True).iterator(chunk_size=EXPORT_BATCH_SIZE):
        renew_read_leases()
        names.update(dict.fromkeys(values or {}))
    writer = csv.writer(output)
    columns = ['field.public_id' if name == 'public_id' else name for name in names]
    writer.writerow(['public_id', *columns])
    for record in records.iterator(chunk_size=EXPORT_BATCH_SIZE):
        renew_read_leases()
        values = record.values or {}
        cells = [(values.get(name) or {}).get('normalized', '') for name in names]
        writer.writerow([str(record.public_id), *cells])


def _write_typed_csv(records, fields, output) -> None:
    writer = csv.DictWriter(output, fieldnames=typed_csv_columns(fields))
    writer.writeheader()
    for record in records.iterator(chunk_size=EXPORT_BATCH_SIZE):
        renew_read_leases()
        row = {'public_id': str(record.public_id)}
        for field in fields:
            name = field['name']
            cell = record.values.get(name) or {}
            conversion = field.get('conversion')
            if not conversion:
                row[name] = cell.get('normalized', '')
                continue
            typed = cell.get('typed') or {}
            value = typed.get('value')
            row[f'{name}__status'] = typed.get('status')
            row[f'{name}__error'] = (typed.get('error') or {}).get('code')
            if conversion['type'] == 'money':
                amount = value or {}
                row[name] = amount.get('amount')
                row[f'{name}__currency'] = amount.get('currency')
            else:
                row[name] = value
        writer.writerow(row)
