"""Ответы API без числовых id базы."""

from django.db.models import Count, Q

from .models import CrawlURL
from .services.constants import TASK_EXTRACT, TASK_FAILED, TASK_FETCH, TASK_SUCCEEDED
from .services.journal import journal_lines, stored_messages
from .services.product_identity import public_identity
from .services.value_types import legacy_values, missing_typed_fields


def job_payload(job) -> dict:
    revision = job.revisions.order_by('-revision_number').first()
    return {
        'public_id': str(job.public_id),
        'title': job.title,
        'revision': None if revision is None else revision_payload(revision),
    }


def revision_payload(revision) -> dict:
    approved = bool(revision.approved_plan_hash) and revision.approved_plan_hash == revision.plan_hash
    return {
        'public_id': str(revision.public_id),
        'revision_number': revision.revision_number,
        'seed_urls': revision.seed_urls,
        'record': revision.record or None,
        'fields': revision.fields,
        'limits': revision.limits,
        'crawl': revision.crawl or {'mode': 'pages'},
        **({'identity': revision.identity} if revision.identity else {}),
        'plan_hash': revision.plan_hash,
        **({'proxy_profile_revision_public_id': str(revision.proxy_profile_revision_public_id)}
           if revision.proxy_profile_revision_public_id else {}),
        'approved': approved,
    }


def run_payload(run, *, preview=None, page_type_preview=None, summary=False) -> dict:
    from .services.sitemap_scan import scan_payload
    data = {
        'public_id': str(run.public_id),
        'status': run.status,
        'error_summary': run.error_summary,
        'job_public_id': str(run.job_revision.job.public_id),
        'plan_hash': run.job_revision.plan_hash,
        'crawl': run.job_revision.crawl or {'mode': 'pages'},
        'approved': bool(run.job_revision.approved_plan_hash)
        and run.job_revision.approved_plan_hash == run.job_revision.plan_hash,
        'snapshot_count': run.snapshots.count(),
        'record_count': (run.retained_counts.get('record_count', 0)
                         if run.results_availability != 'available' else run.records.count()),
        'results_availability': run.results_availability,
        'stop_reason': run.stop_reason,
        'frontier': _frontier_counts(run),
    }
    if (run.job_revision.crawl or {}).get('sitemap_url') or (run.job_revision.crawl or {}).get('sitemap_from_robots'):
        data['sitemap'] = scan_payload(run)
    if summary:
        succeeded = Q(status=TASK_SUCCEEDED)
        data['progress'] = run.tasks.aggregate(
            fetched=Count('pk', filter=succeeded & Q(kind=TASK_FETCH)),
            extracted=Count('pk', filter=succeeded & Q(kind=TASK_EXTRACT)),
            errors=Count('pk', filter=Q(status=TASK_FAILED)),
        )
        return data
    data.update(
        journal=journal_lines(run),
        events=stored_messages(run),
        tasks=[
            {
                'public_id': str(task.public_id),
                'kind': task.kind,
                'status': task.status,
                'error_code': task.error_code,
                'error_message': task.error_message,
            }
            for task in run.tasks.all()
        ],
    )
    if preview is not None:
        data['preview'] = preview
    if page_type_preview is not None:
        data['page_type_preview'] = page_type_preview
    return data


def _frontier_counts(run) -> dict:
    counts = {'queued': 0, 'active': 0, 'done': 0, 'skipped': 0, 'failed': 0}
    rows = CrawlURL.objects.filter(run=run).values('state').annotate(total=Count('pk')).order_by()
    for row in rows:
        state = row['state']
        if state in counts:
            counts[state] = row['total']
    return counts


def records_with_evidence(records, *, typed: bool):
    if typed:
        return records.select_related('rules_revision').prefetch_related('evidence__snapshot')
    return records.prefetch_related('evidence__snapshot')


def record_payload(record, *, typed: bool = False) -> dict:
    identity = record.identity_state
    missing = missing_typed_fields(record.values) if typed and not identity else set()
    evidence = record.evidence.all()
    if identity:
        evidence = sorted(evidence, key=lambda item: (item.source_url, item.snapshot_id, item.observation_index, item.pk))
    return {
        'public_id': str(record.public_id),
        'values': record.values if typed else legacy_values(record.values),
        **({'identity': public_identity(record.entity_key, identity)} if identity else {}),
        'evidence': [
            {
                'field_path': item.field_path,
                'source_url': item.source_url,
                'locator': item.locator,
                'raw_value': item.raw_value,
                'normalized_value': item.normalized_value,
                'extractor_id': item.extractor_id,
                'extractor_version': item.extractor_version,
                'html_availability': item.snapshot.html_availability,
                **(_evidence_revision(item, record) if typed else {}),
                **({'selected': item.snapshot_id == record.snapshot_id
                   and item.observation_index == record.observation_index} if identity else {}),
                **({'typed': item.typed_value} if typed and identity and item.typed_value is not None else {}),
            }
            for item in evidence
            if item.field_path not in missing
        ],
    }


def _evidence_revision(item, record) -> dict:
    return {
        'snapshot_public_id': str(item.snapshot.public_id),
        'rules_revision_public_id': str(record.rules_revision.public_id),
    }
