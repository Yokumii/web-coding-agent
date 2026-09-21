#!/usr/bin/env python3
"""Bounded online mining, reusing the single-case supervisor; no batch deadline."""
from __future__ import annotations

import argparse
import errno
import fcntl
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import sys
import threading
import traceback
from urllib.parse import urlsplit

import httpx
import openai

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.run_live_url_audit_case import supervise
from scripts.mine_live_url_capability_pool import load_sources
from inspiration_library.doc_api import ProviderResponseError


def classify_error(exc):
    """Only explicit process-wide failures stop the batch; unknown case errors stay local."""
    kind = 'sample_error'
    if isinstance(exc, ProviderResponseError):
        kind = 'global_error' if exc.fatal else ('provider_transient' if exc.retryable else 'sample_check_failed')
    elif isinstance(exc, openai.APIStatusError):
        code = getattr(exc, 'code', None)
        if exc.status_code in {401, 403} or code in {'insufficient_quota', 'billing_hard_limit_reached'}:
            kind = 'global_error'
        elif exc.status_code in {408, 409, 429} or exc.status_code >= 500:
            kind = 'provider_transient'
    elif isinstance(exc, (openai.APIConnectionError, openai.APITimeoutError)):
        kind = 'provider_transient'
    elif isinstance(exc, (httpx.TransportError, TimeoutError, ConnectionError)):
        kind = 'network_transient'
    elif isinstance(exc, (ImportError, SyntaxError, MemoryError)):
        kind = 'global_error'
    elif isinstance(exc, OSError) and exc.errno in {errno.ENOSPC, errno.ENOMEM, errno.EMFILE, errno.ENFILE}:
        kind = 'global_error'
    elif isinstance(exc, ValueError):
        kind = 'sample_check_failed'
    elif 'playwright' in type(exc).__module__ and 'net::ERR_' in str(exc):
        kind = 'network_transient'
    return dict(status=kind, error_type=type(exc).__name__)


def guarded_worker(operation, failure_path):
    try:
        return operation()
    except Exception as exc:
        failure_path.parent.mkdir(parents=True, exist_ok=True)
        failure_path.write_text(json.dumps(classify_error(exc)))
        traceback.print_exc()
        return 1


def notify(message):
    address = os.environ.get('NOTIFY_SOCKET')
    if address:
        if address.startswith('@'):
            address = '\0' + address[1:]
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(address)
            sock.sendall(message.encode())


def resources(run_dir):
    memory = dict(line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines())
    available_gb = int(memory['MemAvailable'].split()[0]) / 1024**2
    free_gb = shutil.disk_usage(run_dir).free / 1024**3
    load = os.getloadavg()[0] / (os.cpu_count() or 1)
    return dict(available_memory_gb=round(available_gb, 1), disk_free_gb=round(free_gb, 1),
                normalized_load=round(load, 2), stop=available_gb < 16 or free_gb < 50 or load > .9)


def network_failure(case):
    """Match transport exceptions, not ordinary selector or schema failures."""
    log = case / 'stdout.log'
    tail = log.read_text(errors='replace')[-12000:] if log.exists() else ''
    markers = ('net::ERR_NETWORK_CHANGED', 'net::ERR_CONNECTION_', 'net::ERR_TIMED_OUT',
               'net::ERR_NAME_NOT_RESOLVED', 'net::ERR_INTERNET_DISCONNECTED',
               'net::ERR_PROXY_CONNECTION_FAILED', 'net::ERR_TUNNEL_CONNECTION_FAILED',
               'httpx.ConnectError:', 'httpx.ReadTimeout:', 'httpx.ConnectTimeout:',
               'httpx.RemoteProtocolError:', 'openai.APIConnectionError:', 'openai.APITimeoutError:')
    if 'TimeoutError: Page.goto:' in tail:
        return 'navigation_timeout'
    return next((marker for marker in markers if marker in tail), None)


def sample_check_failure(case):
    log = case / 'stdout.log'
    tail = log.read_text(errors='replace')[-12000:] if log.exists() else ''
    # A bad model sample is not a broken worker or a provider outage.
    return (('ValueError:' in tail or 'JSONDecodeError:' in tail)
            and any(name in tail for name in ('in validate_capability_extraction',
                'in validate_live_card_evidence', 'in validate_product_pattern',
                'in load_json_response')))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--sources', type=Path, required=True)
    p.add_argument('--run-dir', type=Path, required=True)
    p.add_argument('--max-sources', type=int, required=True)
    p.add_argument('--concurrency', type=int, choices=range(1, 9), default=2)
    p.add_argument('--max-new-sources', type=int, help='Stop dispatch after this many new sources in this invocation')
    p.add_argument('--seconds', type=int, default=600)
    p.add_argument('--browser-proxy', default='http://127.0.0.1:7890')
    p.add_argument('--source-mode', choices=('reference', 'examples', 'none'), default='reference')
    p.add_argument('--exploration-rounds', type=int, choices=(0, 1, 2), default=0)
    p.add_argument('--max-paths', type=int, default=2)
    p.add_argument('--max-actions', type=int, default=3)
    p.add_argument('--key-stdin', action='store_true')
    p.add_argument('--retry-seed-id', action='append', default=[], help='Explicitly resume a diagnosed failed/interrupted source')
    args = p.parse_args()
    if args.max_sources < 1 or not 1 <= args.seconds <= 600:
        p.error('positive source limit and 1-600 second case deadline required')
    if args.max_new_sources is not None and args.max_new_sources < 1:
        p.error('max-new-sources must be positive')
    if args.key_stdin:
        os.environ['DOC_API_KEY'] = sys.stdin.readline().strip()
    if not os.environ.get('DOC_API_KEY'):
        p.error('protected DOC_API_KEY required')
    sources = load_sources(args.sources, 'url')[:args.max_sources]
    if len(sources) != args.max_sources:
        p.error('source list is smaller than requested limit')
    run = args.run_dir.resolve()
    run.mkdir(parents=True, exist_ok=True)
    lock = (run/'batch.lock').open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        p.error('another runner owns this batch')
    identity = dict(sources=sources, max_sources=args.max_sources, concurrency=args.concurrency,
                    seconds=args.seconds, base_url=os.environ.get('DOC_API_BASE_URL'),
                    model=os.environ.get('DOC_API_CHAT_MODEL'), browser_proxy=args.browser_proxy,
                    source_mode=args.source_mode, exploration_rounds=args.exploration_rounds,
                    max_paths=args.max_paths, max_actions=args.max_actions)
    identity_path = run / 'batch_identity.json'
    if identity_path.exists():
        saved_identity = json.loads(identity_path.read_text())
        if {k:v for k,v in saved_identity.items() if k != 'concurrency'} != {k:v for k,v in identity.items() if k != 'concurrency'}:
            raise ValueError('batch identity changed; use a new run directory')
    else:
        identity_path.write_text(json.dumps(identity, ensure_ascii=False, indent=2))
    results_path = run / 'results.jsonl'
    results = [json.loads(line) for line in results_path.read_text().splitlines()] if results_path.exists() else []
    # All attempted sources are skipped on resume. Failed cases require explicit diagnosis.
    latest = {r['seed_id']: r for r in results}
    retry_ids = set(args.retry_seed_id)
    if any(seed not in latest or latest[seed]['status'] == 'ok' for seed in retry_ids):
        p.error('retry IDs must name recorded unsuccessful sources')
    attempted = set(latest) - retry_ids
    pending = [(i, row) for i, row in enumerate(sources) if row['seed_id'] not in attempted]
    pending_total = len(pending)
    if args.max_new_sources is not None:
        pending = pending[:args.max_new_sources]
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda *_: stop.set())
    env = os.environ.copy()
    env['PYTHONPATH'] = os.pathsep.join(filter(None, [str(ROOT), env.get('PYTHONPATH')]))
    env['PYTHONUNBUFFERED'] = '1'
    env['DOC_API_PROXY'] = env.get('DOC_API_PROXY') or args.browser_proxy
    if urlsplit(env.get('DOC_API_BASE_URL', '')).hostname == 'api.tokenwave.us':
        env['TOKENWAVE_API_PROXY'] = args.browser_proxy
    for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
        env.pop(key, None)

    def worker(index, row):
        case = run / 'cases' / f'{index:04d}'
        if stop.is_set():
            return dict(seed_id=row['seed_id'], status='interrupted')
        # An interrupted, unrecorded attempt is never repeated automatically.
        if case.exists() and row['seed_id'] not in retry_ids:
            return dict(seed_id=row['seed_id'], status='interrupted_attempt_exists', run_dir=str(case))
        command = [sys.executable, str(Path(__file__).resolve()), '--case-worker',
                   '--mode', 'url', '--sources', str(args.sources.resolve()), '--seed-id', row['seed_id'],
                   '--run-dir', str(case/'miner'), '--stage', 'full',
                   '--exploration-rounds', str(args.exploration_rounds),
                   '--max-paths', str(args.max_paths), '--max-actions', str(args.max_actions),
                   '--api-timeout-seconds', '90',
                   '--source-mode', args.source_mode, '--max-source-regions', '4', '--product-patterns']
        if args.browser_proxy:
            command.extend(['--browser-proxy', args.browser_proxy])
        def monitor():
            state = resources(run)
            if state['stop']:
                stop.set()
            state['stop'] = stop.is_set()
            return state
        case.mkdir(parents=True, exist_ok=True)
        # Keep old artifacts and request IDs immutable, including failed API requests.
        histories = sorted(case.glob('attempt_*'))
        first_attempt = len(histories) + (1 if (case/'supervisor_result.json').exists() else 0)
        if first_attempt >= 3:
            return dict(seed_id=row['seed_id'], status='attempt_limit_reached', run_dir=str(case))
        attempt = case / f'attempt_{first_attempt+1:02d}'
        command[command.index('--run-dir')+1] = str(attempt/'miner')
        result = supervise(command, cwd=ROOT, run_dir=attempt, seconds=args.seconds, env=env, monitor=monitor)
        failure_path = attempt/'miner/worker_failure.json'
        if result['status'] == 'error':
            if failure_path.exists():
                result.update(json.loads(failure_path.read_text()))
            else:
                result['status'] = ('network_transient' if network_failure(attempt) else
                                    'sample_check_failed' if sample_check_failure(attempt) else 'sample_error')
        # Model retry is local to the failed request. Never restart browser/earlier LLM stages here.
        if result['status'] == 'provider_transient':
            result['status'] = 'provider_retry_exhausted'
        elif result['status'] == 'network_transient':
            result['status'] = 'network_unavailable'
        with (case/'attempts.jsonl').open('a') as ledger:
            ledger.write(json.dumps(dict(attempt=first_attempt+1, **result))+'\n')
            ledger.flush()
            os.fsync(ledger.fileno())
        return dict(seed_id=row['seed_id'], run_dir=str(attempt), **result)

    notify('READY=1\nWATCHDOG=1')
    active = {}
    cursor = 0
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool, results_path.open('a') as output:
        while active or (cursor < len(pending) and not stop.is_set()):
            state = resources(run)
            if state['stop']:
                stop.set()
            while not stop.is_set() and len(active) < args.concurrency and cursor < len(pending):
                index, row = pending[cursor]
                active[pool.submit(worker, index, row)] = row['seed_id']
                cursor += 1
            done, _ = wait(active, timeout=15, return_when=FIRST_COMPLETED) if active else (set(), set())
            for future in done:
                seed_id = active.pop(future)
                try:
                    result = future.result()
                except Exception as exc:
                    result = dict(seed_id=seed_id, **classify_error(exc))
                results.append(result)
                output.write(json.dumps(result) + '\n')
                output.flush()
                os.fsync(output.fileno())
                latest[seed_id] = result
                if result['status'] in ('global_error', 'resource_pressure', 'interrupted'):
                    stop.set()
            status = dict(time=datetime.now(timezone.utc).isoformat(), attempted=len(latest),
                          concurrency=args.concurrency, max_new_sources=args.max_new_sources,
                          successful=sum(r['status']=='ok' for r in latest.values()), active=list(active.values()),
                          network_paused=False, provider_cooldown_seconds=0,
                          queued=pending_total-cursor, stopped=stop.is_set(), resources=state)
            temp = run/'batch_status.tmp'
            temp.write_text(json.dumps(status, indent=2))
            temp.replace(run/'batch_status.json')
            notify('WATCHDOG=1')
    fcntl.flock(lock, fcntl.LOCK_UN)
    lock.close()
    return 1 if stop.is_set() else 0


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--case-worker':
        sys.argv.pop(1)
        from scripts.mine_live_url_capability_pool import main as mine
        failure_path = Path(sys.argv[sys.argv.index('--run-dir')+1])/'worker_failure.json'
        raise SystemExit(guarded_worker(mine, failure_path))
    raise SystemExit(main())
