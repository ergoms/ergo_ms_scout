"""Измерения SCALE-001, подключаемые только настройками изолированного стенда."""
import json
import os
from collections import defaultdict
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
from time import perf_counter_ns, time_ns

from celery.signals import after_task_publish, before_task_publish, task_postrun, task_prerun, task_received

PREFIX = 'modules.scout.api.tasks.'
TARGETS = {PREFIX + 'execute_scout_task', PREFIX + 'dispatch_scout_pending'}
sample = ContextVar('scout_scale_sample', default=None)
publish_started = {}
received_at = {}
output = None


def emit(event):
    global output
    if output is None:
        output = (Path(os.environ['SCOUT_SCALE_DIR']) / f'probe-{os.getpid()}.jsonl').open(
            'a', encoding='utf-8', buffering=1,
        )
    output.write(json.dumps(event, separators=(',', ':')) + '\n')


def before_publish(sender=None, headers=None, **_):
    if sender in TARGETS and headers:
        publish_started[headers['id']] = time_ns()


def after_publish(sender=None, headers=None, body=None, **_):
    if sender not in TARGETS or not headers:
        return
    message = headers['id']
    finished = time_ns()
    emit({'event': 'published', 'id': message, 'name': sender.rsplit('.', 1)[-1],
          'start': publish_started.pop(message, finished), 'end': finished, 'args': body[0]})


def install_publish():
    before_task_publish.connect(before_publish, weak=False)
    after_task_publish.connect(after_publish, weak=False)


def received(sender=None, request=None, **_):
    if request and request.name in TARGETS:
        received_at[request.id] = time_ns()


def db_time(data, start):
    elapsed = perf_counter_ns() - start
    data['db_total'][0] += elapsed
    data['db_total'][1] += 1
    for stage in data['stack']:
        data['db_stages'][stage][0] += elapsed
        data['db_stages'][stage][1] += 1


def sql_timer(execute, sql, params, many, context):
    data = sample.get()
    if data is None:
        return execute(sql, params, many, context)
    started = perf_counter_ns()
    try:
        return execute(sql, params, many, context)
    finally:
        db_time(data, started)


def prerun(task_id=None, task=None, args=None, **_):
    if not task or task.name not in TARGETS:
        return
    from django.db import connection
    context = connection.execute_wrapper(sql_timer)
    context.__enter__()
    sample.set({
        'context': context, 'start': time_ns(), 'received': received_at.pop(task_id, None),
        'clock': perf_counter_ns(), 'stack': [],
        'stages': defaultdict(lambda: [0, 0]),
        'db_stages': defaultdict(lambda: [0, 0]), 'db_total': [0, 0],
    })


def postrun(task_id=None, task=None, args=None, retval=None, state=None, **_):
    if not task or task.name not in TARGETS:
        return
    data = sample.get()
    if data is None:
        return
    total = perf_counter_ns() - data['clock']
    data['context'].__exit__(None, None, None)
    sample.set(None)
    emit({'event': 'worker', 'id': task_id, 'name': task.name.rsplit('.', 1)[-1],
          'args': args, 'start': data['start'], 'received': data['received'], 'total': total,
          'db': data['db_total'], 'stages': data['stages'], 'db_stages': data['db_stages'],
          'state': state, 'changed': retval.get('changed') if isinstance(retval, dict) else None})


def wrap_stage(module, attr, stage):
    original = getattr(module, attr)

    @wraps(original)
    def timed(*args, **kwargs):
        data = sample.get()
        if data is None:
            return original(*args, **kwargs)
        start = perf_counter_ns()
        data['stack'].append(stage)
        try:
            return original(*args, **kwargs)
        finally:
            data['stack'].pop()
            data['stages'][stage][0] += perf_counter_ns() - start
            data['stages'][stage][1] += 1

    setattr(module, attr, timed)


def wrap_transaction(module, attr):
    original = getattr(module, attr)

    @wraps(original)
    def timed(self, *args, **kwargs):
        data = sample.get()
        if data is None or self.vendor != 'postgresql':
            return original(self, *args, **kwargs)
        start = perf_counter_ns()
        try:
            return original(self, *args, **kwargs)
        finally:
            db_time(data, start)

    setattr(module, attr, timed)


def install_worker():
    from celery import Celery
    from django.db.backends.base.base import BaseDatabaseWrapper
    from django.db.backends.postgresql.base import DatabaseWrapper
    from modules.scout.api.services import crawl_budget, frontier, journal, tasks_exec

    task_received.connect(received, weak=False)
    task_prerun.connect(prerun, weak=False)
    task_postrun.connect(postrun, weak=False)
    wrap_transaction(BaseDatabaseWrapper, '_commit')
    wrap_transaction(BaseDatabaseWrapper, '_rollback')
    wrap_stage(DatabaseWrapper, 'connect', 'postgres_connect')
    for attr in ('_claim', '_fetch', '_crawl', '_extract', '_finish', '_settle_idle_runs',
                 'recover_expired_leases', 'fetch_public_html', 'extract_html', 'read_snapshot_bytes',
                 'store_snapshot_bytes', 'describe_extract'):
        wrap_stage(tasks_exec, attr, attr)
    wrap_stage(crawl_budget, 'reconcile', 'reconcile')
    wrap_stage(frontier, 'discover', 'discover')
    wrap_stage(frontier, 'iter_hrefs', 'iter_hrefs')
    wrap_stage(journal, '_nodes', 'journal_match')
    wrap_stage(Celery, 'send_task', 'broker_publish')
