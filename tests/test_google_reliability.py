"""Risk-based, offline contracts for Google crawler v1; never launch a browser."""
import asyncio
import json
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Lock
from types import SimpleNamespace

import pytest
import pyarrow.parquet as pq

from scripts import google_live_workers as workers
from crawl_experiment.core.errors import (AuthRequiredError, BrowserSessionError,
    ChallengeError, RateLimitedError, ReviewSurfaceError)
from crawl_experiment.core.models import Review, Store
from crawl_experiment.orchestration.access_policy import AccessPolicy, AccessPolicyConfig
from crawl_experiment.orchestration.crawlee_adapter import CrawleeAdapter
from crawl_experiment.orchestration.retry_policy import RetryPolicy
from crawl_experiment.sources.google_maps.extractor import ReviewExtractor
from crawl_experiment.sources.google_maps.paginator import ReviewPaginator
from crawl_experiment.sources.google_maps.review_timestamps import (NetworkTimestampCollector,
    ReviewTimestamps, timestamp_pair)
from crawl_experiment.storage.place_access_repository import PlaceAccessRepository
from crawl_experiment.storage.parquet_review_writer import ParquetReviewWriter
from crawl_experiment.storage.review_repository import ReviewRepository
from test_google_live_compat import fake_crawlee, FakeFactory
from test_google_vietnamese_review_ui import _Driver


@pytest.mark.parametrize('state', ['UNKNOWN', 'BROWSER_ERROR', 'RUNTIME_ERROR'])
@pytest.mark.parametrize('budget', [0, 1, 2])
def test_access_policy_honors_zero_and_nonzero_budget(state, budget):
    repo = PlaceAccessRepository(':memory:')
    try:
        policy = AccessPolicy(repo, AccessPolicyConfig(max_browser_retries=budget, max_runtime_retries=budget), uniform=lambda low, high: low)
        for n in range(budget + 1):
            decision = policy.failure('store', state, {state: n})
            assert decision.retry_fresh is (n < budget)
        if state == 'UNKNOWN':
            assert decision.job_status == 'DEFERRED_UNKNOWN'
            assert not policy.eligible('store')
        else:
            assert decision.job_status.startswith('FAILED_')
    finally:
        repo.close()


@pytest.mark.parametrize('scenario,budget,expected_calls', [
    ('unknown', 0, 1), ('unknown', 1, 2), ('unknown', 2, 3),
    ('mixed', 1, 3), ('mixed', 2, 5),
    ('auth', 1, 1), ('challenge', 1, 1), ('rate_limit', 1, 1),
])
def test_real_adapter_policy_does_not_bypass_stop_or_retry_budget(fake_crawlee, monkeypatch, scenario, budget, expected_calls):
    ceilings, calls, deferred, terminal = [], [], [], []
    class RetryingCrawler:
        def __init__(self, request_manager, request_handler, **kwargs):
            self.queue, self.handle = request_manager, request_handler
            self.ceiling = kwargs['max_request_retries']
            ceilings.append(self.ceiling)
        async def run(self, **kwargs):
            for request in self.queue.requests:
                while True:
                    try:
                        await self.handle(SimpleNamespace(request=request, session=None))
                        break
                    except Exception:
                        if request.retry_count >= self.ceiling:
                            raise
                        request.retry_count += 1
    monkeypatch.setattr(sys.modules['crawlee.crawlers'], 'BasicCrawler', RetryingCrawler)
    repo = PlaceAccessRepository(':memory:')
    policy = AccessPolicy(repo, AccessPolicyConfig(max_browser_retries=budget, max_runtime_retries=0), uniform=lambda low, high: low)
    def crawl(store, driver):
        calls.append(store.id)
        if scenario == 'mixed':
            if len(calls) > 2 * budget:
                return SimpleNamespace(parse_errors=0)
            if len(calls) % 2:
                raise BrowserSessionError('lost session')
        if scenario == 'auth':
            raise AuthRequiredError('state UNKNOWN: login required')
        if scenario == 'challenge':
            raise ChallengeError('captcha')
        if scenario == 'rate_limit':
            raise RateLimitedError('HTTP 429')
        raise ReviewSurfaceError('review surface state UNKNOWN: intercepted control')
    adapter = CrawleeAdapter(FakeFactory(), crawl, access_policy=policy,
        retry_policy=RetryPolicy(max_session_retries=0, max_navigation_retries=0, max_surface_retries=0),
        on_deferred=lambda *args: deferred.append(args), on_terminal_failure=lambda *args: terminal.append(args))
    try:
        asyncio.run(adapter.run([Store('s', 'Cafe', 'Cafe')]))
        assert len(calls) == expected_calls
        if budget == 0:
            assert ceilings == [0]
        if scenario == 'unknown':
            assert len(deferred) == 1
        if scenario in {'auth', 'challenge', 'rate_limit'}:
            assert len(terminal) == 1
            assert terminal[0][1].status.name == {'auth': 'AUTH_REQUIRED', 'challenge': 'CHALLENGE', 'rate_limit': 'RATE_LIMITED'}[scenario]
    finally:
        repo.close()


def _review():
    return Review(source='google_maps', source_review_id='r1', store_id='s1', text='fixture', rating=4)


@pytest.mark.parametrize('failure', ['before_publish', 'after_publish'])
def test_outbox_recovery_survives_publication_crash_without_duplicate(tmp_path, monkeypatch, failure):
    writer = ParquetReviewWriter(tmp_path/'lake', 'run', '2026-11-04', batch_size=10)
    repo = ReviewRepository(tmp_path/'state.sqlite3', run_id='run', parquet_writer=writer)
    assert repo.upsert(_review())
    original = writer.write_batch
    def crash(events):
        if failure == 'after_publish':
            original(events)
        raise OSError('injected publication failure')
    monkeypatch.setattr(writer, 'write_batch', crash)
    with pytest.raises(OSError, match='injected'):
        repo.flush()
    assert repo.count() == 1 and repo.pending_count() == 1
    repo._db.close()  # Simulated crash: do not call close(), which flushes.
    recovery_writer = ParquetReviewWriter(tmp_path/'lake', 'run', '2026-11-04', batch_size=10)
    recovered = ReviewRepository(tmp_path/'state.sqlite3', run_id='run', parquet_writer=recovery_writer)
    try:
        assert recovered.pending_count() == 0 and recovered.count() == 1
        assert not recovered.upsert(_review())
        files = list((tmp_path/'lake').rglob('*.parquet'))
        assert len(files) == 1 and pq.ParquetFile(files[0]).metadata.num_rows == 1
    finally:
        recovered.close()


def test_corrupt_existing_batch_cannot_acknowledge_and_drop_outbox(tmp_path):
    writer = ParquetReviewWriter(tmp_path/'lake', 'run', '2026-11-04', batch_size=10)
    repo = ReviewRepository(tmp_path/'state.sqlite3', run_id='run', parquet_writer=writer)
    try:
        repo.upsert(_review())
        row = repo._db.execute('SELECT event_id,payload_json FROM pending_review_events').fetchone()
        path, _ = writer.write_batch([(row[0], json.loads(row[1]))])
        original = pq.ParquetFile(path).read()
        pq.write_table(original.slice(0, 0), path)  # Same event footer, missing review row.
        with pytest.raises(RuntimeError, match='row count'):
            repo.flush()
        assert repo.pending_count() == 1
    finally:
        repo._db.close()


def test_sqlite_transaction_failure_does_not_leave_state_without_outbox(tmp_path):
    writer = ParquetReviewWriter(tmp_path/'lake', 'run', '2026-11-04', batch_size=10)
    repo = ReviewRepository(tmp_path/'state.sqlite3', run_id='run', parquet_writer=writer)
    try:
        repo._db.execute("CREATE TRIGGER reject_outbox BEFORE INSERT ON pending_review_events BEGIN SELECT RAISE(ABORT,'injected'); END")
        with pytest.raises(sqlite3.IntegrityError, match='injected'):
            repo.upsert(_review())
        assert repo.count() == 0 and repo.pending_count() == 0
    finally:
        repo.close()


def test_dispatch_concurrent_claims_and_crashed_owner_are_not_replayed(tmp_path):
    path = tmp_path/'dispatch.sqlite3'
    workers.create_queue(path, [{'place_key': str(i), 'name': str(i)} for i in range(12)])
    def claim_all(owner):
        result = []
        while (item := workers.claim_next(path, owner)) is not None:
            result.append(item[0])
        return result
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = [i for group in pool.map(claim_all, ['w1','w2','w3','w4']) for i in group]
    assert sorted(ids) == list(range(12)) and len(set(ids)) == 12
    assert workers.claim_next(path, 'restart') is None
    with pytest.raises(RuntimeError, match='ownership'):
        workers.finish_dispatch(path, ids[0], 'wrong-owner', {'status': 'ERROR'})
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM dispatch WHERE state='RUNNING'").fetchone()[0] == 12


@pytest.mark.parametrize('status', ['VERIFIED', 'MISSING', 'PARSE_FAILED', 'AMBIGUOUS', 'collector_exception'])
def test_cdp_status_never_discards_valid_dom_review(status):
    card = _Driver('<div data-review-id="r1"><span class="wiI7pd">fixture</span></div>').find_elements('css selector','[data-review-id]')[0]
    extractor = ReviewExtractor()
    if status == 'collector_exception':
        class Collector:
            def enrich(self, review): raise RuntimeError('offline CDP failure')
        extractor.timestamp_collector = Collector()
    else:
        collector = NetworkTimestampCollector.__new__(NetworkTimestampCollector)
        collector.lock, collector.records, collector.failed = Lock(), {}, status == 'PARSE_FAILED'
        date = datetime(2026, 1, 1, tzinfo=timezone.utc)
        collector.records['r1'] = ReviewTimestamps(date if status == 'VERIFIED' else None,
            date if status == 'VERIFIED' else None, timestamp_status=status)
        extractor.timestamp_collector = collector
    review = extractor.extract(card, 's1')
    assert review.source_review_id == 'r1' and review.store_id == 's1'
    assert review.text == 'fixture'
    assert review.timestamp_status == ('PARSE_FAILED' if status == 'collector_exception' else status)
    if status != 'VERIFIED':
        assert review.published_at is None and review.last_modified_at is None


@pytest.mark.parametrize('trigger,expected', [('limit','max_scrolls'),('deadline','timeout'),('dom_change','stalled')])
def test_pagination_does_not_label_limit_or_missing_cards_as_zero_reviews(trigger, expected):
    paginator = ReviewPaginator(None, None, max_scrolls=5, deadline=10, clock=lambda: 0)
    if trigger == 'limit':paginator.state.scroll_count = 5
    elif trigger == 'deadline':paginator.clock = lambda: 11
    else:paginator.state.non_scrollable = True
    assert paginator.stop_reason() == expected


@pytest.mark.parametrize('first,second,status', [
    (None, None, 'MISSING'), (True, 1700000000000000, 'PARSE_FAILED'),
    (1700000000000001, 1700000000000000, 'AMBIGUOUS'),
    (1700000000000000, 1700000000000001, 'VERIFIED'),
])
def test_timestamp_pairs_preserve_precision_and_reject_uncertain_values(first, second, status):
    value = timestamp_pair(first, second, now=datetime(2026,1,1,tzinfo=timezone.utc))
    assert value.timestamp_status == status
    if status == 'VERIFIED':
        assert (value.last_modified_at - value.published_at).microseconds == 1


def test_atomic_parquet_replace_failure_keeps_outbox_and_recovers(tmp_path, monkeypatch):
    from pathlib import Path
    writer = ParquetReviewWriter(tmp_path/'lake', 'run', '2026-11-04', batch_size=10)
    repo = ReviewRepository(tmp_path/'state.sqlite3', run_id='run', parquet_writer=writer)
    try:
        repo.upsert(_review())
        original = Path.replace
        def fail_replace(self, destination):
            if str(destination).endswith('.parquet'):
                raise OSError('injected replace failure')
            return original(self, destination)
        with monkeypatch.context() as patch:
            patch.setattr(Path, 'replace', fail_replace)
            with pytest.raises(OSError, match='replace failure'):
                repo.flush()
        assert repo.pending_count() == 1
        assert not list((tmp_path/'lake').rglob('*.parquet'))
        assert not list((tmp_path/'lake').rglob('*.tmp'))
        repo.flush()
        assert repo.pending_count() == 0
        assert sum(pq.ParquetFile(f).metadata.num_rows for f in (tmp_path/'lake').rglob('*.parquet')) == 1
    finally:
        repo.close()


def test_saved_smoke_name_variant_requires_branch_address_and_preserves_hours_gap():
    # Only small sanitized fields from the retained smoke are replayed here.
    from crawl_experiment.sources.google_maps.navigator import GoogleMapsNavigator
    from crawl_experiment.sources.google_maps.place_details import extract_place_details
    navigator = GoogleMapsNavigator(None)
    store = Store('source-id', 'Highlands Coffee', 'query', expected_address='182 Bến Vân Đồn')
    candidate = dict(text='Highlands Coffee Ben Van Don HCM', address='182 Bến Vân Đồn')
    assert navigator._best_exact_candidate(store, [candidate]) is candidate
    assert navigator._best_exact_candidate(store, [dict(candidate, address='1182 Bến Vân Đồn')]) is None
    details = extract_place_details(_Driver('<div role="main"><h1>Quán ốc 284</h1><span>Đang mở cửa · Đóng cửa vào 23:00</span><span>Hiện giờ mở cửa trong tuần</span></div>'), 'source-id')
    assert details['next_closing_time']['local_time'] == '23:00'
    assert details['weekly_opening_hours'] == []
    assert details['metadata_completeness']['weekly_hours_complete'] is False


def test_intercepted_review_click_stays_unknown_not_zero_reviews(monkeypatch):
    from crawl_experiment.sources.google_maps.review_surface import ReviewSurface, ReviewSurfaceState
    surface = ReviewSurface(None)
    monkeypatch.setattr(surface, 'classify_state', lambda *args: (ReviewSurfaceState.UNKNOWN, {'cards': 6}))
    def intercepted():
        raise ReviewSurfaceError('element click intercepted by search combobox')
    monkeypatch.setattr(surface, 'open_reviews', intercepted)
    state, pane, evidence = surface.prepare_and_classify()
    assert state == ReviewSurfaceState.UNKNOWN and pane is None
    assert 'click intercepted' in evidence['open_error']
    assert state != ReviewSurfaceState.NO_REVIEWS


def test_cdp_never_enriches_dom_review_with_another_review_id():
    collector = NetworkTimestampCollector.__new__(NetworkTimestampCollector)
    collector.lock, collector.failed = Lock(), False
    date = datetime(2026,1,1,tzinfo=timezone.utc)
    collector.records = {'other-review': ReviewTimestamps(date, date, timestamp_status='VERIFIED')}
    result = collector.enrich(_review())
    assert result.source_review_id == 'r1'
    assert result.timestamp_status == 'MISSING'
    assert result.published_at is None and result.last_modified_at is None
