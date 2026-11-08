"""Collection eligibility is provisional; it never establishes store identity."""
from types import SimpleNamespace

from crawl_experiment.core.models import Store
from crawl_experiment.sources.google_maps.candidate_ranking import evaluate
from crawl_experiment.sources.google_maps.navigator import GoogleMapsNavigator
from test_google_search_discovery import setup


def card(name, address=None, suffix='one'):
    return dict(text=name,address=address,href='https://www.google.com/maps/place/'+suffix)


def test_oc_cuc_with_location_suffix_and_optional_house_number():
    store=Store('source','Ốc Cúc','offline',expected_address='129 Đ. Vĩnh Khánh')
    for address in ('129 Vĩnh Khánh','Đường Vĩnh Khánh'):
        item=card('Ốc Cúc Vĩnh Khánh Quận 4',address)
        decision=evaluate(store,item)
        assert decision['crawl_eligible']
        assert decision['identity_status']=='IDENTITY_UNVERIFIED'
        assert decision['name_similarity']>decision['raw_name_similarity']


def test_exact_name_missing_address_still_collectible_not_verified():
    store=Store('source','Ốc Cúc','offline')
    decision=evaluate(store,card('Ốc Cúc'))
    assert decision['crawl_eligible'] and decision['distance_m'] is None


def test_saved_suspicious_nhoc_nuong_is_not_supported_by_generic_nuong():
    store=Store('source','Quán Nhóc Nướng','offline',expected_address='232/119 Vĩnh Khánh')
    decision=evaluate(store,card('Quán Cá Biển Nướng','Đường Vĩnh Khánh'))
    assert not decision['crawl_eligible']
    assert 'NO_DISTINCTIVE_NAME_SUPPORT' in decision['rejection_reasons']
    assert GoogleMapsNavigator(None)._best_exact_candidate(store,[card('Quán Cá Biển Nướng','Vĩnh Khánh')]) is None


def test_same_brand_clear_other_branch_remains_rejected():
    store=Store('source','Highlands Coffee','offline',expected_address='182 Bến Vân Đồn')
    assert not evaluate(store,card('Highlands Coffee','384 Hoàng Diệu'))['crawl_eligible']


def test_numbered_streets_are_not_equivalent_despite_similar_spelling():
    store=Store('source','Ốc Cúc','offline',expected_address='129 Đường Số 41')
    assert not evaluate(store,card('Ốc Cúc','129 Đường Số 45'))['crawl_eligible']


def test_distance_is_supporting_when_name_and_address_agree():
    store=Store('source','Ốc Cúc','offline',expected_address='129 Vĩnh Khánh',
                expected_latitude=10.76,expected_longitude=106.70)
    item=card('Ốc Cúc','129 Vĩnh Khánh','detail/data=!3d10.78!4d106.70')
    assert evaluate(store,item)['distance_m']>1000
    assert GoogleMapsNavigator(None)._best_exact_candidate(store,[item]) is item


def test_card_heading_and_address_extracted_without_rating_or_closed_text():
    link=SimpleNamespace(text='',get_attribute=lambda k: {
        'href':card('')['href'],'aria-label':'Ốc Cúc\n4,5 (120)\nĐóng cửa vĩnh viễn'}.get(k))
    driver=SimpleNamespace(find_elements=lambda *a:[link],execute_script=lambda *a:dict(
        name='Ốc Cúc Vĩnh Khánh Quận 4',lines=['Nhà hàng · 129 Đ. Vĩnh Khánh'],
        text='Ốc Cúc\n4,5 (120)\nĐóng cửa vĩnh viễn'))
    result=GoogleMapsNavigator(driver)._candidate_details()[0]
    assert result['text']=='Ốc Cúc Vĩnh Khánh Quận 4'
    assert result['address']=='129 Đ. Vĩnh Khánh'


def test_try_next_relevant_candidate_when_first_detail_is_unsuitable(monkeypatch):
    nav,opened,_=setup(monkeypatch)
    first=card('Ốc Cúc','129 Vĩnh Khánh','first')
    second=card('Ốc Cúc','Vĩnh Khánh','second')
    monkeypatch.setattr(nav,'_candidate_details',lambda:[first,second])
    monkeypatch.setattr(nav,'_place_title',lambda: 'Other Business' if nav.driver.current_url.endswith('first') else 'Ốc Cúc')
    monkeypatch.setattr(nav,'_place_address',lambda:'129 Vĩnh Khánh')
    store=Store('source','Ốc Cúc','query',expected_address='129 Vĩnh Khánh')
    assert nav.open_store(store).endswith('second')
    assert len(opened)==3
    assert len(nav.last_resolution_diagnostics['candidate_attempts'])==2
    assert nav.last_resolution_diagnostics['search_elapsed_seconds']>=0
