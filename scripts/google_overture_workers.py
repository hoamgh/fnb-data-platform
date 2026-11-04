"""Offline Overture partition/persistence rehearsal. No live browser capability."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]


def write_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def load_places(catalog, limit):
    import pyarrow.parquet as pq
    if limit < 1:
        raise ValueError('limit must be positive')
    rows = pq.read_table(catalog).to_pylist()
    selected, seen = [], set()
    for row in rows:
        key = row.get('place_key')
        if not isinstance(key, str) or not key.startswith('overture:') or key in seen:
            raise ValueError('Missing or duplicate Overture identity')
        seen.add(key)
        if row.get('source') != 'overture' or not str(row.get('name') or '').strip():
            raise ValueError('Invalid Overture source/name')
        if 'food_and_drink' not in (row.get('category_hierarchy') or []):
            raise ValueError('Not an F&B place')
        for field, bound in [('lat', 90), ('lng', 180)]:
            value = row.get(field)
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or abs(value) > bound:
                raise ValueError('Invalid coordinate')
        selected.append(row)
    if len(selected) < limit:
        raise ValueError('Catalog contains fewer places than requested')
    return sorted(selected, key=lambda r: r['place_key'])[:limit]


def partition(places, workers):
    if not 1 <= workers <= len(places):
        raise ValueError('workers must be between one and the number of places')
    return [places[index::workers] for index in range(workers)]


def run_worker(plan_path):
    # This worker can only run the existing synthetic persistence validator.
    from scripts.validate_persistence_e2e import run_validation
    import psutil
    plan = json.loads(plan_path.read_text(encoding='utf-8'))
    directory = plan_path.parent
    process = psutil.Process()
    stopped = threading.Event()
    rss = [process.memory_info().rss]
    def sample():
        while not stopped.wait(0.05):
            rss.append(process.memory_info().rss)
    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    start = time.monotonic()
    cpu_start = process.cpu_times()
    results = []
    error = None
    try:
        for index, place in enumerate(plan['places']):
            started = time.monotonic()
            # Numeric directories avoid interpreting source IDs as paths.
            result = run_validation(directory / f'job-{index:03d}')
            results.append(dict(place_key=place['place_key'], fixture_store_id='synthetic-store-1',
                status='PASS', elapsed_seconds=time.monotonic()-started,
                parquet_rows=result['parquet_row_count'], pending=result['pending_outbox_rows'],
                files=result['parquet_files'], sqlite=result['state_db_path']))
            with (directory / 'progress.jsonl').open('a', encoding='utf-8') as log:
                log.write(json.dumps(results[-1]) + '\n')
    except Exception as exc:
        error = f'{type(exc).__name__}: {exc}'
    finally:
        stopped.set()
        sampler.join(timeout=1)
    cpu_end = process.cpu_times()
    write_json(directory / 'summary.json', dict(status='FAIL' if error else 'PASS', error=error,
        pid=os.getpid(), results=results, elapsed_seconds=time.monotonic()-start,
        peak_rss_bytes=max(rss+[process.memory_info().rss]),
        cpu_seconds=cpu_end.user+cpu_end.system-cpu_start.user-cpu_start.system,
        source_network=False, real_browser=False, real_source_reviews=0))
    return 1 if error else 0


def run(catalog, output, limit=10, workers=2, timeout=90):
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('timeout must be finite and positive')
    places = load_places(catalog, limit)
    shards = partition(places, workers)
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / 'plan.json', dict(mode='OFFLINE_PERSISTENCE_ONLY', workers=workers,
        catalog_sha256=hashlib.sha256(catalog.read_bytes()).hexdigest(),
        places=places, live_enabled=False, browser_profiles_created=False))
    children, logs = [], []
    start = time.monotonic()
    error = None
    try:
        for index, shard in enumerate(shards):
            directory = output / f'worker-{index+1:02d}'
            directory.mkdir()
            plan_path = directory / 'plan.json'
            write_json(plan_path, dict(places=shard))
            log = (directory / 'worker.log').open('w', encoding='utf-8')
            logs.append(log)
            child = subprocess.Popen([sys.executable, '-B', '-m', 'scripts.google_overture_workers',
                '--worker-plan', str(plan_path)], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
            children.append((child, directory))
        deadline = start + timeout
        for child, _ in children:
            child.wait(timeout=max(0.01, deadline-time.monotonic()))
    except (OSError, subprocess.TimeoutExpired) as exc:
        error = f'{type(exc).__name__}: {exc}'
    finally:
        # Exact subprocess handles created here; no PID/name-based termination.
        for child, _ in children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)
        for log in logs:
            log.close()
    reports = [json.loads((d/'summary.json').read_text(encoding='utf-8'))
        if (d/'summary.json').exists() else dict(status='FAIL', results=[]) for _, d in children]
    results = [r for report in reports for r in report['results']]
    passed = (not error and len(reports)==workers and len(results)==limit
        and all(c.returncode==0 for c, _ in children)
        and all(r['status']=='PASS' for r in reports)
        and len({r['place_key'] for r in results})==limit)
    summary = dict(status='PASS' if passed else 'FAIL', mode='OFFLINE_PERSISTENCE_ONLY',
        error=error, workers=workers, completed=len(results), elapsed_seconds=time.monotonic()-start,
        parquet_rows=sum(r['parquet_rows'] for r in results),
        parquet_files=sum(len(r['files']) for r in results), pending=sum(r['pending'] for r in results),
        worker_reports=reports, source_network=False, real_browser=False, real_source_reviews=0,
        coverage='NOT_MEASURED', live_readiness='BLOCKED_BY_ORIGINAL_HOLD')
    write_json(output / 'summary.json', summary)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalog', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--limit', type=int, default=10)
    parser.add_argument('--workers', type=int, default=2)
    parser.add_argument('--timeout-seconds', type=float, default=90)
    parser.add_argument('--worker-plan', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.worker_plan:
        return run_worker(args.worker_plan)
    if args.catalog is None or args.output is None:
        parser.error('--catalog and --output are required')
    result = run(args.catalog, args.output, args.limit, args.workers, args.timeout_seconds)
    print(json.dumps({k:v for k,v in result.items() if k!='worker_reports'}))
    return 0 if result['status']=='PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
