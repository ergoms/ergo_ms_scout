"""Ответы API без числовых id базы."""

from .services.journal import journal_lines


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
        'fields': revision.fields,
        'limits': revision.limits,
        'plan_hash': revision.plan_hash,
        'approved': approved,
    }


def run_payload(run, *, preview=None) -> dict:
    data = {
        'public_id': str(run.public_id),
        'status': run.status,
        'error_summary': run.error_summary,
        'job_public_id': str(run.job_revision.job.public_id),
        'plan_hash': run.job_revision.plan_hash,
        'approved': bool(run.job_revision.approved_plan_hash)
        and run.job_revision.approved_plan_hash == run.job_revision.plan_hash,
        'snapshot_count': run.snapshots.count(),
        'record_count': run.records.count(),
        'journal': journal_lines(run),
        'tasks': [
            {
                'public_id': str(task.public_id),
                'kind': task.kind,
                'status': task.status,
                'error_code': task.error_code,
                'error_message': task.error_message,
            }
            for task in run.tasks.all()
        ],
    }
    if preview is not None:
        data['preview'] = preview
    return data


def record_payload(record) -> dict:
    return {
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
    }
