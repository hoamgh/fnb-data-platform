"""Offline contracts for the bounded Google worker/runner integration."""

import asyncio
from contextlib import ExitStack, contextmanager
import json
import logging
from pathlib import Path
import socket
import sqlite3
import sys
from types import ModuleType, SimpleNamespace

import pytest

from crawl_experiment.browser.session_manager import BrowserSessionManager
from crawl_experiment.core.errors import (ReviewSurfaceError, SortError, PlaceResolutionError,
                                          NavigationError, ChallengeError, ReviewParseError, PaginationStalledError)
from crawl_experiment.core.models import Store
from crawl_experiment.orchestration.crawlee_adapter import CrawleeAdapter
from crawl_experiment.runners import full_crawl_runner as full
from scripts import google_live_workers as workers


def store(name):
    return Store(name, name, name)


def place(name):
    return {'place_key': name, 'name': name, 'address': f'{name} Street'}


class FakeSession:
    def __init__(self):
        self.closed = False

    def start(self):
        return object()

    def is_alive(self):
        return not self.closed

    def close(self):
        self.closed = True


class FakeFactory:
    def __init__(self):
        self.sessions = []

    def create(self, session_id):
        session = FakeSession()
        self.sessions.append(session)
        return session


@pytest.fixture
def fake_crawlee(monkeypatch):
    """Run real adapter callbacks using an in-memory queue and no browsers/network."""
    crawlee = ModuleType('crawlee')
    crawlers = ModuleType('crawlee.crawlers')
    storages = ModuleType('crawlee.storages')

    class Request:
        @classmethod
        def from_url(cls, url, *, unique_key, user_data):
            return SimpleNamespace(user_data=user_data, retry_count=0)

    class RequestQueue:
        def __init__(self):
            self.requests = []

        @classmethod
        async def open(cls, **kwargs):
            return cls()

        async def add_requests(self, requests):
            self.requests.extend(requests)

        async def drop(self):
            pass

    class BasicCrawler:
        def __init__(self, *, request_manager, request_handler, **kwargs):
            self.queue = request_manager
            self.handle = request_handler

        async def run(self, **kwargs):
            for request in self.queue.requests:
                await self.handle(SimpleNamespace(request=request, session=None))
            return None

    crawlee.Request = Request
    crawlee.ConcurrencySettings = lambda **kwargs: kwargs
    crawlers.BasicCrawler = BasicCrawler
    storages.RequestQueue = RequestQueue
    monkeypatch.setitem(sys.modules, 'crawlee', crawlee)
    monkeypatch.setitem(sys.modules, 'crawlee.crawlers', crawlers)
    monkeypatch.setitem(sys.modules, 'crawlee.storages', storages)


def test_external_browser_manager_reused_across_adapter_runs_and_not_closed(fake_crawlee):
    factory = FakeFactory()
    manager = BrowserSessionManager(factory, worker_id='worker-01')
    seen = []
    warmed = []
    loops = []
    with asyncio.Runner() as event_loop:
        for name in ('alpha', 'beta'):
            adapter = CrawleeAdapter(
                factory, lambda item, driver: seen.append(item.id) or SimpleNamespace(parse_errors=0),
                browser_session_manager=manager,
                browser_warm_up=lambda driver: warmed.append(driver),
            )

            async def run_one():
                loops.append(asyncio.get_running_loop())
                await adapter.run([store(name)])

            event_loop.run(run_one())
            assert factory.sessions[0].closed is False
            assert adapter.lifecycle_summary['browsers_created'] == 1

    assert loops[0] is loops[1]
    assert seen == ['alpha', 'beta']
    assert len(factory.sessions) == 1
    assert len(warmed) == 1
    assert set(manager.summary()['stores_per_browser']['browser-001']) == {'alpha', 'beta'}
    manager.close()
    assert factory.sessions[0].closed is True


def test_default_adapter_closes_its_owned_browser(fake_crawlee):
    factory = FakeFactory()
    adapter = CrawleeAdapter(factory, lambda item, driver: SimpleNamespace(parse_errors=0))
    asyncio.run(adapter.run([store('alpha')]))
    assert adapter.lifecycle_summary['browsers_created'] == 1
    assert adapter.lifecycle_summary['browsers_retired'] == 1
    assert factory.sessions[0].closed


@pytest.mark.parametrize('intercepted', [False, True])
def test_missing_review_tab_goes_to_dlq_and_next_store_reuses_browser(fake_crawlee, intercepted):
    factory = FakeFactory()
    manager = BrowserSessionManager(factory)
    retry, Access = workers.live_policies()

    class Repository:
        def __init__(self):
            self.rows = {}

        def get(self, key):
            return self.rows.setdefault(key, dict(next_eligible_at=None,
                last_access_state=None, consecutive_runtime_error_count=0,
                consecutive_unknown_count=0, consecutive_limited_count=0))

        def update(self, key, mutate):
            row = self.get(key)
            mutate(row)
            return row

    terminal, succeeded = [], []

    def crawl(item, driver):
        if item.id == 'missing':
            error = ReviewSurfaceError('review surface state UNKNOWN: Reviews tab not found')
            error.local_missing_review_tab = not intercepted
            error.local_review_tab_click_intercepted = intercepted
            raise error
        succeeded.append(item.id)
        return SimpleNamespace(parse_errors=0)

    adapter = CrawleeAdapter(factory, crawl, retry_policy=retry,
        browser_session_manager=manager, access_policy=Access(Repository()),
        on_terminal_failure=lambda *args: terminal.append(args))
    asyncio.run(adapter.run([store('missing'), store('next')]))

    assert succeeded == ['next']
    assert len(terminal) == 1
    assert terminal[0][0].id == 'missing'
    assert len(factory.sessions) == 1
    assert manager.summary()['browser_restarts'] == 0
    assert workers.stop_scope({'status': 'REVIEW_SURFACE_UNAVAILABLE',
        'final_access_state': 'UNKNOWN', 'failure_reason_code': 'REVIEW_SURFACE_NOT_FOUND',
        'local_missing_review_tab': True, 'local_store_failure': retry.local_store_failure}) is None
    manager.close()


@pytest.mark.parametrize('error,status', [
    (ReviewParseError('invalid card'), 'EXTRACTION_DEGRADED'),
    (PaginationStalledError('no progress'), 'PAGINATION_STALLED'),
    (SortError('sort menu unavailable'), 'SORT_FAILED'),
    (PlaceResolutionError('no matching store'), 'PLACE_RESOLUTION_FAILED'),
    (NavigationError('page timeout'), 'NAVIGATION_FAILED'),
])
def test_store_local_failure_continues_without_browser_restart(error, status, fake_crawlee):
    if isinstance(error, NavigationError) and type(error) is NavigationError:
        error.readonly_navigation_failure = 'PAGE_LOAD_TIMEOUT'
    factory = FakeFactory()
    manager = BrowserSessionManager(factory)
    retry, _ = workers.live_policies()
    failures = []

    def crawl(item, driver):
        if item.id == 'bad':
            raise error
        return SimpleNamespace(parse_errors=0)

    adapter = CrawleeAdapter(factory, crawl, retry_policy=retry,
        browser_session_manager=manager,
        on_terminal_failure=lambda *args: failures.append(args))
    asyncio.run(adapter.run([store('bad'), store('next')]))
    assert len(failures) == 1
    assert str(failures[0][1].status) == status
    assert workers.stop_scope({'status': status, 'final_access_state': 'UNKNOWN',
                               'local_store_failure': True}) is None
    assert len(factory.sessions) == 1
    assert manager.summary()['browser_restarts'] == 0
    manager.close()


def test_challenge_and_dead_session_are_not_store_local():
    retry, _ = workers.live_policies()
    retry.decide(ChallengeError('captcha'))
    assert retry.local_store_failure is False
    assert workers.stop_scope({'status': 'CHALLENGE',
        'local_store_failure': True, 'security_restriction': True}) == 'WORKER'
    ended = NavigationError('driver session closed')
    ended.readonly_navigation_failure = 'DRIVER_SESSION_ENDED'
    retry.decide(ended)
    assert retry.local_store_failure is False
    assert workers.stop_scope({'status': 'NAVIGATION_FAILED',
        'final_access_state': 'UNKNOWN', 'local_store_failure': False}) == 'WORKER'


@pytest.mark.parametrize('intercepted', [False, True])
def test_worker_store_error_queues_next_store_and_preserves_healthy_browser(tmp_path, monkeypatch, intercepted):
    monkeypatch.setattr(workers, 'reconcile', lambda *args: {'pending': 0})
    root = tmp_path/'run'
    directory = root/'worker-01'
    directory.mkdir(parents=True)
    queue = root/'dispatch.sqlite3'
    workers.create_queue(queue, [place('bad'), place('good')])
    factory = FakeFactory()

    def fake_runner(stores, **kwargs):
        manager = kwargs['browser_session_manager']
        manager.acquire(stores[0].id, 1)
        row = {'status': 'COMPLETE'}
        if stores[0].name == 'bad':
            error = ReviewSurfaceError('review surface state UNKNOWN: Reviews tab not found')
            error.local_missing_review_tab = not intercepted
            error.local_review_tab_click_intercepted = intercepted
            kwargs['retry_policy'].decide(error)
            row = {'status': 'REVIEW_SURFACE_UNAVAILABLE', 'error': str(error),
                   'failure_reason_code': 'REVIEW_SURFACE_NOT_FOUND',
                   'final_access_state': 'UNKNOWN'}
        path = kwargs['output_root']/'offline'
        path.mkdir(parents=True)
        (path/'summary.json').write_text(json.dumps({'stores': [row]}), encoding='utf-8')
        return 0

    with asyncio.Runner() as loop, ExitStack() as cleanup:
        result = workers._crawl_shard(
            {'queue_path': str(queue), 'per_store_seconds': 20, 'max_scrolls': 2,
             'review_source': 'dom'}, directory, factory_builder=lambda job: factory,
            crawl_runner=fake_runner, async_runner=loop, session_cleanup=cleanup)
    assert len(result['results']) == 2
    assert [row['disposition'] for row in result['results']] == ['DLQ', 'FULL']
    assert result['browser_lifecycle']['browsers_created'] == 1
    assert result['browser_lifecycle']['browser_restarts'] == 0
    assert not (directory/'WORKER_STOP.json').exists()
    with sqlite3.connect(queue) as db:
        assert [row[0] for row in db.execute('SELECT state FROM dispatch ORDER BY id')] == [
            'FINISHED', 'FINISHED']


@pytest.fixture
def fake_runner_artifacts(monkeypatch):
    class Artifacts:
        def __init__(self, *args, **kwargs):
            pass

        def finalize(self, **kwargs):
            pass

    monkeypatch.setattr(full, 'CrawlArtifacts', Artifacts)
    monkeypatch.setattr(full, 'create_run_directory', lambda path: path)
    monkeypatch.setattr(full, 'configure_logging', lambda path: logging.getLogger('offline'))
    monkeypatch.setattr(full, 'build_summary', lambda *args, **kwargs: {
        'stores': [], 'parquet_output_path': 'offline',
    })
    monkeypatch.setattr(full, 'write_json', lambda path, payload: None)


def test_runner_forwards_injections_and_reuses_worker_loop(monkeypatch, tmp_path, fake_runner_artifacts):
    factory = FakeFactory()
    manager = BrowserSessionManager(factory)
    retry = object()
    access_factory = type('AccessFactory', (), {})
    calls = []

    async def fake_crawl(*args, **kwargs):
        calls.append((asyncio.get_running_loop(), kwargs))

    monkeypatch.setattr(full, '_run_crawl', fake_crawl)
    with asyncio.Runner() as loop:
        for name in ('alpha', 'beta'):
            assert full.run_full_crawl(
                [store(name)], output_root=tmp_path, browser_factory=factory,
                browser_session_manager=manager, retry_policy=retry,
                access_policy_factory=access_factory, async_runner=loop,
                review_source='dom',
            ) == 0
        assert calls[0][0] is calls[1][0] is loop.get_loop()

    assert all(call['browser_session_manager'] is manager for _, call in calls)
    assert all(call['browser_factory'] is factory for _, call in calls)
    assert all(call['retry_policy'] is retry for _, call in calls)
    assert all(call['access_policy_factory'] is access_factory for _, call in calls)
    assert all(call['review_source'] == 'dom' for _, call in calls)

    assert full.run_full_crawl([store('default')], output_root=tmp_path) == 0
    assert calls[-1][1]['browser_factory'] is None
    assert calls[-1][1]['browser_session_manager'] is None
    assert calls[-1][1]['review_source'] == 'dom'


def test_review_source_and_factory_mismatch_fail_before_runner(tmp_path):
    with pytest.raises(ValueError, match="only 'dom'"):
        full.run_full_crawl([store('alpha')], output_root=tmp_path, review_source='hybrid')
    with pytest.raises(ValueError, match='BrowserFactory'):
        full.run_full_crawl([store('alpha')], output_root=tmp_path,
                            browser_factory=FakeFactory(),
                            browser_session_manager=BrowserSessionManager(FakeFactory()))
    with pytest.raises(ValueError, match="Unsupported review source"):
        workers._run(tmp_path/'missing.csv', tmp_path/'output', review_source='hybrid',
                     operator_submit=True)
    assert not (tmp_path/'output').exists()


def test_internal_runner_wires_browser_retry_access_and_dom(monkeypatch, tmp_path):
    seen = {}

    class FakeReviewRepository:
        def __init__(self, *args, **kwargs):
            pass

        def close(self):
            pass

        def stats(self):
            return {}

    class FakeAccessRepository:
        def __init__(self, *args, **kwargs):
            pass

        def close(self):
            pass

    class FakeCrawler:
        def __init__(self, *args, **kwargs):
            pass

        def crawl(self, *args, **kwargs):
            seen['crawl_kwargs'] = kwargs
            return 'DOM_RESULT'

        def mark_browser_warm_up_completed(self):
            pass

    class FakeAdapter:
        def __init__(self, *args, **kwargs):
            seen['adapter_args'] = args
            seen['adapter_kwargs'] = kwargs
            self.lifecycle_summary = {}
            self.access_job_metrics = []

        async def run(self, stores):
            seen['result'] = seen['adapter_args'][1](stores[0], object())

    class FakeArtifacts:
        on_attempt = on_result = on_terminal_failure = on_access_deferred = lambda *a: None
        elapsed_for_store = lambda *a: 0
        record_browser_lifecycle = record_persistence = lambda *a, **k: None

    monkeypatch.setattr(full, 'ParquetReviewWriter', lambda *a: None)
    monkeypatch.setattr(full, 'ReviewRepository', FakeReviewRepository)
    monkeypatch.setattr(full, 'PlaceAccessRepository', FakeAccessRepository)
    monkeypatch.setattr(full, 'GoogleMapsCrawler', FakeCrawler)
    monkeypatch.setattr(full, 'CrawleeAdapter', FakeAdapter)
    factory = FakeFactory()
    manager = BrowserSessionManager(factory)
    retry = object()

    class Access:
        def __init__(self, repository, config):
            seen['access_config'] = config

    config = object()
    run_dir = tmp_path/'run'
    run_dir.mkdir()
    asyncio.run(full._run_crawl(
        run_dir, logging.getLogger('offline'), FakeArtifacts(), started_at='2026-11-03',
        stores=(store('alpha'),), state_database=tmp_path/'state.sqlite',
        parquet_root=tmp_path/'lake', timeout_seconds=10, max_scrolls=2,
        concurrency=1, browser_factory=factory, retry_policy=retry,
        access_policy_factory=Access, access_policy_config=config,
        browser_session_manager=manager, review_source='dom',
    ))
    assert seen['result'] == 'DOM_RESULT'
    assert seen['crawl_kwargs']['sort_newest'] is True
    assert 'review_source' not in seen['crawl_kwargs']  # existing DOM extraction
    assert seen['adapter_args'][0] is factory
    assert seen['adapter_kwargs']['browser_session_manager'] is manager
    assert seen['adapter_kwargs']['retry_policy'] is retry
    assert seen['access_config'] is config


def test_failed_dispatch_is_terminal_and_never_reclaimed(tmp_path):
    path = tmp_path/'dispatch.sqlite3'
    workers.create_queue(path, [place('alpha'), place('beta')])
    index, _ = workers.claim_next(path, 'worker-01')
    workers.finish_dispatch(path, index, 'worker-01',
                            {'status': 'ERROR', 'coverage': 'UNKNOWN',
                             'error': "TypeError: unexpected keyword argument 'browser_factory'"})
    second, _ = workers.claim_next(path, 'worker-02')
    workers.finish_dispatch(path, second, 'worker-02',
                            {'status': 'COMPLETE', 'coverage': 'FULL'})
    assert workers.claim_next(path, 'worker-03') is None
    with sqlite3.connect(path) as db:
        assert [json.loads(r[0])['outcome'] for r in db.execute('SELECT result FROM dispatch ORDER BY id')] == ['ERROR', 'SUCCESS']
    with sqlite3.connect(path) as db:
        assert list(db.execute('SELECT state FROM dispatch ORDER BY id')) == [
            ('FINISHED',), ('FINISHED',)]


def test_interrupted_queue_keeps_checkpoint_and_resumes_only_pending(tmp_path):
    from crawl_experiment.storage.checkpoint_repository import Checkpoint,CheckpointRepository
    from crawl_experiment.core.statuses import CrawlStatus
    queue=tmp_path/'dispatch.sqlite3'
    workers.create_queue(queue,[place('first'),place('interrupted'),place('pending')])
    index,_=workers.claim_next(queue,'worker-01')
    workers.finish_dispatch(queue,index,'worker-01',dict(status='NO_REVIEWS',coverage='FULL'))
    interrupted,_=workers.claim_next(queue,'worker-02')
    repo=CheckpointRepository(tmp_path/'checkpoints')
    repo.save(Checkpoint('interrupted',CrawlStatus.PARTIAL_LIMIT,reviews_seen=7,scroll_count=2))
    before=(tmp_path/'checkpoints'/'interrupted.json').read_bytes()
    # Reopening the same SQLite dispatch does not replay unknown browser work.
    pending,_=workers.claim_next(queue,'worker-03')
    assert pending!=interrupted and pending==2
    assert repo.load('interrupted').reviews_seen==7
    assert (tmp_path/'checkpoints'/'interrupted.json').read_bytes()==before
    assert workers.claim_next(queue,'worker-04') is None


def test_worker_integration_error_stops_without_replay(tmp_path):
    root = tmp_path/'run'
    directory = root/'worker-01'
    directory.mkdir(parents=True)
    queue = root/'dispatch.sqlite3'
    workers.create_queue(queue, [place('alpha'), place('beta')])
    plan = {'queue_path': str(queue), 'per_store_seconds': 20, 'max_scrolls': 2,
            'review_source': 'dom'}

    def bad_runner(*args, **kwargs):
        raise TypeError("run_full_crawl() got an unexpected keyword argument 'browser_factory'")

    with asyncio.Runner() as loop, ExitStack() as cleanup:
        result = workers._crawl_shard(plan, directory, factory_builder=lambda job: FakeFactory(),
                                     crawl_runner=bad_runner, async_runner=loop,
                                     session_cleanup=cleanup)
    assert len(result['results']) == 1
    assert result['results'][0]['error_category'] == 'INTEGRATION_ERROR'
    assert result['results'][0]['status'] == 'ERROR'
    assert (directory/'WORKER_STOP.json').exists()
    with sqlite3.connect(queue) as db:
        assert list(db.execute('SELECT state FROM dispatch ORDER BY id')) == [
            ('FINISHED',), ('PENDING',)]


@pytest.mark.parametrize(('runner_error', 'category'), [
    ("TypeError: unexpected keyword argument 'browser_factory'", 'INTEGRATION_ERROR'),
    ('ValueError: unsupported review_source', 'CONFIGURATION_ERROR'),
])
def test_runner_reported_errors_are_failed_and_not_replayed(tmp_path, runner_error, category, monkeypatch):
    monkeypatch.setattr(workers, 'reconcile', lambda *args: {'pending': 0})
    directory = tmp_path/'worker-01'
    directory.mkdir()
    queue = tmp_path/'dispatch.sqlite3'
    workers.create_queue(queue, [place('alpha'), place('beta')])

    def failed_runner(stores, **kwargs):
        path = kwargs['output_root']/'offline'
        path.mkdir(parents=True)
        (path/'summary.json').write_text(json.dumps({
            'runner_error': runner_error,
            'stores': [{'status': 'COMPLETE', 'coverage': 'FULL'}],
        }), encoding='utf-8')
        return 1

    with asyncio.Runner() as loop, ExitStack() as cleanup:
        result = workers._crawl_shard(
            {'queue_path': str(queue), 'per_store_seconds': 20, 'max_scrolls': 2,
             'review_source': 'dom'}, directory, factory_builder=lambda job: FakeFactory(),
            crawl_runner=failed_runner, async_runner=loop, session_cleanup=cleanup)
    assert len(result['results']) == 1
    assert result['results'][0]['error_category'] == category
    assert result['results'][0]['status'] == 'ERROR'
    assert result['results'][0]['coverage'] == 'UNKNOWN'
    assert (directory/'WORKER_STOP.json').exists()
    with sqlite3.connect(queue) as db:
        assert list(db.execute('SELECT state FROM dispatch ORDER BY id')) == [
            ('FINISHED',), ('PENDING',)]


def test_worker_forwards_same_manager_loop_and_isolated_profiles(monkeypatch, tmp_path):
    root = tmp_path/'run'
    directory = root/'worker-01'
    directory.mkdir(parents=True)
    queue = root/'dispatch.sqlite3'
    workers.create_queue(queue, [place('alpha'), place('beta')])
    monkeypatch.setattr(workers, 'reconcile', lambda *args: {'pending': 0})
    seen = []

    def fake_runner(stores, **kwargs):
        seen.append((kwargs['browser_session_manager'], kwargs['async_runner'].get_loop(),
                     kwargs['browser_factory'], kwargs['review_source']))
        path = kwargs['output_root']/'offline'
        path.mkdir(parents=True)
        (path/'summary.json').write_text(json.dumps({'stores': [{'status': 'COMPLETE'}]}),
                                         encoding='utf-8')
        return 0

    with asyncio.Runner() as loop, ExitStack() as cleanup:
        result = workers._crawl_shard(
            {'queue_path': str(queue), 'per_store_seconds': 20, 'max_scrolls': 2,
             'review_source': 'dom'}, directory, factory_builder=lambda job: FakeFactory(),
            crawl_runner=fake_runner, async_runner=loop, session_cleanup=cleanup)
    assert len(seen) == 2
    assert seen[0][0] is seen[1][0]
    assert seen[0][1] is seen[1][1]
    assert all(factory is manager.browser_factory and source == 'dom'
               for manager, _, factory, source in seen)
    assert all(row['coverage'] == 'FULL' for row in result['results'])

    captured = {}
    monkeypatch.setattr(workers, '_run', lambda catalog, output, **kwargs:
                        captured.update(kwargs) or {'acceptance_pass': True})
    @contextmanager
    def fake_reservation(profile, worker_count):
        assert worker_count == 1
        yield Path(profile)

    monkeypatch.setattr(workers, 'reserve_profile', fake_reservation)
    profiles = [tmp_path/f'worker-profile-{i}' for i in range(4)]
    workers.run(tmp_path/'catalog', tmp_path/'output', profile_dir=profiles,
                workers=4, operator_submit=True)
    assert len(set(map(str, captured['profile_dirs']))) == 4
    with pytest.raises(ValueError, match='share the same profile'):
        workers.run(tmp_path/'catalog', tmp_path/'output',
                    profile_dir=[profiles[0]]*4, workers=4, operator_submit=True)


def test_four_worker_driver_builders_preserve_profile_isolation(monkeypatch, tmp_path):
    """Exercise four real driver builders with offline drivers and isolated profiles."""
    monkeypatch.setattr(workers, 'verify_native_membership', lambda plan: None)
    monkeypatch.setattr(workers, 'reconcile', lambda *args: {'pending': 0})
    created = []

    class FakeDriver:
        def __init__(self, **kwargs):
            assert kwargs['uc'] is True and kwargs['headless'] is False
            port = int(kwargs['chromium_arg'].split('=', 1)[1])
            self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.listener.bind(('127.0.0.1', port))
            self.current_url = 'about:blank'
            created.append((port, kwargs['user_data_dir'], self))

        def set_page_load_timeout(self, seconds):
            pass

        def set_script_timeout(self, seconds):
            pass

        def quit(self):
            # Keep ports bound until all four workers have started.
            pass

    stub = ModuleType('seleniumbase')
    stub.Driver = FakeDriver
    monkeypatch.setitem(sys.modules, 'seleniumbase', stub)

    def fake_runner(stores, **kwargs):
        kwargs['browser_session_manager'].acquire(stores[0].id, 1)
        result_dir = kwargs['output_root']/'offline'
        result_dir.mkdir(parents=True)
        (result_dir/'summary.json').write_text(
            json.dumps({'stores': [{'status': 'COMPLETE'}]}), encoding='utf-8')
        return 0

    root = tmp_path/'run'
    root.mkdir()
    try:
        for index in range(4):
            directory = root/f'worker-{index+1:02d}'
            directory.mkdir()
            plan = {'places': [place(f'store-{index}')], 'per_store_seconds': 20,
                    'max_scrolls': 2, 'review_source': 'dom'}
            with asyncio.Runner() as loop, ExitStack() as cleanup:
                result = workers._crawl_shard(plan, directory,
                    crawl_runner=fake_runner, async_runner=loop, session_cleanup=cleanup)
            assert result['results'][0]['coverage'] == 'FULL'
            assert result['browser_lifecycle']['browsers_created'] == 1
        assert len(created) == 4
        assert len({port for port, _, _ in created}) == 4
        assert len({profile for _, profile, _ in created}) == 4
    finally:
        for _, _, driver in created:
            driver.listener.close()


@pytest.mark.parametrize('status', ['PARTIAL_LIMIT', 'PARTIAL_TIMEOUT'])
def test_partial_scan_is_not_error_or_automatic_replay(tmp_path, status):
    queue = tmp_path/'queue.sqlite3'
    workers.create_queue(queue, [place('one')])
    index, _ = workers.claim_next(queue, 'worker-01')
    result = dict(status=status, coverage='PARTIAL')
    assert workers.crawl_outcome(result) == 'PARTIAL'
    assert workers.stop_scope(result) is None
    workers.finish_dispatch(queue, index, 'worker-01', result)
    with sqlite3.connect(queue) as db:
        state, saved = db.execute('SELECT state,result FROM dispatch').fetchone()
    assert state == 'FINISHED'
    assert json.loads(saved)['outcome'] == 'PARTIAL'
    assert workers.claim_next(queue, 'worker-02') is None


@pytest.mark.parametrize('status', ['AUTH_REQUIRED', 'CHALLENGE', 'RATE_LIMITED', 'SESSION_FAILED', 'ERROR', 'PERSISTING'])
def test_local_flag_never_bypasses_hard_failures(status):
    assert workers.stop_scope(dict(status=status, local_store_failure=True)) == 'WORKER'


@pytest.mark.parametrize('message', ['captcha', 'http 429', 'login required'])
def test_intercepted_tab_does_not_bypass_security_restrictions(message):
    retry, _ = workers.live_policies()
    error = ReviewSurfaceError(message)
    error.local_review_tab_click_intercepted = True
    retry.decide(error)
    assert retry.security_restriction
    assert not retry.local_review_tab_click_intercepted
    assert not retry.local_store_failure
