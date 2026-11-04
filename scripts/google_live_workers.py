"""Bounded Windows Google DOM benchmark; run manually with --operator-submit.

Reuses the standalone crawler and writer. Historical incidents are never edited
or replayed. Session failures stop their worker; explicit shared latches stop all.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager, ExitStack
from dataclasses import asdict
import json
import hashlib
import math
import os
import re
from pathlib import Path
import sqlite3
import socket
import sys
import time
import uuid
import base64
import faulthandler
from datetime import datetime, timezone

from scripts.google_overture_workers import ROOT, load_places, write_json

RESTRICTED = {'AUTH_REQUIRED', 'CHALLENGE', 'RATE_LIMITED',
    'DEFERRED_UNKNOWN', 'REVIEW_SURFACE_UNAVAILABLE', 'NAVIGATION_FAILED'}

def json_default(value):
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {
            "__type__": "binary",
            "encoding": "base64",
            "data": base64.b64encode(bytes(value)).decode("ascii")
        }

    raise TypeError(
        f"Object of type {type(value).__name__} is not JSON serializable"
    )
def json_ready_place(place):
    """Validate and copy a catalog record without altering its source values."""
    return json.loads(json.dumps(place, ensure_ascii=False, default=json_default))


def create_queue(path, places):
    """Fresh run-local dispatch metadata only; source databases stay separate."""
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE dispatch (id INTEGER PRIMARY KEY, place_key TEXT UNIQUE NOT NULL, '
                   'payload TEXT NOT NULL, state TEXT NOT NULL, owner TEXT, result TEXT)')
        db.executemany('INSERT INTO dispatch VALUES (?, ?, ?, ?, NULL, NULL)',
            [(i, p['place_key'], json.dumps(json_ready_place(p), ensure_ascii=False), 'PENDING')
             for i, p in enumerate(places)])


def claim_next(path, owner):
    # Never reclaim RUNNING work after a crash: its browser outcome is unknown.
    with sqlite3.connect(path.as_uri()+'?mode=rw', uri=True, timeout=30) as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute("SELECT id,payload FROM dispatch WHERE state='PENDING' ORDER BY id LIMIT 1").fetchone()
        if row is None:
            return None
        db.execute("UPDATE dispatch SET state='RUNNING',owner=? WHERE id=? AND state='PENDING'",
                   (owner, row[0]))
        return row[0], json.loads(row[1])


def crawl_outcome(result):
    if result.get('status') in {'COMPLETE', 'NO_REVIEWS'} and result.get('coverage') == 'FULL':
        return 'SUCCESS'
    if result.get('status') in {'PARTIAL_LIMIT', 'PARTIAL_TIMEOUT'} and result.get('coverage') == 'PARTIAL':
        return 'PARTIAL'
    return 'ERROR'


def finish_dispatch(path, index, owner, result):
    result = dict(result, outcome=crawl_outcome(result))
    with sqlite3.connect(path.as_uri()+'?mode=rw', uri=True, timeout=30) as db:
        changed = db.execute("UPDATE dispatch SET state='FINISHED',result=? "
            "WHERE id=? AND state='RUNNING' AND owner=?",
            (json.dumps(result, ensure_ascii=False), index, owner)).rowcount
        if changed != 1:
            raise RuntimeError('Queue ownership mismatch; refusing completion')


def queued_places(plan, directory, stop):
    if plan.get('queue_path'):
        path = Path(plan['queue_path']).resolve()
        if path != directory.parent/'dispatch.sqlite3':
            raise ValueError('Queue must belong to this benchmark output')
        while not stop.exists():
            task = claim_next(path, directory.name)
            if task is None:
                return
            yield task
    else:
        yield from enumerate(plan['places'])  # Existing fixture/legacy plans.


def validate_profile(profile, workers):
    """Only the dedicated operator-created profile; never personal Chrome data."""
    profile = Path(profile).resolve()
    if workers != 1:
        raise ValueError('A logged-in profile requires --workers 1')
    if (profile.parent != (ROOT/'output').resolve() or
            not re.fullmatch(r'google-crawler-login(?:-[0-9]+)?',profile.name)):
        raise ValueError('Only dedicated output/google-crawler-login[-NN] profiles are allowed')
    if not (profile/'Local State').is_file() or not (profile/'Default'/'Preferences').is_file():
        raise ValueError('Dedicated profile is missing; manual setup is required')
    return profile


def assert_profile_available(profile):
    import psutil
    expected = os.path.normcase(str(profile.resolve()))
    for process in psutil.process_iter(['name']):
        if (process.info['name'] or '').casefold() not in {'chrome.exe', 'chrome'}:
            continue
        try:
            args = process.cmdline()
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied:
            raise RuntimeError('Cannot inspect Chrome profile use; refusing shared access') from None
        for index, arg in enumerate(args):
            if arg.startswith('--user-data-dir='):
                value = arg.split('=', 1)[1].strip('"')
            elif arg=='--user-data-dir' and index+1<len(args):
                value = args[index+1].strip('"')
            else:
                continue
            if os.path.normcase(str(Path(value).resolve())) == expected:
                raise RuntimeError('Dedicated profile is still open in Chrome; close its windows first')


@contextmanager
def reserve_profile(profile, workers):
    if profile is None:
        yield None
        return
    import fasteners
    profile = validate_profile(profile, workers)
    lock = fasteners.InterProcessLock(str(profile.with_name(profile.name+'.crawler.lock')))
    if not lock.acquire(blocking=False):
        raise RuntimeError('Dedicated profile is reserved by another crawler')
    try:
        assert_profile_available(profile)
        yield profile
    finally:
        lock.release()


def restricted(row):
    hard_states={'CHALLENGE', 'AUTH_REQUIRED', 'RATE_LIMITED'}
    if (row.get('security_restriction', False)
            or row.get('final_access_state') in hard_states
            or row.get('access_state_after_place_resolution') in hard_states):
        return True
    if (row.get('local_missing_review_tab') is True
            and row.get('status') == 'REVIEW_SURFACE_UNAVAILABLE'
            and row.get('failure_reason_code') == 'REVIEW_SURFACE_NOT_FOUND'):
        return False
    if (row.get('local_navigation_failure') in {'PAGE_LOAD_TIMEOUT', 'DRIVER_SESSION_ENDED'}
            and row.get('status') == 'NAVIGATION_FAILED'):
        return False
    if row.get('local_store_failure') and row.get('status') in {
            'SORT_FAILED', 'PLACE_RESOLUTION_FAILED', 'NAVIGATION_FAILED', 'REVIEW_SURFACE_UNAVAILABLE',
            'EXTRACTION_DEGRADED', 'PAGINATION_STALLED'}:
        return False
    if row.get('status') in RESTRICTED:
        return True
    if row.get('status')=='SORT_FAILED' and row.get('failure_reason_code')=='SORT_MENU_NOT_OPENED':
        # ReviewSurface UNKNOWN can mean "sort control not identified".
        # Retain that observation, but do not relabel a UI failure as a source
        # ban or an UNKNOWN browser-command outcome. Existing DLQ retains it.
        return False
    return (
        row.get('final_access_state') in {'UNKNOWN', 'CHALLENGE', 'AUTH_REQUIRED', 'RATE_LIMITED'}
        or row.get('access_state_after_place_resolution') in {'UNKNOWN', 'CHALLENGE', 'AUTH_REQUIRED', 'RATE_LIMITED'})


def stop_scope(row):
    """Stop the affected worker; only an explicit shared latch stops siblings."""
    if row.get('source_restriction_active') or row.get('shared_ownership_failure'):
        return 'SOURCE'
    if (row.get('security_restriction') or row.get('status') in {'AUTH_REQUIRED','CHALLENGE','RATE_LIMITED'}
            or any(row.get(key) in {'AUTH_REQUIRED','CHALLENGE','RATE_LIMITED'}
                   for key in ('final_access_state','access_state_after_place_resolution'))):
        return 'WORKER'
    if row.get('status') in {'ERROR','SESSION_FAILED','FAILED_BROWSER','FAILED_RUNTIME','PERSISTING'}:
        return 'WORKER'
    return 'WORKER' if restricted(row) else None


def live_policies():
    """Use existing retry counters and DLQ, with a scoped LIMITED retry rule."""
    from crawl_experiment.core.errors import LimitedReviewViewError
    from crawl_experiment.orchestration.retry_policy import RetryPolicy
    from crawl_experiment.orchestration.access_policy import AccessPolicy, AccessDecision

    class Retry(RetryPolicy):
        security_restriction = False
        local_store_failure = False
        local_missing_review_tab = False
        local_review_tab_click_intercepted = False
        local_navigation_failure = None

        def decide(self, error, attempt=0):
            from crawl_experiment.core.errors import ReviewSurfaceError, NavigationError, PlaceResolutionError, SortError, ReviewParseError, PaginationStalledError
            from crawl_experiment.core.statuses import CrawlStatus
            from crawl_experiment.orchestration.retry_policy import RetryDecision, RetryAction
            text = str(error).casefold()
            self.security_restriction = any(term in text for term in (
                'sign in to see all reviews', 'đăng nhập để xem tất cả',
                'login required', 'captcha', 'http 403', 'http 429'))
            self.simple_limited = isinstance(error, LimitedReviewViewError) and not self.security_restriction
            self.local_missing_review_tab = (type(error) is ReviewSurfaceError
                and getattr(error, 'local_missing_review_tab', False) is True
                and not self.security_restriction)
            self.local_review_tab_click_intercepted = (type(error) is ReviewSurfaceError
                and getattr(error, 'local_review_tab_click_intercepted', False) is True
                and not self.security_restriction)
            kind = getattr(error, 'readonly_navigation_failure', None)
            self.local_navigation_failure = (kind if type(error) is NavigationError
                and kind in {'PAGE_LOAD_TIMEOUT','DRIVER_SESSION_ENDED'} and not self.security_restriction else None)
            self.local_store_failure = (not self.security_restriction and (
                self.local_missing_review_tab or self.local_review_tab_click_intercepted or isinstance(error, (SortError, PlaceResolutionError, ReviewParseError, PaginationStalledError))
                or self.local_navigation_failure == 'PAGE_LOAD_TIMEOUT'))
            if isinstance(error, PlaceResolutionError) and not self.security_restriction:
                return RetryDecision(RetryAction.STOP, CrawlStatus.PLACE_RESOLUTION_FAILED,
                                     reason='place_resolution_failed')
            if self.local_navigation_failure:
                return RetryDecision(RetryAction.STOP, CrawlStatus.NAVIGATION_FAILED,
                                     reason=self.local_navigation_failure)
            if self.local_review_tab_click_intercepted:
                return RetryDecision(RetryAction.STOP, CrawlStatus.REVIEW_SURFACE_UNAVAILABLE,
                                     reason='review_tab_click_intercepted')
            if self.local_missing_review_tab:
                return RetryDecision(RetryAction.STOP, CrawlStatus.REVIEW_SURFACE_UNAVAILABLE,
                                     reason='place_missing_review_tab')
            return super().decide(error, attempt)

    retry = Retry(max_session_retries=2, max_navigation_retries=0, max_surface_retries=0)

    class Access(AccessPolicy):
        def failure(self, place_id, state, counts):
            if state == 'BROWSER_ERROR' and retry.local_navigation_failure:
                return AccessDecision('BROWSER_ERROR', retry.local_navigation_failure)
            if state == 'UNKNOWN' and retry.local_review_tab_click_intercepted:
                return AccessDecision('UNKNOWN', 'PLACE_REVIEW_TAB_CLICK_INTERCEPTED')
            if state == 'UNKNOWN' and retry.local_missing_review_tab:
                # Missing UI is a job-local terminal failure, not source access
                # authority or permission to clear an existing restriction.
                return AccessDecision('UNKNOWN', 'PLACE_MISSING_REVIEW_TAB')
            if state=='LIMITED' and getattr(retry, 'simple_limited', False):
                if counts.get('LIMITED', 0) < retry.max_session_retries:
                    return AccessDecision('LIMITED', 'RETRY_LIMITED_FRESH', retry_fresh=True)
                # Persist the final deferral/cooldown, then use the existing
                # terminal-failure/DLQ callback instead of returning early.
                super().failure(place_id, state, counts)
                return AccessDecision('LIMITED', 'LIMITED_RETRY_EXHAUSTED')
            return super().failure(place_id, state, counts)

    return retry, Access


def stop_source(path, row):
    # First writer wins; the file is a stop signal, not a restriction release.
    try:
        with path.open('x', encoding='utf-8') as stream:
            json.dump(dict(reason='NEW_SOURCE_ACCESS_RESTRICTION', scope='SOURCE', place_key=row.get('id'),
                status=row.get('status'), access_state=row.get('final_access_state')), stream)
    except FileExistsError:
        pass


def store_for(place):
    from crawl_experiment.core.models import Store
    address = str(place.get('address') or '').strip()
    if not address:
        raise ValueError('Live matching requires a source address')
    # Overture IDs are retained; source-provided names/addresses are not asserted
    # to be verified Google identities. Existing navigator performs matching.
    query = ' '.join([place['name'], address, str(place.get('admin_area_name') or '')])
    return Store('overture-' + uuid.uuid5(uuid.NAMESPACE_URL, place['place_key']).hex,
        place['name'], query, expected_address=address)


def reconcile(database, lake):
    import pyarrow.parquet as pq
    if not database.exists():
        raise RuntimeError('Source database missing')
    with sqlite3.connect(database.as_uri()+'?mode=ro', uri=True) as db:
        records = db.execute('SELECT COUNT(*) FROM review_state').fetchone()[0]
        pending = db.execute('SELECT COUNT(*) FROM pending_review_events').fetchone()[0]
    files, ids, rows = sorted(lake.rglob('*.parquet')), [], 0
    for file in files:
        reader = pq.ParquetFile(file)
        table = reader.read()  # Full read, not just a footer count.
        footer = json.loads(reader.metadata.metadata[b'review_event_ids'])
        if len(footer) != table.num_rows:
            raise RuntimeError('Parquet event membership/count mismatch')
        ids.extend(footer)
        rows += table.num_rows
    if len(set(ids)) != len(ids):
        raise RuntimeError('Duplicate published event membership')
    return dict(sqlite_records=records, pending=pending, parquet_rows=rows,
        parquet_files=len(files), duplicate_events=0, parquet_bytes=sum(f.stat().st_size for f in files))


def classify_worker_error(error):
    if error.startswith('TypeError:'):
        return 'INTEGRATION_ERROR'
    if error.startswith('ValueError:'):
        return 'CONFIGURATION_ERROR'
    return 'RUNTIME_ERROR'


@contextmanager
def driver_startup_diagnostics(directory, profile, stack_delay=90):
    """One-shot stack dump and flushed, metadata-only startup events."""
    started = time.monotonic()
    events = stack = None
    armed = False
    try:
        events = (directory/'driver-startup.jsonl').open('a', encoding='utf-8')
        stack = (directory/'driver-startup-stacks.log').open('ab', buffering=0)
        faulthandler.dump_traceback_later(stack_delay, repeat=False, file=stack, exit=False)
        armed = True
    except (OSError, RuntimeError):
        pass  # Diagnostic availability must not change browser startup.

    def emit(event):
        if events is not None:
            try:
                events.write(json.dumps(dict(event=event,
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    worker_id=directory.name, pid=os.getpid(),
                    elapsed_ms=round((time.monotonic()-started)*1000, 3),
                    profile_id=hashlib.sha256(str(profile.resolve()).encode()).hexdigest()[:16]))+'\n')
                events.flush()
            except OSError:
                pass
    try:
        yield emit
    finally:
        if armed:
            faulthandler.cancel_dump_traceback_later()
        if stack is not None:
            stack.close()
        if events is not None:
            events.close()


def choose_debug_port(directory):
    import psutil
    # Observe listeners without attaching to or stopping another browser.
    listeners = sorted({c.laddr.port for c in psutil.net_connections(kind='tcp')
                        if c.laddr and c.status == psutil.CONN_LISTEN})
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            reservation.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        reservation.bind(('127.0.0.1', 0))
        selected = reservation.getsockname()[1]
        write_json(directory/'driver-network.json', dict(
            timestamp=datetime.now(timezone.utc).isoformat(), worker_id=directory.name,
            pid=os.getpid(), existing_listening_ports=listeners, selected_debug_port=selected,
            semantics='OS-selected free port; released immediately before Chrome startup under startup lock'))
    return selected


def start_diagnosed_driver(Driver, lock, profile, directory, options, startups, *, startup_debug=False):
    with driver_startup_diagnostics(directory, profile) as emit:
        emit('DRIVER_LOCK_WAIT')
        if not lock.acquire(timeout=60):
            raise RuntimeError('Driver startup lock timeout')
        emit('DRIVER_LOCK_ACQUIRED')
        started = time.monotonic()
        try:
            if (directory.parent/'SOURCE_STOP.json').exists():
                raise RuntimeError('Source stop signal active')
            debug_port = choose_debug_port(directory)
            options = dict(options)
            extra_args = options.get('chromium_arg')
            port_arg = f'--remote-debugging-port={debug_port}'
            if extra_args and 'remote-debugging-port=' in str(extra_args):
                raise ValueError('Explicit debug port conflicts with run-owned port selection')
            if isinstance(extra_args, (list, tuple)):
                options['chromium_arg'] = [*extra_args, port_arg]
            else:
                options['chromium_arg'] = f'{extra_args},{port_arg}' if extra_args else port_arg
            emit('DRIVER_CALL_START')
            # Selenium's verbose service output uses this worker's captured stderr.
            # Keep raw diagnostics local; never copy them into progress events.
            diagnostic_env = ({'SE_DEBUG': '1',
                               'CHROME_LOG_FILE': str((directory/'chrome.log').resolve())}
                              if startup_debug else {})
            previous_env = {key: os.environ.get(key) for key in diagnostic_env}
            try:
                os.environ.update(diagnostic_env)
                driver = Driver(**options, user_data_dir=str(profile))
            finally:
                for key, previous in previous_env.items():
                    if previous is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = previous
            emit('DRIVER_CALL_RETURN')
            try:
                emit('DRIVER_TIMEOUT_CONFIG_START')
                driver.set_page_load_timeout(30)
                driver.set_script_timeout(15)
                emit('DRIVER_TIMEOUT_CONFIG_END')
            except BaseException:
                driver.quit()
                raise
            startups.append(dict(profile=str(profile), elapsed_seconds=time.monotonic()-started))
            return driver
        finally:
            lock.release()
            emit('DRIVER_LOCK_RELEASED')


def crawl_shard(plan, directory, *, factory_builder=None, crawl_runner=None):
    # Crawlee caches asyncio-bound storage clients across runs in a process.
    # Reuse one loop for all places instead of asyncio.run once per place.
    import asyncio
    with asyncio.Runner() as async_runner, ExitStack() as session_cleanup:
        return _crawl_shard(plan, directory, factory_builder=factory_builder,
            crawl_runner=crawl_runner, async_runner=async_runner, session_cleanup=session_cleanup)


def _crawl_shard(plan, directory, *, factory_builder=None, crawl_runner=None, async_runner=None,
                 session_cleanup=None):
    from crawl_experiment.browser.browser_factory import BrowserFactory
    from crawl_experiment.browser.session_manager import BrowserSessionManager
    from crawl_experiment.runners.full_crawl_runner import run_full_crawl
    from crawl_experiment.orchestration.access_policy import AccessPolicyConfig
    directory = directory.resolve()
    worker_started = time.monotonic()
    stop = directory.parent/'SOURCE_STOP.json'
    rows, startups = [], []
    worker_browser = None
    worker_stop_reason = None
    progress = directory/'progress.jsonl'
    if factory_builder is None:
        verify_native_membership(plan)

    def driver_builder(**options):
        if stop.exists():
            raise RuntimeError('Source stop signal active; browser creation refused')
        from seleniumbase import Driver
        import fasteners
        if plan.get('profile_dir'):
            profile = validate_profile(plan['profile_dir'], 1)
            assert_profile_available(profile)
        else:
            profile = directory/'profiles'/uuid.uuid4().hex
            profile.mkdir(parents=True, exist_ok=False)
        # Serialize only shared driver initialization, not collection.
        lock = fasteners.InterProcessLock(str(directory.parent/'driver-start.lock'))
        return start_diagnosed_driver(Driver, lock, profile, directory, options, startups,
                                      startup_debug=plan.get('startup_debug', False))

    for index, place in queued_places(plan, directory, stop):
        if stop.exists():
            break
        store = store_for(place)
        job = directory/f'job-{index:02d}'
        job.mkdir(exist_ok=False)
        write_json(job/'input.json', dict(overture=place, store=asdict(store)))
        database, lake = job/'reviews.sqlite3', job/'lake'
        started = time.monotonic()
        startup_offset = len(startups)
        row = dict(id=store.id, place_key=place['place_key'], status='ERROR', coverage='UNKNOWN')
        try:
            retry_policy, access_factory = live_policies()
            if worker_browser is None:
                factory = (factory_builder(job) if factory_builder else
                    BrowserFactory(driver_builder=driver_builder, headless=False))
                worker_browser = BrowserSessionManager(factory, worker_id=directory.name)
                session_cleanup.callback(worker_browser.close)
            factory = worker_browser.browser_factory
            code = (crawl_runner or run_full_crawl)((store,), output_root=job/'crawl',
                state_database=database, parquet_root=lake,
                timeout_seconds=plan['per_store_seconds'], max_scrolls=plan['max_scrolls'],
                concurrency=1, browser_factory=factory, validation_mode=True,
                retry_policy=retry_policy, access_policy_factory=access_factory,
                async_runner=async_runner,
                browser_session_manager=worker_browser,
                review_source=plan.get('review_source','dom'),
                access_policy_config=AccessPolicyConfig(max_browser_retries=0, max_runtime_retries=0),
                runner_name='scripts.google_live_workers')
            summaries = list((job/'crawl').glob('*/summary.json'))
            if len(summaries) != 1:
                raise RuntimeError('Missing or ambiguous crawler summary')
            source = json.loads(summaries[0].read_text(encoding='utf-8'))
            row.update(source['stores'][0])
            row['security_restriction'] = retry_policy.security_restriction
            row['local_store_failure'] = retry_policy.local_store_failure
            row['local_review_tab_click_intercepted'] = retry_policy.local_review_tab_click_intercepted
            row['local_missing_review_tab'] = retry_policy.local_missing_review_tab
            row['local_navigation_failure'] = retry_policy.local_navigation_failure
            row['crawler_exit_code'] = code
            row['runner_error'] = source.get('runner_error')
            if row['runner_error']:
                row.update(status='ERROR', coverage='UNKNOWN', final_access_state='UNKNOWN',
                           error=row['runner_error'], error_category=classify_worker_error(row['runner_error']))
            row['reconciliation'] = reconcile(database, lake)
            row['summary_path'] = str(summaries[0])
            row['browser_lifecycle_scope'] = 'worker-cumulative; final totals in worker summary'
            row['review_source'] = plan.get('review_source','dom')
            metrics_file=summaries[0].parent/'network_payload_metrics.json'
            if metrics_file.exists():
                row['network_payload_metrics']=json.loads(metrics_file.read_text(encoding='utf-8')).get(store.id)
            row['dlq_path'] = source.get('artifacts', {}).get('dlq')
            row['coverage'] = ('FULL' if row['status'] in {'COMPLETE', 'NO_REVIEWS'}
                and not code and row['reconciliation']['pending']==0 else
                'PARTIAL' if row['status'] in {'PARTIAL_TIMEOUT', 'PARTIAL_LIMIT'} else 'UNKNOWN')
            if row['reconciliation']['pending']:
                row['status'], row['coverage'] = 'PERSISTING', 'UNKNOWN'
        except Exception as exc:
            detail = f'{type(exc).__name__}: {exc}'
            if row.get('runner_error'):
                row['secondary_error'] = detail
            else:
                row['error'] = detail
                row['error_category'] = classify_worker_error(detail)
            row.update(status='ERROR', coverage='UNKNOWN')
            # Unclassified failure is not permission to start another source session.
            row['final_access_state'] = 'UNKNOWN'
        if row.get('local_navigation_failure') == 'DRIVER_SESSION_ENDED' and worker_browser is not None:
            # Abandon this failed read-only navigation, never replay it. The
            # next different place can acquire a new owned session.
            worker_browser.retire('place_navigation_transport_failure', store_id=store.id)
        row['outcome'] = crawl_outcome(row)
        scope = stop_scope(row)
        row['elapsed_seconds'] = time.monotonic()-started
        row['disposition'] = ('SOURCE_STOP' if scope=='SOURCE' else
                              'WORKER_STOP' if scope=='WORKER' else
                              'DLQ' if row['coverage']=='UNKNOWN' else row['coverage'])
        row['browser_startup_seconds'] = sum(s['elapsed_seconds'] for s in startups[startup_offset:])
        rows.append(row)
        with progress.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(row, ensure_ascii=False)+'\n')
        write_json(directory/'summary.json', dict(results=rows, startups=startups))
        print(f"{place['name']}: {row['status']} coverage={row['coverage']}", flush=True)
        if scope == 'SOURCE':
            stop_source(stop, row)
        elif scope == 'WORKER':
            worker_stop_reason = ('ACCESS_GATE_NO_RETRY' if row.get('security_restriction')
                or row.get('status') in {'AUTH_REQUIRED','CHALLENGE','RATE_LIMITED'}
                else 'AMBIGUOUS_SESSION_NO_REPLAY')
            write_json(directory/'WORKER_STOP.json', dict(reason=worker_stop_reason,
                scope=scope,place_key=place['place_key'],status=row['status']))
        if plan.get('queue_path'):
            finish_dispatch(Path(plan['queue_path']).resolve(), index, directory.name, row)
        if scope:
            break
    shutdown_started = time.monotonic()
    if worker_browser is not None:
        worker_browser.close()
    result = dict(results=rows, startups=startups, assigned=len(rows) if plan.get('queue_path') else len(plan['places']),
        browser_lifecycle=worker_browser.summary() if worker_browser else {},
        browser_shutdown_seconds=time.monotonic()-shutdown_started,
        worker_stop_reason=worker_stop_reason,
        worker_elapsed_seconds=time.monotonic()-worker_started,
        source_stopped=stop.exists(), real_browser=factory_builder is None)
    write_json(directory/'summary.json', result)
    return result


def verify_native_membership(plan):
    import ctypes as c
    from ctypes import wintypes as w
    from crawl_experiment.browser.windows_worker import WindowsAPI
    name = plan.get('native_job_name')
    if not isinstance(name, str) or not name.startswith('google-benchmark-'):
        raise RuntimeError('Missing exact native Job Object scope')
    api = WindowsAPI()
    api.k.OpenJobObjectW.argtypes = [w.DWORD, w.BOOL, w.LPCWSTR]
    api.k.OpenJobObjectW.restype = w.HANDLE
    job = api.k.OpenJobObjectW(4, False, name)
    api.require(job)
    try:
        api.member(c.c_void_p(-1), job)
    finally:
        api.k.CloseHandle(job)


class NativeWorker:
    """Retained handles plus atomic Job-list creation from existing native code."""
    def __init__(self, plan_path, *, argv=None):
        import ctypes as c
        from crawl_experiment.browser.windows_worker import WindowsAPI, ExtendedLimits
        self.api = WindowsAPI()
        name = 'google-benchmark-'+uuid.uuid4().hex
        plan = json.loads(plan_path.read_text(encoding='utf-8'))
        plan['native_job_name'] = name
        write_json(plan_path, plan)
        self.job = self.api.k.CreateJobObjectW(None, name)
        self.info = None
        self.api.require(self.job)
        try:
            limits = ExtendedLimits()
            limits.basic.flags = 0x2000  # KILL_ON_JOB_CLOSE, no breakaway.
            self.api.require(self.api.k.SetInformationJobObject(self.job, 9, c.byref(limits), c.sizeof(limits)))
            self.api.noninherited(self.job)
            self.info = self.api.create(self.job, argv or [sys.executable, '-B', '-m',
                'scripts.google_live_workers', '--worker-plan', str(plan_path)], ROOT)
            self.identity = self.api.identity(self.info.process)
            self.api.member(self.info.process, self.job)
            if self.api.k.ResumeThread(self.info.thread)==0xFFFFFFFF:
                raise RuntimeError('Cannot resume owned worker')
            self.api.k.CloseHandle(self.info.thread)
            self.info.thread = None
        except BaseException:
            self.close()
            raise

    def done(self):
        return self.api.wait(self.info.process)

    def exit_code(self):
        import ctypes as c
        from ctypes import wintypes as w
        code = w.DWORD()
        self.api.require(self.api.k.GetExitCodeProcess(self.info.process, c.byref(code)))
        return code.value

    def close(self):
        if not self.job:
            return 0
        try:
            self.api.require(self.api.k.TerminateJobObject(self.job, 91))
            deadline = time.monotonic()+10
            while self.api.active(self.job) and time.monotonic()<deadline:
                time.sleep(0.02)
            active = self.api.active(self.job)
            if active:
                raise RuntimeError('Owned Job Object cleanup incomplete')
            if self.info and not self.api.wait(self.info.process, 1000):
                raise RuntimeError('Retained worker handle not signaled')
            return active
        finally:
            if self.info:
                if self.info.thread:
                    self.api.k.CloseHandle(self.info.thread)
                self.api.k.CloseHandle(self.info.process)
            self.api.k.CloseHandle(self.job)
            self.job = None


def run(catalog, output, *, profile_dir=None, **options):
    if not options.get('operator_submit', False):
        raise ValueError('Explicit --operator-submit is required for new public-source attempts')
    workers=options.get('workers',2)
    profiles=[] if profile_dir is None else list(profile_dir) if isinstance(profile_dir,(list,tuple)) else [profile_dir]
    if profiles and len(profiles)!=workers:
        raise ValueError('Provide exactly one distinct --profile-dir per worker')
    if len({str(Path(p).resolve()).casefold() for p in profiles})!=len(profiles):
        raise ValueError('Workers cannot share the same profile')
    with ExitStack() as stack:
        reserved=[stack.enter_context(reserve_profile(p,1)) for p in profiles]
        return _run(catalog, output, profile_dirs=reserved, **options)


def _run(catalog, output, *, limit=10, workers=2, per_store_seconds=180,
        max_scrolls=50, total_seconds=1200, operator_submit=False, profile_dirs=None,
        review_source='dom', startup_debug=False):
    if not operator_submit:
        raise ValueError('Explicit --operator-submit is required for new public-source attempts')
    if os.name != 'nt':
        raise RuntimeError('Live runner requires Windows native containment; Linux is unvalidated')
    if review_source != 'dom':
        raise ValueError('Unsupported review source')
    if any(not math.isfinite(v) or v<=0 for v in (per_store_seconds, total_seconds)) or max_scrolls<1:
        raise ValueError('Invalid limits')
    places = [json_ready_place(place) for place in load_places(catalog, limit)]
    for place in places:
        store_for(place)  # Refuse incomplete inputs before creating output.
    if not 1 <= workers <= len(places):
        raise ValueError('workers must be between one and the number of places')
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    queue_path = output/'dispatch.sqlite3'
    create_queue(queue_path, places)
    write_json(output/'plan.json', dict(mode='LIVE_GOOGLE', places=places, workers=workers,
        scheduling='dynamic-sqlite',
        logged_out=not profile_dirs, profile_mode='operator-dedicated' if profile_dirs else 'fresh',
        catalog_sha256=hashlib.sha256(catalog.read_bytes()).hexdigest(),
        per_store_seconds=per_store_seconds, max_scrolls=max_scrolls,
        review_source=review_source, startup_debug=startup_debug,
        total_seconds=total_seconds, legacy_incident='PRESERVED_NOT_REPLAYED',
        storage_semantics='existing standalone change events; not append-all Bronze'))
    children, cleanup, samples = [], [], []
    closed_workers = set()
    sampled_cpu={}
    started, reason = time.monotonic(), None
    launch_seconds = 0.0
    cleanup_seconds = 0.0
    import psutil
    try:
        for index in range(workers):
            directory = output/f'worker-{index+1:02d}'
            directory.mkdir()
            write_json(directory/'plan.json', dict(places=[], queue_path=str(queue_path),
                per_store_seconds=per_store_seconds, max_scrolls=max_scrolls,
                profile_dir=str(profile_dirs[index]) if profile_dirs else None,review_source=review_source,
                startup_debug=startup_debug))
            child = NativeWorker(directory/'plan.json')
            children.append((child, directory))
        launch_seconds = time.monotonic()-started
        while not all(directory.name in closed_workers or child.done() for child, directory in children):
            for child, directory in children:
                if directory.name not in closed_workers and child.done():
                    code = child.exit_code()
                    active = child.close()  # Exact native Job only, before siblings continue.
                    cleanup.append(dict(worker=directory.name, identity=asdict(child.identity),
                        active_after=active,exit_code_before_cleanup=code))
                    closed_workers.add(directory.name)
                    if code != 0:
                        write_json(directory/'WORKER_STOP.json', dict(scope='WORKER',
                            reason='WORKER_CRASH_NO_REPLAY', exit_code=code))
            if (output/'SOURCE_STOP.json').exists():
                reason = 'SOURCE_STOP'
                break
            if time.monotonic()-started > total_seconds:
                reason = 'EXTERNAL_TIMEOUT_UNKNOWN_NO_REPLAY'
                break
            memory = psutil.virtual_memory()
            sample = dict(elapsed_seconds=time.monotonic()-started, rss_bytes=0, cpu_seconds=0,
                system_available_ram_bytes=memory.available, system_ram_percent=memory.percent)
            for child, directory in children:
                if directory.name in closed_workers:
                    continue
                for pid in child.api.pids(child.job):
                    try:
                        process = psutil.Process(pid)
                        sample['rss_bytes'] += process.memory_info().rss
                        cpu = process.cpu_times()
                        key=(pid,process.create_time())
                        sampled_cpu[key]=max(sampled_cpu.get(key,0),cpu.user+cpu.system)
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
            sample['cpu_seconds']=sum(sampled_cpu.values())
            samples.append(sample)
            time.sleep(0.2)  # Self-contained runner; operator need not babysit.
    except BaseException as exc:
        reason = f'{type(exc).__name__}: {exc}'
        if isinstance(exc, KeyboardInterrupt):
            reason = 'OPERATOR_INTERRUPTED_UNKNOWN_NO_REPLAY'
    finally:
        cleanup_started = time.monotonic()
        for child, directory in children:
            if directory.name in closed_workers:
                continue
            try:
                code = child.exit_code()
                if code not in {0, 259} and not (directory/'WORKER_STOP.json').exists():
                    write_json(directory/'WORKER_STOP.json', dict(scope='WORKER',
                        reason='WORKER_CRASH_NO_REPLAY',exit_code=code))
                active = child.close()
                cleanup.append(dict(worker=directory.name, identity=asdict(child.identity),
                    active_after=active, exit_code_before_cleanup=code))
            except Exception as exc:
                cleanup.append(dict(worker=directory.name, error=str(exc)))
        cleanup_seconds = time.monotonic()-cleanup_started
        reports = []
        for _, directory in children:
            path = directory/'summary.json'
            reports.append(json.loads(path.read_text(encoding='utf-8')) if path.exists() else dict(results=[]))
        rows = [r for report in reports for r in report['results']]
        if reason is None and (output/'SOURCE_STOP.json').exists():
            reason = 'SOURCE_STOP'
        if reason is None and any(r.get('exit_code_before_cleanup')!=0 for r in cleanup):
            reason = 'WORKER_FAILED_UNKNOWN_NO_REPLAY'
        elapsed = time.monotonic()-started
        with sqlite3.connect(queue_path.as_uri()+'?mode=ro', uri=True) as db:
            queue_counts = dict(db.execute('SELECT state,COUNT(*) FROM dispatch GROUP BY state'))
        if reason is None and queue_counts.get('FINISHED', 0) != limit:
            reason = 'INCOMPLETE_QUEUE_NO_REPLAY'
        storage,storage_errors=[],[]
        for database in output.glob('worker-*/job-*/reviews.sqlite3'):
            try:
                storage.append(dict(job=str(database.parent.relative_to(output)),
                    **reconcile(database,database.parent/'lake')))
            except Exception as exc:
                storage_errors.append(dict(job=str(database.parent.relative_to(output)),error=str(exc)))
        write_json(output/'resource_samples.json', dict(samples=samples,
            cpu_semantics='sampled CPU lower bound by PID+creation-time identity; excludes unsampled tail before exit'))
        result = dict(mode='LIVE_GOOGLE', status='FINISHED' if reason is None else 'STOPPED',
            stop_reason=reason, assigned=limit, completed=len(rows), workers=workers,
            scheduling='dynamic-sqlite', queue_counts=queue_counts,
            stopped_workers=[directory.name for _,directory in children
                             if (directory/'WORKER_STOP.json').exists()],
            full_store_count=sum(r['coverage']=='FULL' for r in rows),
            partial_store_count=sum(r['coverage']=='PARTIAL' for r in rows),
            unknown_store_count=sum(r['coverage']=='UNKNOWN' for r in rows),
            elapsed_seconds=elapsed, results=rows, cleanup=cleanup,
            source_stopped=(output/'SOURCE_STOP.json').exists(),
            peak_owned_rss_bytes=max((s['rss_bytes'] for s in samples), default=0),
            sqlite_records=sum(r['sqlite_records'] for r in storage),
            parquet_rows=sum(r['parquet_rows'] for r in storage),
            pending=sum(r['pending'] for r in storage),
            retained_storage=storage,reconciliation_errors=storage_errors,
            cleanup_verified=len(cleanup)==workers and all(r.get('active_after')==0 for r in cleanup))
        # Completion of a process is not completion of collection or publication.
        result['acceptance_pass'] = (reason is None and len(rows)==limit and result['cleanup_verified']
            and not result['source_stopped'] and not storage_errors and result['pending']==0
            and all(r['coverage']=='FULL' for r in rows))
        durations=sorted(r['elapsed_seconds'] for r in rows)
        def percentile(fraction):
            if not durations:return None
            point=(len(durations)-1)*fraction
            low=math.floor(point);high=math.ceil(point)
            return durations[low]+(durations[high]-durations[low])*(point-low)
        published_jobs=sum(r['coverage'] in {'FULL','PARTIAL'} and
            r.get('reconciliation',{}).get('pending')==0 for r in rows)
        result['benchmark']=dict(
            measurement_complete=reason is None and len(rows)==limit and result['cleanup_verified']
                and not result['stopped_workers'] and not storage_errors and result['pending']==0,
            wall_seconds=elapsed,workers=workers,assigned_stores=limit,attempted_stores=len(rows),
            published_job_rate=published_jobs/limit,full_coverage_rate=result['full_store_count']/limit,
            published_jobs_per_hour=published_jobs*3600/elapsed if elapsed else None,
            staged_reviews_per_minute=result['sqlite_records']*60/elapsed if elapsed else None,
            published_events_per_minute=result['parquet_rows']*60/elapsed if elapsed else None,
            store_p50_seconds=percentile(.5),store_p95_seconds=percentile(.95),
            failed_or_unverified_stores=sum(r['coverage']=='UNKNOWN' for r in rows),
            parquet_files=sum(r['parquet_files'] for r in storage),
            parquet_bytes=sum(r['parquet_bytes'] for r in storage),
            pending_events=result['pending'],
            peak_owned_rss_mib=result['peak_owned_rss_bytes']/1024**2,
            host_total_ram_gib=psutil.virtual_memory().total/1024**3,
            minimum_system_available_ram_gib=min((s['system_available_ram_bytes'] for s in samples), default=0)/1024**3,
            sampled_cpu_seconds_lower_bound=sum(sampled_cpu.values()),
            mean_busy_cores_lower_bound=sum(sampled_cpu.values())/elapsed if elapsed else None,
            logical_cpus=psutil.cpu_count(),cloud_ready=False,
            limits=dict(per_store_seconds=per_store_seconds,max_scrolls=max_scrolls,total_seconds=total_seconds),
            review_source=review_source)
        startup_seconds = sum(s['elapsed_seconds'] for report in reports for s in report.get('startups', []))
        job_seconds = sum(r['elapsed_seconds'] for r in rows)
        result['benchmark']['overhead'] = dict(
            native_worker_launch_wall_seconds=launch_seconds,
            final_owned_cleanup_wall_seconds=cleanup_seconds,
            browser_startup_worker_seconds=startup_seconds,
            browser_sessions=sum(len(report.get('startups', [])) for report in reports),
            normal_browser_shutdown_worker_seconds=sum(report.get('browser_shutdown_seconds', 0) for report in reports),
            worker_browser_lifecycles=[report.get('browser_lifecycle', {}) for report in reports],
            total_job_worker_seconds=job_seconds,
            failed_or_unverified_job_worker_seconds=sum(r['elapsed_seconds'] for r in rows if r['coverage']=='UNKNOWN'),
            worker_utilization=[dict(worker=directory.name,
                completed=len(report['results']),
                job_seconds=sum(r['elapsed_seconds'] for r in report['results']),
                worker_elapsed_seconds=report.get('worker_elapsed_seconds'))
                for (_, directory), report in zip(children, reports)],
            semantics='Wall times overlap across workers. Job times include startup when needed, crawler work and per-place publication. Normal browser shutdown is measured separately per worker; final cleanup is supervisor cleanup only. CPU/RSS are sampled; CPU is a lower bound.')
        result['benchmark']['capacity_caveats'] = [
            'Bounded scans and failed place matches cannot establish full-review capacity.',
            'Use matched per-place coverage/row counts when comparing worker counts.',
            'Catalog sample is not a representative citywide probability sample.',
            'Windows Job Object evidence does not validate Linux/GCP deployment.',
            'No 77000-store completion-time or cloud-cost forecast is supported by this trial alone.']
        write_json(output/'summary.json', result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalog', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--limit', type=int, default=10)
    parser.add_argument('--workers', type=int, default=2)
    parser.add_argument('--per-store-seconds', type=float, default=180)
    parser.add_argument('--max-scrolls', type=int, default=50)
    parser.add_argument('--total-seconds', type=float, default=1200)
    parser.add_argument('--operator-submit', action='store_true')
    parser.add_argument('--profile-dir', type=Path,action='append',
        help='Repeat once per worker: distinct dedicated operator profiles; no personal Chrome profiles')
    parser.add_argument('--review-source',choices=('dom',),default='dom',
        help='DOM review content with CDP network timestamp enrichment')
    parser.add_argument('--startup-debug', action='store_true',
        help='Capture verbose ChromeDriver/Chrome startup diagnostics locally')
    parser.add_argument('--worker-plan', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.worker_plan:
        directory = args.worker_plan.resolve().parent
        with (directory/'worker.log').open('w', encoding='utf-8', buffering=1) as log:
            sys.stdout = sys.stderr = log
            crawl_shard(json.loads(args.worker_plan.read_text(encoding='utf-8')), directory)
        return 0
    if args.catalog is None or args.output is None:
        parser.error('--catalog and --output are required')
    result = run(args.catalog, args.output, limit=args.limit, workers=args.workers,
        per_store_seconds=args.per_store_seconds, max_scrolls=args.max_scrolls,
        total_seconds=args.total_seconds, operator_submit=args.operator_submit,
        profile_dir=args.profile_dir, review_source=args.review_source, startup_debug=args.startup_debug)
    print(json.dumps({k:v for k,v in result.items() if k not in {'results', 'cleanup'}}))
    return 0 if result['acceptance_pass'] else 1


if __name__=='__main__':
    raise SystemExit(main())
