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
import pyarrow as pa
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


@pytest.mark.parametrize('damage', ['payload', 'swapped', 'missing_column', 'wrong_type', 'data_page'])
def test_replay_validates_payload_before_ack(tmp_path, damage):
    from dataclasses import replace
    writer = ParquetReviewWriter(tmp_path/'lake', 'run', '2026-11-04', batch_size=10)
    repo = ReviewRepository(tmp_path/'state.sqlite3', run_id='run', parquet_writer=writer)
    try:
        repo.upsert(_review())
        repo.upsert(replace(_review(), source_review_id='r2', text='other payload'))
        pending = list(repo._db.execute(
            'SELECT event_id,payload_json FROM pending_review_events ORDER BY created_at,event_id'))
        path, _ = writer.write_batch([(row[0], json.loads(row[1])) for row in pending])
        table = pq.ParquetFile(path).read()
        if damage == 'data_page':
            column = pq.ParquetFile(path).metadata.row_group(0).column(0)
            raw = bytearray(path.read_bytes())
            offset = column.data_page_offset
            raw[offset:offset + 16] = b'\xff' * 16
            path.write_bytes(raw)
            assert pq.ParquetFile(path).metadata.num_rows == 2  # Footer remains readable.
        else:
            if damage == 'payload':
                rows = table.to_pylist(); rows[0]['review_text'] = 'altered'
                damaged = pa.Table.from_pylist(rows, schema=table.schema)
            elif damage == 'swapped':
                damaged = table.take(pa.array([1, 0]))  # Keep original footer membership order.
            elif damage == 'missing_column':
                damaged = table.drop(['review_text'])
            else:
                index = table.schema.get_field_index('rating')
                damaged = table.set_column(index, 'rating', table['rating'].cast(pa.string()))
            pq.write_table(damaged, path)
        before = path.read_bytes()
        with pytest.raises((RuntimeError, pa.ArrowException, OSError)):
            repo.flush()
        assert repo.pending_count() == 2
        assert [tuple(row) for row in repo._db.execute(
            'SELECT event_id,payload_json FROM pending_review_events ORDER BY created_at,event_id')
        ] == [tuple(row) for row in pending]
        assert path.read_bytes() == before
    finally:
        repo._db.close()  # Do not retry the intentionally corrupt fixture during cleanup.


def test_replay_uses_publication_serialization_contract(tmp_path):
    from dataclasses import replace
    writer = ParquetReviewWriter(tmp_path/'lake', 'run', '2026-11-04', batch_size=10)
    repo = ReviewRepository(tmp_path/'state.sqlite3', run_id='run', parquet_writer=writer)
    instant = datetime(2026, 11, 4, tzinfo=timezone.utc)
    try:
        repo.upsert(replace(_review(), published_at=instant, last_modified_at=instant,
                            timestamp_status='VERIFIED', image_urls=['https://example.invalid/image']))
        events = [(row[0], json.loads(row[1])) for row in repo._db.execute(
            'SELECT event_id,payload_json FROM pending_review_events ORDER BY created_at,event_id')]
        path, _ = writer.write_batch(events)
        before = path.read_bytes()
        repo.flush()
        assert repo.pending_count() == 0 and path.read_bytes() == before
        assert len(list((tmp_path/'lake').rglob('*.parquet'))) == 1
    finally:
        repo.close()


@pytest.mark.parametrize('damage', ['one_file', 'all_files', 'different_identity', 'content'])
def test_reconciliation_rejects_missing_or_altered_reviews(tmp_path, damage):
    from dataclasses import replace
    database, lake = tmp_path/'state.sqlite3', tmp_path/'lake'
    repo = ReviewRepository(database, run_id='run', parquet_writer=ParquetReviewWriter(
        lake, 'run', '2026-11-04', batch_size=1))
    repo.upsert(_review())
    repo.upsert(replace(_review(), source_review_id='r2'))
    repo.close()
    files = sorted(lake.rglob('*.parquet'))
    if damage in {'one_file', 'all_files'}:
        for path in files[:1] if damage == 'one_file' else files:
            path.unlink()  # Only pytest temporary fixtures.
    else:
        table = pq.ParquetFile(files[0]).read(); rows = table.to_pylist()
        rows[0]['source_review_id' if damage == 'different_identity' else 'review_text'] = 'different'
        pq.write_table(pa.Table.from_pylist(rows, schema=table.schema), files[0])
    with pytest.raises(RuntimeError, match='coverage|identities|content hash'):
        workers.reconcile(database, lake)


@pytest.mark.parametrize('rating', [4, 4.0, 4.5, None])
def test_reconciliation_allows_updates_and_distinguishes_unpublished_state(tmp_path, rating):
    from dataclasses import replace
    database, lake = tmp_path/'state.sqlite3', tmp_path/'lake'
    repo = ReviewRepository(database, run_id='run', parquet_writer=ParquetReviewWriter(
        lake, 'run', '2026-11-04', batch_size=10))
    review = replace(_review(), rating=rating)
    try:
        empty = workers.reconcile(database, lake)
        assert empty['sqlite_records'] == empty['parquet_rows'] == 0
        repo.upsert(review)
        pending = workers.reconcile(database, lake)
        assert pending['pending'] == pending['unpublished_review_identities'] == 1
        repo.upsert_review_state(replace(review, text='updated once'))
        repo.upsert_review_state(replace(review, text='updated twice'))
        repo.flush()
        complete = workers.reconcile(database, lake)
        assert complete['sqlite_records'] == complete['persisted_review_identities'] == 1
        assert complete['parquet_rows'] == 3 and complete['pending'] == 0
        assert complete['unpublished_review_identities'] == 0
        assert 'full_event_history_not_proven' in complete['integrity_scope']
        assert not repo.upsert(replace(review, text='updated twice'))
        assert workers.reconcile(database, lake)['parquet_rows'] == 3
    finally:
        repo.close()


def test_reconciliation_rejects_state_only_capture_without_fabricating_recovery(tmp_path):
    database, lake = tmp_path/'state.sqlite3', tmp_path/'lake'
    repo = ReviewRepository(database)
    repo.upsert(_review())
    repo.close()
    with pytest.raises(RuntimeError, match='without pending evidence'):
        workers.reconcile(database, lake)


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


@pytest.fixture
def metadata_repo(tmp_path):
    writer = ParquetReviewWriter(tmp_path/'lake', 'metadata', '2026-11-10')
    repo = ReviewRepository(tmp_path/'state.sqlite3', run_id='metadata', parquet_writer=writer,
                            clock=lambda: '2026-11-10T00:00:00+00:00')
    try:
        yield repo
    finally:
        repo.close()


def _verified_review(day=2, **changes):
    from dataclasses import replace
    return replace(_review(), rating=4.0, timestamp_status='VERIFIED',
                   timestamp_source='google_maps_network',
                   published_at=datetime(2026, 11, 1, tzinfo=timezone.utc),
                   last_modified_at=datetime(2026, 11, day, tzinfo=timezone.utc), **changes)


def _pending_payloads(repo):
    return [json.loads(row[0]) for row in repo._db.execute(
        'SELECT payload_json FROM pending_review_events ORDER BY rowid')]


def test_metadata_timestamp_upgrade_increase_and_no_downgrade(metadata_repo):
    from dataclasses import replace
    from crawl_experiment.storage.review_repository import ReviewChange
    repo = metadata_repo
    missing = replace(_review(), rating=4.0)
    assert repo.upsert_review_state(missing) is ReviewChange.INSERT
    assert repo.upsert_review_state(_verified_review()) is ReviewChange.UPDATE
    assert repo.upsert_review_state(_verified_review()) is ReviewChange.UNCHANGED
    assert repo.upsert_review_state(_verified_review(3)) is ReviewChange.UPDATE
    for status in ('MISSING', 'PARSE_FAILED', 'AMBIGUOUS'):
        assert repo.upsert_review_state(replace(missing, timestamp_status=status)) is ReviewChange.UNCHANGED
    assert repo.pending_count() == 3
    state = repo.get_review_state(*missing.identity)
    assert state['timestamp_status'] == 'VERIFIED'
    assert state['last_modified_at'] == _verified_review(3).last_modified_at.isoformat()
    payloads = _pending_payloads(repo)
    assert [row['timestamp_status'] for row in payloads] == ['MISSING', 'VERIFIED', 'VERIFIED']
    assert len({row['content_hash'] for row in payloads}) == 1
    assert [row['change_type'] for row in payloads] == ['INSERT', 'UPDATE', 'UPDATE']


@pytest.mark.parametrize('field,value', [
    ('review_url', 'https://example.invalid/review'), ('reviewer_id', 'reviewer-1'),
    ('reviewer_profile_url', 'https://example.invalid/profile'),
    ('reviewer_avatar_url', 'https://example.invalid/avatar'),
    ('reviewer_review_count', 12), ('reviewer_photo_count', 4), ('reviewer_is_local_guide', True),
])
def test_meaningful_metadata_update_without_hash_change_or_spam(metadata_repo, field, value):
    from dataclasses import replace
    from crawl_experiment.storage.review_repository import ReviewChange
    repo = metadata_repo
    original = _verified_review()
    changed = replace(original, **{field: value})
    repo.upsert_review_state(original)
    assert repo.upsert_review_state(changed) is ReviewChange.UPDATE
    for _ in range(3):
        assert repo.upsert_review_state(changed) is ReviewChange.UNCHANGED
    noisy = replace(changed, displayed_date='a week ago', reviewer_metadata_raw='changed UI text')
    assert repo.upsert_review_state(noisy) is ReviewChange.UNCHANGED
    payloads = _pending_payloads(repo)
    assert len(payloads) == 2 and payloads[-1][field] == value
    assert payloads[0]['content_hash'] == payloads[1]['content_hash']


def test_metadata_updates_at_identical_clock_do_not_collide(metadata_repo):
    from dataclasses import replace
    repo = metadata_repo
    for url in (None, 'https://example.invalid/one', 'https://example.invalid/two'):
        repo.upsert_review_state(replace(_verified_review(), review_url=url))
    assert repo.pending_count() == 3
    assert len({row[0] for row in repo._db.execute('SELECT event_id FROM pending_review_events')}) == 3


@pytest.mark.parametrize('mode', ['stale', 'missing', 'same_time', 'published_conflict'])
def test_conflicting_observation_preserves_current_and_own_parquet_timestamps(metadata_repo, tmp_path, mode):
    from dataclasses import replace
    from crawl_experiment.storage.review_repository import ReviewChange, review_content_hash
    from fnb_platform import google_reviews_silver as silver
    repo = metadata_repo
    current = _verified_review(3, text='new content')
    if mode == 'missing':
        incoming = replace(_review(), rating=4.0, text='uncertain content')
    elif mode == 'published_conflict':
        incoming = replace(_verified_review(4, text='conflicting publication'),
                           published_at=datetime(2026, 11, 2, tzinfo=timezone.utc))
    else:
        incoming = _verified_review(2 if mode == 'stale' else 3, text='old or conflicting content')
    repo.upsert_review_state(current, observed_at='2026-11-09T00:00:00+00:00')
    assert repo.upsert_review_state(incoming) is ReviewChange.UPDATE
    assert repo.upsert_review_state(incoming) is ReviewChange.UNCHANGED
    state = repo.get_review_state(*current.identity)
    assert state['content_hash'] == review_content_hash(current)
    assert state['last_modified_at'] == current.last_modified_at.isoformat()
    assert 'CURRENT_STATE_NOT_ADVANCED' in json.loads(state['change_diagnostics'])
    payload = _pending_payloads(repo)[-1]
    assert payload['review_text'] == incoming.text
    assert payload['last_modified_at'] == (incoming.last_modified_at.isoformat() if mode != 'missing' else None)
    assert payload['timestamp_status'] == incoming.timestamp_status
    repo.flush()
    bridge = dict(binding_status='BOUND', crawler_store_id='s1', google_id_kind='data_id',
                  google_identity_value='0x1:0x1', bridge_id='fixture', source_run='fixture',
                  job_ref='fixture-job', capture_ref='fixture-capture')
    events = []
    for path in (tmp_path/'lake').rglob('*.parquet'):
        reader = pq.ParquetFile(path)
        ids = json.loads(reader.metadata.metadata[b'review_event_ids'])
        for index, row in enumerate(reader.read().to_pylist()):
            normalized = silver.normalize_event(row, bridge, path, index, ids[index], 'fixture-sha')
            events.append((normalized, silver._digest(row)))
    observations, selected = silver.resolve_events(events)
    assert len(observations) == 2 and len(selected) == 1
    assert selected[0]['review_text'] == current.text
    assert selected[0]['last_modified_at'] == current.last_modified_at
    rejected = next(row for row in observations if row['review_text'] == incoming.text)
    assert not rejected['current_eligible'] and 'CURRENT_STATE_NOT_ADVANCED' in rejected['dq_flags']


def test_reopen_replays_metadata_events_and_preserves_dedup_state(tmp_path):
    from crawl_experiment.storage.review_repository import ReviewChange
    path, lake = tmp_path/'state.sqlite3', tmp_path/'lake'
    repo = ReviewRepository(path, run_id='old', parquet_writer=ParquetReviewWriter(lake, 'old', '2026-11-10'))
    repo.upsert_review_state(_review())
    # Same numeric representation as the original content hash.
    from dataclasses import replace
    verified = replace(_verified_review(), rating=4)
    repo.upsert_review_state(verified)
    before = [tuple(row) for row in repo._db.execute('SELECT event_id,payload_json FROM pending_review_events')]
    repo._db.close()  # Simulate exit before flush, not a hard-crash durability claim.
    reopened = ReviewRepository(path, run_id='new', parquet_writer=ParquetReviewWriter(lake, 'new', '2026-11-10'))
    try:
        assert reopened.pending_count() == 0
        assert reopened.upsert_review_state(verified) is ReviewChange.UNCHANGED
        events = []
        for file in lake.rglob('*.parquet'):
            events.extend(json.loads(pq.ParquetFile(file).metadata.metadata[b'review_event_ids']))
        assert set(events) == {row[0] for row in before} and len(events) == 2
        assert reopened.get_review_state(*verified.identity)['timestamp_status'] == 'VERIFIED'
    finally:
        reopened.close()


def test_nullable_metadata_migration_preserves_existing_pending_events(tmp_path):
    from crawl_experiment.storage.review_repository import ReviewChange
    path = tmp_path/'legacy.sqlite3'
    repo = ReviewRepository(path, run_id='old', parquet_writer=ParquetReviewWriter(tmp_path/'lake', 'old', '2026-11-10'))
    repo.upsert_review_state(_verified_review())
    pending = [tuple(row) for row in repo._db.execute('SELECT * FROM pending_review_events')]
    repo._db.execute('ALTER TABLE review_state DROP COLUMN metadata_json')
    repo._db.execute('ALTER TABLE review_state DROP COLUMN last_event_fingerprint')
    repo._db.commit(); repo._db.close()
    migrated = ReviewRepository(path, run_id='next')  # No writer: inspect migration without flushing.
    try:
        assert [tuple(row) for row in migrated._db.execute('SELECT * FROM pending_review_events')] == pending
        state = migrated.get_review_state('google_maps', 'r1')
        assert state['metadata_json'] is None and state['last_event_fingerprint'] is None
        # Legacy metadata is unknown: establish one baseline, then no repeat spam.
        assert migrated.upsert_review_state(_verified_review()) is ReviewChange.UPDATE
        assert migrated.upsert_review_state(_verified_review()) is ReviewChange.UNCHANGED
    finally:
        migrated.close()


def test_newer_verified_content_advances_state(metadata_repo):
    from crawl_experiment.storage.review_repository import ReviewChange, review_content_hash
    repo = metadata_repo
    original, newer = _verified_review(2), _verified_review(3, text='verified new content')
    repo.upsert_review_state(original)
    assert repo.upsert_review_state(newer) is ReviewChange.UPDATE
    state = repo.get_review_state(*newer.identity)
    assert state['content_hash'] == review_content_hash(newer)
    assert state['last_modified_at'] == newer.last_modified_at.isoformat()
    assert 'CURRENT_STATE_NOT_ADVANCED' not in _pending_payloads(repo)[-1]['change_diagnostics']
    assert repo.upsert_review_state(newer) is ReviewChange.UNCHANGED


def test_unverified_metadata_gain_is_evidence_not_verified_current_downgrade(metadata_repo):
    from dataclasses import replace
    from crawl_experiment.storage.review_repository import ReviewChange
    repo = metadata_repo
    original = _verified_review()
    incoming = replace(original, published_at=None, last_modified_at=None,
                       timestamp_status='MISSING', timestamp_source=None,
                       review_url='https://example.invalid/new-evidence')
    repo.upsert_review_state(original)
    assert repo.upsert_review_state(incoming) is ReviewChange.UPDATE
    assert repo.upsert_review_state(incoming) is ReviewChange.UNCHANGED
    row = _pending_payloads(repo)[-1]
    assert row['review_url'] == incoming.review_url and row['last_modified_at'] is None
    assert row['timestamp_status'] == 'MISSING'
    assert 'CURRENT_STATE_NOT_ADVANCED' in row['change_diagnostics']
    state = repo.get_review_state(*original.identity)
    assert state['timestamp_status'] == 'VERIFIED'
    assert state['last_modified_at'] == original.last_modified_at.isoformat()


def test_reactivation_reuses_locked_current_row_without_duplicate_select(metadata_repo):
    repo = metadata_repo
    review = _verified_review()
    repo.upsert_review_state(review)
    with repo._db:
        repo._db.execute('UPDATE review_state SET is_active=0,miss_count=3')
    statements = []
    repo._db.set_trace_callback(statements.append)
    repo.upsert_review_state(review)
    repo._db.set_trace_callback(None)
    reads = [sql for sql in statements if sql.upper().startswith('SELECT') and 'review_state' in sql]
    assert len(reads) == 1
    assert repo.consume_observed_previous(review.source_review_id) == (3, False)
    assert repo.consume_reactivation(review.source_review_id)
    assert repo.reactivation_previous_miss_count(review.source_review_id) == 3
    assert repo.get_review_state(*review.identity)['is_active'] == 1
