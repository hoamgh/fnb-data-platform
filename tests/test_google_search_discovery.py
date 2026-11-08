"""Bounded list discovery is deliberately broader than branch verification."""
from types import SimpleNamespace

import pytest

from crawl_experiment.core.models import Store
from crawl_experiment.core.errors import PlaceResolutionError, RateLimitedError
from crawl_experiment.sources.google_maps import selectors
from crawl_experiment.sources.google_maps.navigator import GoogleMapsNavigator


def candidate(name='Hồng Trà Ngô Gia',suffix='one',address=None):
    return dict(text=name,address=address,href='https://www.google.com/maps/place/'+suffix)


def test_card_parent_address_not_only_link_data_address():
    link=SimpleNamespace(text='',get_attribute=lambda key:{'href':candidate()['href'],'aria-label':'Hồng Trà Ngô Gia'}.get(key))
    driver=SimpleNamespace(find_elements=lambda *a:[link],
        execute_script=lambda *a:dict(text='Hồng Trà Ngô Gia\nTrà sữa\n141 Tôn Đản',address=None))
    result=GoogleMapsNavigator(driver)._candidate_details()
    assert result[0]['address']=='141 Tôn Đản'


def test_missing_card_address_can_be_inspected_but_is_not_a_confirmed_match():
    nav=GoogleMapsNavigator(None)
    store=Store('source','Hồng Trà Ngô Gia','query',expected_address='141 Tôn Đản')
    item=candidate()
    assert nav._best_exact_candidate(store,[item]) is item
    assert nav._rank_inspection_candidates(store,[item])==[item]
    assert nav._rank_inspection_candidates(store,[candidate('Ốc Loan')])==[]


class Clock:
    now=0
    def __call__(self):return self.now
    def sleep(self,seconds):self.now+=seconds


def setup(monkeypatch,*,verify=True,rate_limit=False,max_opens=3):
    clock=Clock(); opened=[];scrolls=[]
    initial='https://www.google.com/maps/search/query/'
    class Driver:
        current_url=initial
        def get(self,url):self.current_url=url;opened.append(url)
        def find_elements(self,*args):
            return ([object()] if self.current_url==initial else []) if args[-1]==selectors.SEARCH_RESULTS_FEED else [object()]
        def execute_script(self,script,*args):scrolls.append(script)
    driver=Driver()
    nav=GoogleMapsNavigator(driver,max_search_scrolls=2,max_candidate_opens=max_opens,
        resolve_timeout_seconds=1,clock=clock,sleeper=clock.sleep)
    items=[candidate(suffix='wrong',address='143 Tôn Đản'),candidate(suffix='right',address='141 Tôn Đản')]
    def access():
        if rate_limit and '/place/' in driver.current_url:raise RateLimitedError('Google rate limit page')
        return driver.current_url,SimpleNamespace(has_place_shell=True)
    monkeypatch.setattr(nav,'_check_access',access)
    monkeypatch.setattr(nav,'_candidate_details',lambda:items if len(scrolls)>=2 else [])
    monkeypatch.setattr(nav,'_place_title',lambda:'Hồng Trà Ngô Gia')
    monkeypatch.setattr(nav,'_place_address',lambda:'141 Tôn Đản' if verify and driver.current_url.endswith('/right') else '143 Tôn Đản')
    return nav,opened,scrolls


def test_rank_all_candidates_then_open_only_best(monkeypatch):
    nav,opened,scrolls=setup(monkeypatch)
    store=Store('source','Hồng Trà Ngô Gia','query',expected_address='141 Tôn Đản')
    assert nav.open_store(store).endswith('/right')
    assert len(scrolls)==2
    assert [url.rsplit('/',1)[-1] for url in opened[1:]]==['right']
    assert nav.last_resolution_diagnostics['identity_status']=='IDENTITY_UNVERIFIED'
    assert len(nav.last_resolution_diagnostics['candidate_rankings'])==2


def test_bounded_exhaustion_does_not_force_match(monkeypatch):
    nav,opened,scrolls=setup(monkeypatch,verify=False,max_opens=1)
    with pytest.raises(PlaceResolutionError):nav.open_store(Store('source','Hồng Trà Ngô Gia','query',expected_address='141 Tôn Đản'))
    assert len(opened)==2 and len(scrolls)==2
    assert nav.last_resolution_diagnostics['place_entity_confirmed'] is False


def test_rate_limit_during_inspection_stops_immediately(monkeypatch):
    nav,opened,_=setup(monkeypatch,rate_limit=True)
    with pytest.raises(RateLimitedError):nav.open_store(Store('source','Hồng Trà Ngô Gia','query',expected_address='141 Tôn Đản'))
    assert len(opened)==2


def test_search_stops_after_two_idle_scrolls_independent_of_review_limits(monkeypatch):
    nav,opened,scrolls=setup(monkeypatch)
    nav.max_search_scrolls=5
    nav.open_store(Store('source','Hồng Trà Ngô Gia','query',expected_address='141 Tôn Đản'))
    # New IDs appear after scroll 2, then scrolls 3 and 4 are idle.
    assert len(scrolls)==4
    assert nav.last_resolution_diagnostics['search_idle_count']==2
    assert len(opened)==2  # initial search + one selected entity


def test_historical_override_hooks_are_executed(monkeypatch):
    nav,opened,_=setup(monkeypatch)
    calls=[]
    best=nav._best_exact_candidate;resolved=nav._resolved_place
    def record_best(*args):calls.append('best');return best(*args)
    def record_resolved(*args):calls.append('resolved');return resolved(*args)
    monkeypatch.setattr(nav,'_best_exact_candidate',record_best)
    monkeypatch.setattr(nav,'_resolved_place',record_resolved)
    nav.open_store(Store('source','Hồng Trà Ngô Gia','query',expected_address='141 Tôn Đản'))
    assert 'best' in calls and 'resolved' in calls and len(opened)==2


def test_direct_detail_page_uses_same_hooks(monkeypatch):
    nav,opened,_=setup(monkeypatch)
    driver=nav.driver
    original_get=driver.get
    def redirect(url):original_get('https://www.google.com/maps/place/right')
    monkeypatch.setattr(driver,'get',redirect)
    monkeypatch.setattr(nav,'_candidate_details',lambda:[])
    assert nav.open_store(Store('source','Hồng Trà Ngô Gia','query',expected_address='141 Tôn Đản')).endswith('/right')
    assert len(opened)==1


def test_recovered_capture_fallback_for_saved_patisstory():
    store=Store('source','PatisStory - Chuyện bánh','offline',expected_address='85 Đường Khánh Hội',
                expected_latitude=10.76,expected_longitude=106.70)
    item=candidate('Tiệm Bánh PatisStory',address=None,
                   suffix='patis/data=!3d10.7601!4d106.70')
    nav=GoogleMapsNavigator(None)
    assert nav._best_exact_candidate(store,[item]) is item
    assert nav.last_resolution_diagnostics['selection_method']=='COMPATIBLE_CANDIDATE_FOR_REVIEW'


def test_missing_source_address_supported_but_name_only_not_forced():
    nav=GoogleMapsNavigator(None)
    store=Store('source','Hồng Trà Ngô Gia','offline',expected_latitude=10.76,expected_longitude=106.70)
    item=candidate(address=None,suffix='store/data=!3d10.76!4d106.70')
    assert nav._best_exact_candidate(store,[item]) is item
    assert nav._best_exact_candidate(Store('source',store.name,'offline'),[candidate()]) is not None
