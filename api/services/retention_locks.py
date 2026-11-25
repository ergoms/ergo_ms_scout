"""Межпроцессные блокировки чтения и удаления; сбой процесса освобождает сеанс PostgreSQL."""

import hashlib
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import timedelta
from uuid import uuid4

from django.db import connection
from django.utils import timezone

from ..models import RetentionLease, Run
from .errors import ScoutError

_locks = {}
_guard = threading.Lock()
_read_leases = ContextVar('scout_retention_reads', default=())


@contextmanager
def resource_lock(resource, *, shared=False, wait=True):
    key = int.from_bytes(hashlib.sha256(('scout.retention:' + resource).encode()).digest()[:8], 'big', signed=True)
    acquired = False
    if connection.vendor == 'postgresql':
        suffix = '_shared' if shared else ''
        transactional = connection.in_atomic_block
        stem = 'advisory_xact_lock' if transactional else 'advisory_lock'
        function = ('pg_' if wait else 'pg_try_') + stem + suffix
        with connection.cursor() as cursor:
            cursor.execute(f'SELECT {function}(%s)', [key])
            acquired = True if wait else cursor.fetchone()[0]
        try:
            yield acquired
        finally:
            if acquired and not transactional:
                with connection.cursor() as cursor:
                    cursor.execute(f'SELECT pg_advisory_unlock{suffix}(%s)', [key])
    else:
        with _guard:
            lock = _locks.setdefault(key, threading.RLock())
        acquired = lock.acquire(blocking=wait)
        try:
            yield acquired
        finally:
            if acquired:
                lock.release()


@contextmanager
def read_resource(resource):
    with resource_lock(resource, shared=True):
        lease = RetentionLease.objects.create(
            token=uuid4(), resource=resource, expires_at=timezone.now() + timedelta(minutes=5),
        )
        context = _read_leases.set((*_read_leases.get(), lease))
        try:
            yield lease
        finally:
            _read_leases.reset(context)
            RetentionLease.objects.filter(pk=lease.pk).delete()


def renew_read_leases():
    now = timezone.now()
    for lease in _read_leases.get():
        if lease.expires_at <= now + timedelta(minutes=1):
            lease.expires_at = now + timedelta(minutes=5)
            RetentionLease.objects.update_or_create(
                token=lease.token, defaults={'resource': lease.resource, 'expires_at': lease.expires_at},
            )


@contextmanager
def read_results(*runs, comparison=False):
    from contextlib import ExitStack

    with ExitStack() as stack:
        for run in sorted(runs, key=lambda value: value.pk):
            stack.enter_context(read_resource(f'run:{run.pk}'))
            if Run.objects.get(pk=run.pk).results_availability != 'available':
                error = ScoutError(
                    'comparison_data_expired' if comparison else 'run_data_expired',
                    'Срок хранения результата истёк или началась его очистка',
                )
                error.details = {'run_public_id': str(run.public_id)}
                raise error
        yield


def result_response(method):
    from functools import wraps

    @wraps(method)
    def guarded(self, request, public_id):
        from django.shortcuts import get_object_or_404
        from rest_framework.response import Response
        from ..integrations import error_payload, error_status
        from .access import runs_for

        run = get_object_or_404(runs_for(request.user), public_id=public_id)
        try:
            with read_results(run):
                return method(self, request, public_id)
        except ScoutError as exc:
            return Response(error_payload(exc), status=error_status(exc))
    return guarded


def visual_response(method):
    from functools import wraps

    @wraps(method)
    def guarded(self, request, public_id=None):
        from ..models import VisualSnapshot

        uid = public_id or (request.data.get('snapshot_public_id') if isinstance(request.data, dict) else None)
        try:
            from uuid import UUID
            uid = UUID(str(uid))
        except (ValueError, TypeError):
            return method(self, request, **({'public_id': public_id} if public_id else {}))
        row = VisualSnapshot.objects.only('pk').filter(public_id=uid, user_public_id=request.user.public_id).first()
        if row is None:
            return method(self, request, **({'public_id': public_id} if public_id else {}))
        with read_resource(f'visual:{row.pk}'):
            return method(self, request, **({'public_id': public_id} if public_id else {}))
    return guarded
