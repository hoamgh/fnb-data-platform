"""Offline bounded collection pilot; real SQLite/outbox/Parquet, mocked browser DOM."""
from types import SimpleNamespace

import pytest

from crawl_experiment.core.models import Review, Store
from crawl_experiment.core.errors import PlaceResolutionError
from crawl_experiment.sources.google_maps import crawler as c
from crawl_experiment.sources.google_maps.paginator import ReviewPaginator
from crawl_experiment.sources.google_maps.review_surface import ReviewSurfaceState
from crawl_experiment.storage.review_repository import ReviewRepository
from crawl_experiment.storage.parquet_review_writer import ParquetReviewWriter
from scripts.google_live_workers import reconcile


def pilot(monkeypatch, tmp_path, observations, verified=True, business_status=None, surface_error=None):
    class Navigator:
        def __init__(self, driver):
            self.last_resolution_diagnostics = {'place_entity_confirmed': verified}
        def open_store(self, store): pass

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
            return ReviewSurfaceState.FULL, object(), {}

    class Extractor:
        def extract(self, card, store_id):
            return Review(source='google_maps', source_review_id=card.get_attribute('id'),
                          store_id=store_id, rating=4, text='offline pilot', raw={'provenance':'offline'})

    events=[]
    monkeypatch.setattr(c, 'GoogleMapsNavigator', Navigator)
    monkeypatch.setattr(c, 'ReviewPaginator', Paginator)
    monkeypatch.setattr(c, 'ReviewSurface', Surface)
    monkeypatch.setattr(c, 'ReviewExtractor', Extractor)
    monkeypatch.setattr(c, 'extract_place_details', lambda driver, store: {'store_id':store,'business_status':business_status})
    repo=ReviewRepository(tmp_path/'reviews.sqlite3', run_id='pilot',
                          parquet_writer=ParquetReviewWriter(tmp_path/'lake','pilot','2026-11-05'))
    crawler=c.GoogleMapsCrawler(repo, object(), max_scrolls=5, max_reviews=50, idle_limit=2,
                                require_verified_branch=True, timestamp_enrichment=False,
                                activity_observer=lambda event, data: events.append(event))
    monkeypatch.setattr(crawler,'_checkpoint',lambda *args, **kwargs:None)
    try:
        result=crawler.crawl(Store('pilot','pilot','pilot'), object(), warm_up=False, sort_newest=False)
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
