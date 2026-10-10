"""Предпросмотр читает сохранённый снимок и не пишет записи результата."""

from .errors import ScoutError
from .html_extract import extract_html
from .snapshot_store import read_snapshot_bytes


def preview_rows(run) -> list[dict]:
    rules = run.job_revision.fields
    rows = []
    for snapshot in run.snapshots.all():
        try:
            body = read_snapshot_bytes(snapshot.content_path)
        except OSError as exc:
            raise ScoutError('snapshot', 'Снимок не прочитан') from exc
        for row in extract_html(body, rules):
            rows.append({
                'source_url': snapshot.source_url,
                'values': row['values'],
                'evidence': row['evidence'],
            })
    return rows
