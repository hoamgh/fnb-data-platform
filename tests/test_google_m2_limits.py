"""Offline bounded collection pilot; real SQLite/outbox/Parquet, mocked browser DOM."""
from types import SimpleNamespace

import pytest

from crawl_experiment.core.models import Review, Store
from crawl_experiment.core.errors import PlaceResolutionError, ReviewParseError
from crawl_experiment.sources.google_maps import crawler as c
from crawl_experiment.sources.google_maps.paginator import ReviewPaginator
from crawl_experiment.sources.google_maps.review_surface import ReviewSurfaceState
from crawl_experiment.storage.review_repository import ReviewRepository
from crawl_experiment.storage.parquet_review_writer import ParquetReviewWriter
from scripts.google_live_workers import reconcile


def pilot(monkeypatch, tmp_path, observations, verified=True, business_status=None, surface_error=None,
          *, failures=None, attempts=None, diagnostics=None, stage_hook=lambda stage: None,
          clock=None, sort_newest=False, surface_state=ReviewSurfaceState.FULL):
    from collections import Counter
    parse_attempts = Counter()
    class Navigator:
        def __init__(self, driver):
            self.last_resolution_diagnostics = {'place_entity_confirmed': verified}
        def open_store(self, store): stage_hook('navigation')

    class Paginator(ReviewPaginator):
        def cards(self):
            index=min(self.state.scroll_count, len(observations)-1)
            return [SimpleNamespace(get_attribute=lambda _, key=key: key) for key in observations[index]]
        def scroll(self):
            self.state.scroll_count += 1
            self.state.non_scrollable = True  # Must not stop on the first idle scroll.
            return {}

    class Surface:
        def __init__(self, driver): pass
        def prepare_and_classify(self):
            if surface_error: raise surface_error
            stage_hook('surface')
            return surface_state, object(), {}
        def sort_newest(self): stage_hook('sort')
        def resolve_review_pane(self): return object()

    class Extractor:
        def extract(self, card, store_id):
            stage_hook('extraction')
            review_id = card.get_attribute('id')
            parse_attempts[review_id] += 1
            if attempts is not None:
                attempts.append(review_id)
            if parse_attempts[review_id] <= (failures or {}).get(review_id, 0):
                raise ReviewParseError('offline incomplete card')
            return Review(source='google_maps', source_review_id=review_id,
                          store_id=store_id, rating=4, text='offline pilot', raw={'provenance':'offline'})

    events=[]
    def activity(event, data):
        events.append(event)
        if diagnostics is not None:
            diagnostics.append((event, data))
    monkeypatch.setattr(c, 'GoogleMapsNavigator', Navigator)
    monkeypatch.setattr(c, 'ReviewPaginator', Paginator)
    monkeypatch.setattr(c, 'ReviewSurface', Surface)
    monkeypatch.setattr(c, 'ReviewExtractor', Extractor)
    monkeypatch.setattr(c, 'extract_place_details', lambda driver, store: {'store_id':store,'business_status':business_status})
    repo=ReviewRepository(tmp_path/'reviews.sqlite3', run_id='pilot',
                          parquet_writer=ParquetReviewWriter(tmp_path/'lake','pilot','2026-11-05'))
    crawler=c.GoogleMapsCrawler(repo, object(), max_scrolls=5, max_reviews=50, idle_limit=2,
                                require_verified_branch=True, timestamp_enrichment=False,
                                **(dict(clock=clock, timeout_seconds=10) if clock else {}),
                                activity_observer=activity)
    def checkpoint(result, stage, *args, **kwargs):
        stage_hook('final_checkpoint' if result.finished_at else f'checkpoint_{stage}')
    monkeypatch.setattr(crawler,'_checkpoint',checkpoint)
    try:
        result=crawler.crawl(Store('pilot','pilot','pilot'), object(), warm_up=False, sort_newest=sort_newest)
    finally:
        repo.close()
    return result, reconcile(tmp_path/'reviews.sqlite3',tmp_path/'lake'), events


def test_fifty_review_cap_even_when_initial_dom_has_more(monkeypatch,tmp_path):
    result, counts, events=pilot(monkeypatch,tmp_path,[[str(i) for i in range(70)]])
    assert result.stop_reason=='max_reviews' and result.scroll_count==0
    assert counts['sqlite_records']==counts['parquet_rows']==50 and counts['pending']==0
    assert events.index('place_details') < events.index('review_surface_classified')


def test_five_scroll_cap(monkeypatch,tmp_path):
    result,counts,_=pilot(monkeypatch,tmp_path,[[str(i)] for i in range(10)])
    assert result.scroll_count==5 and result.stop_reason=='max_scrolls'
    assert counts['sqlite_records']==6


def test_two_consecutive_idle_scrolls(monkeypatch,tmp_path):
    result,counts,_=pilot(monkeypatch,tmp_path,[['r1']])
    assert result.scroll_count==2 and result.stop_reason=='no_new_reviews'
    assert str(result.status)=='PARTIAL_LIMIT' and counts['sqlite_records']==1


def test_idle_streak_resets_on_growth(monkeypatch,tmp_path):
    result,counts,_=pilot(monkeypatch,tmp_path,[['r1'],['r1'],['r1','r2'],['r1','r2'],['r1','r2']])
    assert result.scroll_count==4 and result.stop_reason=='no_new_reviews'
    assert counts['sqlite_records']==2


def test_quota_preserves_dom_order_across_observations_and_duplicate_cards(monkeypatch, tmp_path):
    first = [f'z-{i:02d}' for i in range(49)]
    attempts = []
    result, counts, _ = pilot(monkeypatch, tmp_path,
        [first, first + ['z-last', 'z-last', 'a-lexically-first']], attempts=attempts)
    assert attempts == first + ['z-last']
    assert result.stop_reason == 'max_reviews' and result.scroll_count == 1
    assert counts['sqlite_records'] == counts['parquet_rows'] == 50


@pytest.mark.parametrize('failure_count', [1, 100])
def test_parse_retry_is_bounded_and_does_not_duplicate_events(monkeypatch, tmp_path, failure_count):
    attempts, diagnostics = [], []
    result, counts, _ = pilot(monkeypatch, tmp_path,
        [['z', 'z', 'a'], ['z', 'z', 'a'], ['z', 'a']],
        failures={'z': failure_count}, attempts=attempts, diagnostics=diagnostics)
    assert attempts == ['z', 'a', 'z']  # Never retry a duplicate card in the same observation.
    expected_errors = min(failure_count, 2)
    expected_rows = 2 if failure_count == 1 else 1
    assert result.parse_errors == expected_errors
    assert result.reviews_written == expected_rows
    assert counts['sqlite_records'] == counts['parquet_rows'] == expected_rows
    assert counts['pending'] == counts['duplicate_events'] == 0
    errors = [data for event, data in diagnostics if event == 'review_parse_failed']
    assert [data['attempt'] for data in errors] == list(range(1, expected_errors + 1))
    assert errors[-1]['retry_budget_remaining'] == 2 - expected_errors
    assert result.stop_reason == 'no_new_reviews' and result.scroll_count == 2
    assert str(result.status) == 'PARTIAL_LIMIT'


def test_parse_failure_at_quota_does_not_add_slots_or_scroll_for_retry(monkeypatch, tmp_path):
    ids = [f'z-{i:02d}' for i in range(50)]
    attempts = []
    result, counts, _ = pilot(monkeypatch, tmp_path,
        [ids + [ids[0], 'a-outside-quota']], failures={ids[0]: 1}, attempts=attempts)
    assert attempts == ids
    assert result.parse_errors == 1 and result.reviews_written == 49
    assert counts['sqlite_records'] == counts['parquet_rows'] == 49
    assert result.stop_reason == 'max_reviews' and result.scroll_count == 0
    assert str(result.status) == 'PARTIAL_LIMIT'


def test_retry_does_not_take_another_quota_slot(monkeypatch, tmp_path):
    first = [f'z-{i:02d}' for i in range(49)]
    attempts = []
    result, counts, _ = pilot(monkeypatch, tmp_path,
        [first, first + ['z-last', 'a-outside-quota']],
        failures={first[0]: 1}, attempts=attempts)
    assert attempts == first + [first[0], 'z-last']
    assert len(set(attempts)) == 50 and len(attempts) == 51
    assert counts['sqlite_records'] == counts['parquet_rows'] == 50
    assert result.parse_errors == 1 and result.reviews_written == 50
    assert result.stop_reason == 'max_reviews' and result.scroll_count == 1


def test_permanent_parse_error_does_not_extend_scroll_budget(monkeypatch, tmp_path):
    attempts = []
    result, counts, _ = pilot(monkeypatch, tmp_path,
        [['bad', 'bad', f'new-{i}'] for i in range(10)],
        failures={'bad': 100}, attempts=attempts)
    assert attempts.count('bad') == 2 and result.parse_errors == 2
    assert result.scroll_count == 5 and result.stop_reason == 'max_scrolls'
    assert result.reviews_written == counts['sqlite_records'] == counts['parquet_rows'] == 6


class DeadlineClock:
    def __init__(self): self.now = 0.0
    def __call__(self): return self.now
    def sleep(self, seconds): self.now += seconds


@pytest.mark.parametrize('stage,expected_rows', [
    ('navigation', 0), ('surface', 0), ('sort', 0),
    ('extraction', 1), ('checkpoint_pagination', 2),
])
def test_attempt_deadline_preserves_partial_and_stops_interactions(monkeypatch, tmp_path, stage, expected_rows):
    clock = DeadlineClock(); calls = []; attempts = []
    def advance(name):
        calls.append(name)
        if name == stage: clock.now = 10
    result, counts, _ = pilot(monkeypatch, tmp_path, [['r1', 'r2']],
        clock=clock, stage_hook=advance, sort_newest=True, attempts=attempts)
    assert str(result.status) == 'PARTIAL_TIMEOUT' and result.stop_reason == 'timeout'
    assert result.scroll_count == 0
    assert result.reviews_written == counts['sqlite_records'] == counts['parquet_rows'] == expected_rows
    assert counts['pending'] == 0
    after = calls[calls.index(stage) + 1:]
    assert all(name == 'final_checkpoint' for name in after)
    assert len(attempts) == expected_rows


def test_final_checkpoint_timeout_cannot_return_complete(monkeypatch, tmp_path):
    clock = DeadlineClock(); checkpoints = []
    def advance(stage):
        if stage == 'final_checkpoint':
            checkpoints.append(stage); clock.now = 10
    result, counts, _ = pilot(monkeypatch, tmp_path, [], clock=clock, stage_hook=advance,
                              surface_state=ReviewSurfaceState.NO_REVIEWS)
    assert str(result.status) == 'PARTIAL_TIMEOUT' and result.stop_reason == 'timeout'
    assert counts['sqlite_records'] == 0 and len(checkpoints) == 2


def test_dom_polling_clamps_sleep_and_never_reads_after_expiry():
    from crawl_experiment.sources.google_maps.deadline import StoreAttemptTimeout
    clock = DeadlineClock(); reads = []; waits = []
    pane = SimpleNamespace(find_elements=lambda *args: reads.append(clock()) or [])
    def sleep(seconds): waits.append(seconds); clock.sleep(seconds)
    paginator = ReviewPaginator(None, pane, deadline=0.15, clock=clock, sleeper=sleep,
                                wait_seconds=1, wait_interval=0.1)
    with pytest.raises(StoreAttemptTimeout): paginator._wait_for_dom_growth(set())
    assert sum(waits) == pytest.approx(0.15)
    assert all(at < 0.15 for at in reads)
    assert paginator.state.timed_out and paginator.state.scroll_count == 0


def test_webdriver_timeouts_are_clamped_and_restored():
    from crawl_experiment.sources.google_maps.deadline import AttemptDeadline, StoreAttemptTimeout
    clock = DeadlineClock(); settings = []
    driver = SimpleNamespace(timeouts=SimpleNamespace(page_load=30, script=15, implicit_wait=0),
        set_page_load_timeout=lambda value: settings.append(('page', value)),
        set_script_timeout=lambda value: settings.append(('script', value)),
        implicitly_wait=lambda value: settings.append(('implicit', value)))
    budget = AttemptDeadline(10, clock); budget.configure(driver)
    assert settings == [('page', 10), ('script', 10), ('implicit', 0)]
    clock.now = 9; budget.prepare()
    assert settings[-3:] == [('page', 1), ('script', 1), ('implicit', 0)]
    clock.now = 10; before = list(settings)
    with pytest.raises(StoreAttemptTimeout): budget.prepare()
    assert settings == before
    budget.restore()
    assert settings[-3:] == [('page', 30), ('script', 15), ('implicit', 0)]


@pytest.mark.parametrize('outcome', ['success', 'failure'])
def test_sort_budget_exhausted_does_not_retry(monkeypatch, outcome):
    from crawl_experiment.sources.google_maps.deadline import AttemptDeadline, StoreAttemptTimeout
    from crawl_experiment.sources.google_maps.review_surface import ReviewSurface
    from crawl_experiment.core.errors import SortError
    clock = DeadlineClock(); calls = []
    surface = ReviewSurface(object(), clock=clock, sleeper=clock.sleep)
    AttemptDeadline(1, clock).bind(surface)
    def sort_once():
        calls.append('sort'); clock.now = 1
        if outcome == 'failure': raise SortError('not ready')
    monkeypatch.setattr(surface, '_sort_newest_once', sort_once)
    monkeypatch.setattr(surface, 'open_reviews', lambda: calls.append('reentry'))
    with pytest.raises(StoreAttemptTimeout): surface.sort_newest()
    assert calls == ['sort']


def test_first_sort_success_has_no_retry_or_reentry(monkeypatch):
    from crawl_experiment.sources.google_maps.review_surface import ReviewSurface
    calls = []
    surface = ReviewSurface(object())
    monkeypatch.setattr(surface, '_sort_newest_once', lambda: calls.append('sort'))
    monkeypatch.setattr(surface, 'open_reviews', lambda: calls.append('reentry'))
    surface.sort_newest()
    assert calls == ['sort']


@pytest.mark.parametrize('terminal', ['AUTH_REQUIRED', 'LIMITED', 'challenge', 'rate_limit'])
def test_sort_recovery_does_not_retry_terminal_evidence(monkeypatch, terminal):
    from crawl_experiment.sources.google_maps.review_surface import ReviewSurface
    from crawl_experiment.core.errors import SortError, AuthRequiredError, LimitedReviewViewError, ChallengeError, RateLimitedError
    calls = []
    url = 'https://google.com/' + {'challenge': 'captcha', 'rate_limit': 'sorry/'}.get(terminal, '')
    surface = ReviewSurface(SimpleNamespace(current_url=url, find_elements=lambda *args: []))
    def fail(): calls.append('sort'); raise SortError('menu unavailable')
    monkeypatch.setattr(surface, '_sort_newest_once', fail)
    monkeypatch.setattr(surface, 'classify_state', lambda pane: (getattr(ReviewSurfaceState, terminal, ReviewSurfaceState.UNKNOWN), {}))
    expected = {'AUTH_REQUIRED': AuthRequiredError, 'LIMITED': LimitedReviewViewError,
                'challenge': ChallengeError, 'rate_limit': RateLimitedError}[terminal]
    with pytest.raises(expected): surface.sort_newest()
    assert calls == ['sort']


def test_normal_click_failure_keeps_action_chain_fallback(monkeypatch):
    from crawl_experiment.sources.google_maps import review_surface as module
    calls = []
    def click(): calls.append('normal'); raise RuntimeError('intercepted')
    class Actions:
        def __init__(self, driver): pass
        def move_to_element(self, button): return self
        def click(self): return self
        def perform(self): calls.append('action_chains')
    surface = module.ReviewSurface(object())
    monkeypatch.setattr(module, 'ActionChains', Actions)
    monkeypatch.setattr(surface, '_interactive_sort_control_confirmed', lambda button: True)
    monkeypatch.setattr(surface, '_wait_for_sort_menu', lambda: 'action_chains' in calls)
    monkeypatch.setattr(surface, '_raise_terminal_access', lambda: None)
    opened, attempted = surface._open_menu_with_bounded_clicks(SimpleNamespace(click=click))
    assert opened and attempted == calls == ['normal', 'action_chains']


def test_navigation_deadline_is_not_wrapped_as_retryable_navigation_failure():
    from crawl_experiment.sources.google_maps.deadline import AttemptDeadline, StoreAttemptTimeout
    from crawl_experiment.sources.google_maps.navigator import GoogleMapsNavigator
    clock = DeadlineClock(); calls = []
    def get(url): calls.append(url); clock.now = 1
    nav = GoogleMapsNavigator(SimpleNamespace(get=get), clock=clock, sleeper=clock.sleep)
    AttemptDeadline(1, clock).bind(nav)
    with pytest.raises(StoreAttemptTimeout): nav.open_store(Store('id', 'Cafe', 'Cafe'))
    assert len(calls) == 1


def test_extractor_deadline_stops_next_dom_read_without_parse_retry():
    from crawl_experiment.sources.google_maps.deadline import AttemptDeadline, StoreAttemptTimeout
    from crawl_experiment.sources.google_maps.extractor import ReviewExtractor
    clock = DeadlineClock(); calls = []
    def find(*args): calls.append(args); clock.now = 1; return []
    card = SimpleNamespace(get_attribute=lambda name: 'review', find_elements=find)
    extractor = ReviewExtractor()
    extractor.check_deadline = AttemptDeadline(1, clock).check
    with pytest.raises(StoreAttemptTimeout): extractor.extract(card, 'store')
    assert len(calls) == 1


def test_campaign_signature_detects_deadline_only_change(monkeypatch):
    import hashlib
    from scripts import google_m2_batch as batch
    before = batch.campaign_code_fingerprints()
    deadline = batch.ROOT/'crawl_experiment/sources/google_maps/deadline.py'
    original = batch.e.sha
    # Simulate edited bytes without changing the actual working tree or evidence.
    monkeypatch.setattr(batch.e, 'sha', lambda path:
        hashlib.sha256(path.read_bytes() + b'\n# changed deadline\n').hexdigest()
        if path == deadline else original(path))
    after = batch.campaign_code_fingerprints()
    key = str(deadline.relative_to(batch.ROOT))
    assert key in before
    assert [name for name in before if before[name] != after[name]] == [key]
    assert dict(inputs={}, limits=batch.POLICY, code_sha256=before) != dict(
        inputs={}, limits=batch.POLICY, code_sha256=after)


def test_unverified_branch_never_opens_reviews(monkeypatch,tmp_path):
    with pytest.raises(PlaceResolutionError,match='verification'):
        pilot(monkeypatch,tmp_path,[['r1']],verified=False)
    assert reconcile(tmp_path/'reviews.sqlite3',tmp_path/'lake')['sqlite_records']==0


def test_permanent_closure_still_collects_available_reviews(monkeypatch,tmp_path):
    result,counts,_=pilot(monkeypatch,tmp_path,[['r1','r2']],business_status='CLOSED_PERMANENTLY')
    assert counts['sqlite_records']==counts['parquet_rows']==2
    assert result.business_status=='CLOSED_PERMANENTLY'


def test_permanent_closure_missing_reviews_completes_without_dlq_classification(monkeypatch,tmp_path):
    from crawl_experiment.core.errors import ReviewSurfaceError
    from scripts.google_live_workers import closed_metadata_completed,stop_scope,crawl_outcome
    from dataclasses import asdict
    result,counts,_=pilot(monkeypatch,tmp_path,[],business_status='CLOSED_PERMANENTLY',
                          surface_error=ReviewSurfaceError('Reviews tab not found'))
    row=asdict(result)
    assert str(result.status)=='COMPLETE' and counts['sqlite_records']==0
    assert result.review_collection_status=='UNAVAILABLE_CLOSED_PERMANENTLY'
    assert closed_metadata_completed(row) and crawl_outcome(row)=='SUCCESS'
    assert stop_scope(dict(row,final_access_state='UNKNOWN')) is None
    assert stop_scope(dict(row,final_access_state='RATE_LIMITED'))=='WORKER'


@pytest.mark.parametrize('business_status',['OPERATIONAL','CLOSED_TEMPORARILY',None])
def test_other_business_status_does_not_hide_review_failure(monkeypatch,tmp_path,business_status):
    from crawl_experiment.core.errors import ReviewSurfaceError
    with pytest.raises(ReviewSurfaceError):
        pilot(monkeypatch,tmp_path,[],business_status=business_status,
              surface_error=ReviewSurfaceError('Reviews tab not found'))


def test_permanent_closure_does_not_hide_rate_limit(monkeypatch,tmp_path):
    from crawl_experiment.core.errors import RateLimitedError
    with pytest.raises(RateLimitedError):
        pilot(monkeypatch,tmp_path,[],business_status='CLOSED_PERMANENTLY',
              surface_error=RateLimitedError('HTTP 429'))


def test_closed_completion_artifacts_do_not_emit_dlq(tmp_path):
    import json
    from crawl_experiment.core.models import CrawlResult
    from crawl_experiment.core.statuses import CrawlStatus
    from crawl_experiment.observability.run_artifacts import CrawlArtifacts
    run=tmp_path/'run';(run/'metadata').mkdir(parents=True);(run/'checkpoints').mkdir()
    store=Store('closed-store','Quán Khánh Hào Kin Sushi','offline')
    artifacts=CrawlArtifacts(run,'2026-11-05T00:00:00+00:00',stores=[store],state_database=tmp_path/'state.sqlite3')
    result=c.GoogleMapsCrawler._closed_completed(
        CrawlResult(store.id,CrawlStatus.ERROR,business_status='CLOSED_PERMANENTLY'),'Reviews tab unavailable')
    artifacts.on_result(store,result,1)
    assert not artifacts.dlq_path.read_text().strip()
    metadata=json.loads((run/'metadata'/f'{store.id}.json').read_text())
    assert metadata['status']=='COMPLETE' and metadata['dlq'] is False
    assert metadata['business_status']=='CLOSED_PERMANENTLY'


def test_normal_unresolved_and_zero_reviews_are_not_dlq(tmp_path):
    from crawl_experiment.core.models import CrawlResult
    from crawl_experiment.core.statuses import CrawlStatus
    from crawl_experiment.observability.run_artifacts import CrawlArtifacts
    from crawl_experiment.orchestration.retry_policy import RetryDecision,RetryAction
    run=tmp_path/'run';(run/'metadata').mkdir(parents=True);(run/'checkpoints').mkdir()
    store=Store('unresolved','PatisStory - Chuyện bánh','offline')
    artifacts=CrawlArtifacts(run,'2026-11-05T00:00:00+00:00',stores=[store],state_database=tmp_path/'state.sqlite3')
    artifacts.on_terminal_failure(store,RetryDecision(RetryAction.STOP,CrawlStatus.PLACE_RESOLUTION_FAILED),
                                  1,PlaceResolutionError('No sufficiently relevant candidate'))
    assert not artifacts.dlq_path.read_text().strip()
    artifacts.on_result(store,CrawlResult(store.id,CrawlStatus.NO_REVIEWS),1)
    assert not artifacts.dlq_path.read_text().strip()


@pytest.mark.parametrize('case,expected', [('valid',True),('old_policy',False),('unverified',False),
                                         ('pending_outbox',False),('too_many',False),('wrong_input',False)])
def test_reuse_requires_verified_reconciled_matching_policy(monkeypatch,tmp_path,case,expected):
    from scripts import google_m2_batch as batch
    from scripts.google_overture_workers import write_json
    worker=tmp_path/'worker-01'; job=worker/'job-000'; run=job/'crawl'/'run'
    (run/'metadata').mkdir(parents=True)
    place={'place_key':'overture:source-id','name':'source name'}
    write_json(job/'input.json',{'overture':place if case!='wrong_input' else {}})
    write_json(worker/'plan.json',dict(batch.POLICY,max_reviews=100 if case=='old_policy' else 50))
    write_json(run/'summary.json',{'stores':[{'status':'PARTIAL_LIMIT','scroll_count':5}]})
    write_json(run/'metadata'/'store.json',{'place_entity_confirmed':case!='unverified'})
    write_json(run/'metadata'/'store.place_details.json',{'schema_version':2})
    monkeypatch.setattr(batch.workers,'reconcile',lambda *args:dict(
        pending=1 if case=='pending_outbox' else 0,sqlite_records=51 if case=='too_many' else 50))
    assert (batch.verified_result(job,place) is not None) == expected
