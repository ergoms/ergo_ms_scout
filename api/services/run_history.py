"""Owner-scoped run metadata; cursor order is independent of mutable run state."""

from uuid import UUID

from django.core import signing
from django.db.models import Count, Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from ..models import Record, Task
from .access import require_user_public_id, runs_for
from .constants import RUN_COMPLETED, RUN_COMPLETED_WITH_ERRORS, TASK_FAILED
from .errors import ScoutError

_SALT = 'scout.job_run_history.v1'
_FINISHED = (RUN_COMPLETED, RUN_COMPLETED_WITH_ERRORS)


def _position(value):
    if not isinstance(value, list) or len(value) != 2 or not all(isinstance(v, str) for v in value):
        raise ValueError
    date = parse_datetime(value[0])
    if date is None or not timezone.is_aware(date):
        raise ValueError
    return date, UUID(value[1])


def _before(position, *, inclusive=False):
    date, public_id = position
    return Q(created_at__lt=date) | Q(created_at=date, **{
        'public_id__lte' if inclusive else 'public_id__lt': public_id,
    })


def list_job_runs(user, job, *, limit='50', cursor=None):
    try:
        limit = int(limit)
        if not 1 <= limit <= 100:
            raise ValueError
    except (ValueError, TypeError):
        raise ScoutError('invalid_limit', 'Размер порции — целое число от 1 до 100.') from None
    context = [str(require_user_public_id(user)), str(job.public_id)]
    rows = runs_for(user).filter(job_revision__job=job).order_by('-created_at', '-public_id')
    after = None
    if cursor is not None:
        try:
            if not isinstance(cursor, str) or not 1 <= len(cursor) <= 2048:
                raise ValueError
            token = signing.loads(cursor, salt=_SALT, max_age=86400)
            if not isinstance(token, list) or len(token) != 3 or token[0] != context:
                raise ValueError
            upper, after = _position(token[1]), _position(token[2])
            if after > upper:
                raise ValueError
        except (signing.BadSignature, ValueError, TypeError, KeyError, OverflowError):
            raise ScoutError('invalid_cursor', 'Курсор не соответствует истории этого задания.') from None
    else:
        upper = rows.values_list('created_at', 'public_id').first()
    if upper is None:
        return {'job_public_id': str(job.public_id), 'runs': [], 'next_cursor': None}
    rows = rows.filter(_before(upper, inclusive=True))
    if after:
        rows = rows.filter(_before(after))
    page = list(rows[:limit + 1])
    has_more, page = len(page) > limit, page[:limit]
    ids = [run.pk for run in page]
    counts = dict(Record.objects.filter(run_id__in=ids).values_list('run_id').annotate(total=Count('pk')))
    failures = dict(Task.objects.filter(run_id__in=ids, status=TASK_FAILED).values_list('run_id').annotate(total=Count('pk')))
    items = []
    for run in page:
        ready = run.status in _FINISHED and run.started_at is not None
        reason = ('not_ready' if not ready else
                  'data_expired' if run.results_availability != 'available' else
                  'identity_missing' if (run.job_revision.identity or {}).get('kind') not in ('sku', 'field', 'url') else None)
        items.append({
            'public_id': str(run.public_id), 'job_public_id': str(job.public_id),
            'created_at': run.created_at, 'started_at': run.started_at, 'finished_at': run.finished_at,
            'status': run.status, 'plan_hash': run.job_revision.plan_hash,
            'revision_number': run.job_revision.revision_number,
            'record_count': counts.get(run.pk, 0) if run.results_availability == 'available'
                else run.retained_counts.get('record_count', 0),
            'results_availability': run.results_availability,
            'completeness': ('complete' if run.status == RUN_COMPLETED and not run.stop_reason else 'partial')
                if ready else None,
            'stop_reason': run.stop_reason, 'failed_tasks': failures.get(run.pk, 0),
            'comparison_eligible': reason is None, 'comparison_blocked_reason': reason,
            'comparison_fields': [field['name'] for field in run.job_revision.fields if not field.get('many')],
        })
    def encode(position):
        return [position[0].isoformat(), str(position[1])]
    next_cursor = signing.dumps([context, encode(upper), encode((page[-1].created_at, page[-1].public_id))], salt=_SALT) if has_more else None
    return {'job_public_id': str(job.public_id), 'runs': items, 'next_cursor': next_cursor}
