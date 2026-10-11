"""Предпросмотр читает сохранённый снимок и не пишет записи результата."""

from .errors import ScoutError
from .html_extract import extract_html
from .snapshot_store import read_snapshot_bytes


def preview_rows(run) -> list[dict]:
    revision = run.job_revision
    rules = revision.fields
    record = revision.record or None
    rows = []
    for snapshot in run.snapshots.all():
        try:
            body = read_snapshot_bytes(snapshot.content_path)
        except (OSError, ValueError) as exc:
            raise ScoutError('snapshot', 'Снимок не прочитан') from exc
        for row in extract_html(
            body,
            rules,
            record=record,
            content_type=snapshot.content_type,
            charset=snapshot.charset,
        ):
            rows.append({
                'source_url': snapshot.source_url,
                'values': row['values'],
                'evidence': row['evidence'],
            })
    return rows
