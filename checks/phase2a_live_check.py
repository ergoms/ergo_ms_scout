"""Живой прогон SCOUT через HTTP API и отдельный worker. Пароль только из окружения."""
import argparse
import json
import os
import time
from pathlib import Path

import requests

ROOT = os.environ.get('SCOUT_API_ROOT', 'http://localhost:8001/api/')
SITE = os.environ.get('SCOUT_FIXTURE_ROOT', 'http://127.0.0.1:8767')
TERMINAL = {'completed', 'completed_with_errors', 'failed', 'cancelled'}
OUTPUT = Path(__file__).resolve().parent / 'phase2a-live-results.json'
session = requests.Session()
results = json.loads(OUTPUT.read_text(encoding='utf-8')) if os.environ.get('SCOUT_APPEND') == '1' and OUTPUT.exists() else []


def api(method, path, data=None, expected=(200, 201, 202)):
    response = session.request(method, ROOT + path, json=data, timeout=25)
    if response.status_code not in expected:
        raise RuntimeError(f'{method} {path}: HTTP {response.status_code}')
    body = response.json()
    return body.get('data', body)


def login(username):
    response = session.post(ROOT + 'cms/adp/authorization/', json={
        'username': username,
        'password': os.environ['SCOUT_TEST_PASSWORD'],
        'remember_me': False,
    }, timeout=25)
    response.raise_for_status()
    body = response.json()
    token = body.get('access') or body.get('data', {}).get('access')
    if not token:
        raise RuntimeError('Login did not return access token')
    session.headers['Authorization'] = 'Bearer ' + token


def read_run(run):
    return api('GET', f'scout/runs/{run}/')


def wait_run(run, statuses, seconds=50):
    until = time.monotonic() + seconds
    state = read_run(run)
    while time.monotonic() < until:
        state = read_run(run)
        if state['status'] in statuses:
            return state
        time.sleep(0.5)
    return state


def emit(name, state, checks=None):
    records = api('GET', f'scout/runs/{state["public_id"]}/records/')['records']
    data = {
        'case': name,
        'run': state['public_id'],
        'job': state['job_public_id'],
        'status': state['status'],
        'snapshots': state['snapshot_count'],
        'records': state['record_count'],
        'frontier': state['frontier'],
        'stop_reason': state['stop_reason'],
        'tasks': state['tasks'],
        'values': [item['values'] for item in records],
        'evidence_urls': sorted({
            evidence['source_url'] for item in records for evidence in item['evidence']
        }),
        'hits': requests.get(SITE + '/stats', timeout=5).json(),
        'checks': checks or {},
    }
    results.append(data)
    OUTPUT.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({key: value for key, value in data.items() if key not in {'tasks', 'values'}}, ensure_ascii=False), flush=True)


def prepare(path, crawl=None, limits=None):
    requests.post(SITE + '/reset', timeout=5)
    body = {
        'title': 'SCOUT live ' + path,
        'seed_urls': [SITE + path],
        'fields': [{'name': 'title', 'kind': 'css', 'selector': 'h1', 'scope': 'page'}],
        'crawl': crawl or {'mode': 'site', 'allowed_hosts': ['127.0.0.1'], 'max_depth': 2, 'max_pages': 20},
    }
    if limits:
        body['limits'] = limits
    job = api('POST', 'scout/jobs/', body)
    run = api('POST', f'scout/jobs/{job["public_id"]}/explore/', {})['public_id']
    state = wait_run(run, {'awaiting_confirmation', 'failed'})
    if path == '/redirect/out' and state['status'] == 'failed':
        emit('redirect_scope_blocked', state)
        return None
    if state['status'] != 'awaiting_confirmation':
        emit('preview failed ' + path, state)
        raise RuntimeError('Preview failed or timed out')
    return run


def start(run):
    api('POST', f'scout/runs/{run}/approve/', {})
    return api('POST', f'scout/runs/{run}/start/', {})


def clean(run):
    if read_run(run)['status'] not in TERMINAL:
        requests.post(SITE + '/release', timeout=5)
        api('POST', f'scout/runs/{run}/cancel/', {})
        wait_run(run, TERMINAL)


def crawl_case(name, path, config=None):
    run = prepare(path, config)
    if run is None:
        return
    try:
        response = session.post(ROOT + f'scout/runs/{run}/start/', json={}, timeout=25)
        before = read_run(run)
        start(run)
        state = wait_run(run, TERMINAL)
        emit(name, state, {
            'start_without_approval': response.status_code,
            'preview_fetch_only': all(task['kind'] == 'fetch' for task in before['tasks']),
        })
    finally:
        clean(run)


def pause_case(cancel=False):
    run = prepare('/slow/start')
    try:
        start(run)
        until = time.monotonic() + 15
        while time.monotonic() < until:
            if requests.get(SITE + '/stats', timeout=5).json().get('/slow/child'):
                break
            time.sleep(0.3)
        action = 'cancel' if cancel else 'pause'
        first = api('POST', f'scout/runs/{run}/{action}/', {})
        requests.post(SITE + '/release', timeout=5)
        paused = wait_run(run, {'cancelled' if cancel else 'paused'})
        if cancel:
            emit('cancel_inflight', paused, {'transition': first['status']})
        else:
            api('POST', f'scout/runs/{run}/resume/', {})
            state = wait_run(run, TERMINAL, seconds=8)
            emit('pause_inflight_resume', state, {'transition': first['status'], 'paused': paused['status']})
    finally:
        clean(run)


def expect_hits(name, expected):
    stats = requests.get(SITE + '/stats', timeout=5).json()
    mismatch = {
        key: {'expected': expected[key], 'actual': stats.get(key, 0)}
        for key in expected
        if stats.get(key, 0) != expected[key]
    }
    if mismatch:
        raise RuntimeError(name + ' ' + json.dumps(mismatch, ensure_ascii=False))


def cyrillic_plan():
    fields = [{'name': 'title', 'kind': 'css', 'selector': 'h1', 'scope': 'page'}]
    seen = []
    for seed in ('https://пример.рф/path', 'https://xn--e1afmkfd.xn--p1ai/path'):
        job = api('POST', 'scout/jobs/', {
            'title': 'idn',
            'seed_urls': [seed],
            'fields': fields,
            'crawl': {'mode': 'site'},
        })
        seen.append(job['revision']['crawl']['allowed_hosts'])
    if seen[0] != ['xn--e1afmkfd.xn--p1ai'] or seen[0] != seen[1]:
        raise RuntimeError('idn hosts differ')
    print('cyrillic_plan ok', flush=True)


def other_user_does_not_see_the_job():
    job = api('POST', 'scout/jobs/', {
        'title': 'private',
        'seed_urls': ['https://example.com/private'],
        'fields': [{'name': 'title', 'kind': 'css', 'selector': 'h1', 'scope': 'page'}],
    })
    other = requests.Session()
    response = other.post(ROOT + 'cms/adp/authorization/', json={
        'username': 'scout-live-b',
        'password': os.environ['SCOUT_TEST_PASSWORD'],
        'remember_me': False,
    }, timeout=25)
    response.raise_for_status()
    body = response.json()
    token = body.get('access') or body.get('data', {}).get('access')
    other.headers['Authorization'] = 'Bearer ' + token
    seen = other.get(ROOT + f'scout/jobs/{job["public_id"]}/', timeout=25)
    if seen.status_code != 404:
        raise RuntimeError(f'other user job HTTP {seen.status_code}')
    print('other_user 404', flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--case', default='all')
    args = parser.parse_args()
    login(os.environ.get('SCOUT_TEST_USER', 'scout-live-a'))
    site = {'mode': 'site', 'allowed_hosts': ['127.0.0.1']}
    if args.case in ('all', 'cycle'):
        crawl_case('cycle_duplicate', '/cycle/a')
    if args.case in ('all', 'base'):
        crawl_case('base_query', '/base')
    if args.case in ('all', 'storm'):
        crawl_case('frontier_cap', '/storm', {**site, 'frontier_cap': 3})
    if args.case in ('all', 'depth'):
        crawl_case('depth', '/depth/0', {**site, 'max_depth': 1})
    if args.case in ('all', 'pages'):
        crawl_case('max_pages', '/wide', {**site, 'max_pages': 3})
    if args.case in ('all', 'bytes'):
        crawl_case('max_bytes', '/wide', {**site, 'max_total_bytes': 500, 'max_pages': 30})
    if args.case in ('all', 'time'):
        crawl_case('max_seconds', '/slow/start', {**site, 'max_seconds': 1})
    if args.case in ('all', 'catalog'):
        crawl_case('catalog_detail_preview', '/catalog', {
            **site,
            'preview_urls': [SITE + '/details/1'],
            'extract_on': [{'kind': 'prefix', 'pattern': '/details/'}],
        })
    if args.case in ('all', 'redirect'):
        crawl_case('redirect_scope', '/redirect/out')
    if args.case in ('all', 'redirect-depth'):
        crawl_case('redirect_depth', '/hop', {**site, 'max_depth': 1, 'max_pages': 10})
        expect_hits('redirect_depth', {'/next': 0, '/land': 1})
    if args.case in ('all', 'redirect-chain'):
        crawl_case('redirect_chain', '/chain-start', {**site, 'max_depth': 1, 'max_pages': 10})
        expect_hits('redirect_chain', {'/chain-next': 0, '/chain-land': 1})
    if args.case in ('all', 'shorter'):
        crawl_case('shorter_path', '/diamond', {**site, 'max_depth': 3, 'max_pages': 20})
        expect_hits('shorter_path', {'/leaf': 1, '/target': 1, '/long': 1, '/short': 1, '/dmid': 1})
    if args.case in ('all', 'idn'):
        cyrillic_plan()
    if args.case in ('all', 'access'):
        other_user_does_not_see_the_job()
    if args.case in ('all', 'retry'):
        for code in (503, 429, 'timeout'):
            crawl_case('retry_' + str(code), '/retry/' + str(code), {'mode': 'pages'})
    if args.case in ('all', 'pause'):
        pause_case()
    if args.case in ('all', 'cancel'):
        pause_case(cancel=True)


if __name__ == '__main__':
    main()
