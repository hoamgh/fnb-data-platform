"""Source-neutral locality evidence; synthetic ward variants are unit fixtures."""
from dataclasses import replace

import pytest

from crawl_experiment.core.models import Store
from crawl_experiment.sources.google_maps.candidate_ranking import evaluate, address_parts, RankingConfig
from crawl_experiment.sources.google_maps.navigator import GoogleMapsNavigator
from scripts.google_live_workers import store_for


@pytest.mark.parametrize('ward',['Khánh Hội','Bến Thành','Hải Châu','12'])
def test_same_policy_for_any_input_ward_and_minor_street_variation(ward):
    # Deliberately vary context; these fixtures do not assert actual locations.
    source=Store('test','Ốc Cúc','offline',expected_address='129 Vĩnh Khánh',
                 geographic_context=f'Phường {ward}, Việt Nam')
    candidate=dict(text='Ốc Cúc',address=f'Đường Vĩnh Khan, Phường {ward}, Việt Nam',
                   href='https://www.google.com/maps/place/local')
    result=evaluate(source,candidate)
    assert result['same_ward'] and result['expected_ward']
    assert result['crawl_eligible']
    assert result['identity_status']=='IDENTITY_UNVERIFIED'
    assert GoogleMapsNavigator(None)._best_exact_candidate(source,[candidate]) is candidate


def test_same_ward_does_not_accept_unrelated_business():
    source=Store('test','Ốc Cúc','offline',expected_address='129 Vĩnh Khánh',expected_ward='Bến Thành')
    candidate=dict(text='Highlands Coffee',address='129 Vĩnh Khánh, Bến Thành',
                   href='https://www.google.com/maps/place/other')
    assert evaluate(source,candidate)['same_ward']
    assert not evaluate(source,candidate)['crawl_eligible']


def test_same_ward_and_name_allow_collection_despite_source_house_conflict():
    source=Store('test','Highlands Coffee','offline',expected_address='182 Bến Vân Đồn',expected_ward='Khánh Hội')
    candidate=dict(text='Highlands Coffee',address='384 Bến Vân Đồn, Khánh Hội',
                   href='https://www.google.com/maps/place/branch')
    result=evaluate(source,candidate)
    assert result['same_ward'] and result['house_conflict']
    assert result['crawl_eligible'] and result['penalty']>0
    assert result['identity_status']=='IDENTITY_UNVERIFIED'


def test_same_ward_and_name_sufficient_without_street_or_point_agreement():
    source=Store('test','Ốc Cúc','offline',expected_address='129 Vĩnh Khánh',expected_ward='Khánh Hội')
    candidate=dict(text='Ốc Cúc',address='55 Nguyễn Huệ, Khánh Hội',
                   href='https://www.google.com/maps/place/other-street')
    assert evaluate(source,candidate)['same_ward']
    assert evaluate(source,candidate)['crawl_eligible']
    assert GoogleMapsNavigator(None)._best_exact_candidate(source,[candidate]) is candidate


def test_unreliable_source_point_does_not_veto_observed_same_ward_name():
    source=Store('test','Ốc Cúc','offline',expected_address='129 Vĩnh Khánh',
                 expected_ward='Khánh Hội',expected_latitude=11.76,expected_longitude=106.70)
    candidate=dict(text='Ốc Cúc',address='55 Nguyễn Huệ, Khánh Hội',
                   href='https://www.google.com/maps/place/store/data=!3d10.76!4d106.70')
    result=evaluate(source,candidate)
    assert result['location_conflict'] and result['same_ward_name_eligible']
    assert result['crawl_eligible']
    assert GoogleMapsNavigator(None)._best_exact_candidate(source,[candidate]) is candidate


def test_ambiguous_same_brand_candidates_in_ward_are_not_forced():
    source=Store('test','Highlands Coffee','offline',expected_address='182 Bến Vân Đồn',expected_ward='Khánh Hội')
    candidates=[dict(text='Highlands Coffee',address=f'{number} Bến Vân Đồn, Khánh Hội',
                    href=f'https://www.google.com/maps/place/{number}') for number in (384,400)]
    assert all(evaluate(source,c)['crawl_eligible'] for c in candidates)
    assert GoogleMapsNavigator(None)._best_exact_candidate(source,candidates) is None


def test_near_name_match_keeps_conflicting_street_as_unverified_evidence():
    source=Store('test','Quan Oc 148','offline',expected_address='755 Quốc Lộ 13',
                 expected_latitude=10.758787,expected_longitude=106.70672,
                 geographic_context='Phường Khánh Hội, Thành phố Hồ Chí Minh, Việt Nam')
    candidate=dict(text='Quán ốc 148',address='148 Tôn Đản',
                   href='https://www.google.com/maps/place/store/data=!3d10.7588301!4d106.7066663')
    result=evaluate(source,candidate)
    assert result['street_conflict'] and result['crawl_eligible']
    assert result['local_address_variant_eligible']
    assert not result['same_ward']  # Proximity must never masquerade as a ward lookup.
    assert result['local_evidence_basis']=='NEAR_SOURCE_POINT_WARD_NOT_CONFIRMED'
    assert not evaluate(source,candidate,RankingConfig(local_distance_m=1))['crawl_eligible']


def test_explicit_other_ward_is_retained_and_not_forced():
    source=Store('test','Ốc Cúc','offline',expected_address='129 Vĩnh Khánh',expected_ward='Bến Thành')
    candidate=dict(text='Ốc Cúc',address='129 Vĩnh Khánh, Phường Hải Châu',
                   href='https://www.google.com/maps/place/elsewhere')
    result=evaluate(source,candidate)
    assert result['ward_conflict'] and not result['crawl_eligible']
    assert GoogleMapsNavigator(None)._best_exact_candidate(source,[candidate]) is None


def test_locality_only_and_descriptive_address_are_not_fabricated():
    for ward in ['Khánh Hội','Bến Thành','Hải Châu']:
        assert address_parts(ward,ward)==(None,None)
    assert address_parts('Trước Cửa Nhà 134/41 Đoàn Văn Bơ')==(None,'doan van bo')
    assert address_parts('158-160, Hoàng Diệu')==(None,'hoang dieu')
    source=Store('test','Ốc Cúc','offline')
    result=evaluate(source,dict(text='Ốc Cúc',address=None,href='https://www.google.com/maps/place/a'))
    assert result['address'] is None and result['same_ward'] is False


def test_different_distinctive_names_not_rescued_by_cuisine_and_proximity():
    source=Store('test','Quán Chay Thiện Lành','offline',expected_address='122 Vĩnh Khánh',
                 expected_latitude=10.76,expected_longitude=106.70)
    candidate=dict(text='Quán chay Thiện Ân',address='65 Đ. Số 41',
                   href='https://www.google.com/maps/place/other/data=!3d10.7601!4d106.70')
    assert not evaluate(source,candidate)['crawl_eligible']
    assert GoogleMapsNavigator(None)._best_exact_candidate(source,[candidate]) is None


def test_worker_propagates_locality_without_replacing_original_address():
    raw=dict(source='test',source_id='1',name='Ốc Cúc',address=None,
             ward='Hải Châu',city='Đà Nẵng',lat=16.06,lng=108.22)
    original=dict(raw);store=store_for(raw)
    assert store.expected_ward=='Hải Châu'
    assert store.geographic_context=='Hải Châu, Đà Nẵng'
    assert store.expected_address is None and raw==original


def test_saved_phuc_kien_milk_tea_and_tra_phuc_kien_are_collectible():
    source=Store('overture:1cf45f67-635b-4baf-b4de-6cb5c595a82d','Phúc Kiến Milk Tea','offline',
        expected_address='576/75 Hẻm 124 Đường Xóm Chiếu',
        expected_latitude=10.75804,expected_longitude=106.70996,
        geographic_context='Phường Khánh Hội, Thành phố Hồ Chí Minh, Việt Nam')
    candidate=dict(text='Trà Phúc Kiến',address='86 Xóm Chiếu, Khánh Hội, Hồ Chí Minh, Việt Nam',
        href='https://www.google.com/maps/place/store/data=!3d10.7585833!4d106.7109409')
    original=dict(candidate)
    result=evaluate(source,candidate)
    assert result['raw_name_similarity']==pytest.approx(.5806451612903226)
    assert result['name_similarity']==pytest.approx(1.0)  # Same distinctive core: Phúc Kiến.
    assert result['same_ward_name_eligible'] and result['crawl_eligible']
    assert GoogleMapsNavigator(None)._best_exact_candidate(source,[candidate]) is candidate
    assert result['identity_status']=='IDENTITY_UNVERIFIED' and candidate==original
    unrelated=dict(candidate,text='Trà Phúc Long')
    assert not evaluate(source,unrelated)['crawl_eligible']


@pytest.mark.parametrize('ward',['Khánh Hội','Hải Châu'])
@pytest.mark.parametrize('name',['Trà Phúc Kiến','Phuc Kien Tea Shop','Phúc Kiên','Kiến Phúc'])
def test_near_distinctive_name_in_input_ward_is_collectible(ward,name):
    # Synthetic name/context variants exercise the source-neutral collection rule.
    source=Store('test','Phúc Kiến Milk Tea','offline',expected_address='576/75 Xóm Chiếu',expected_ward=ward)
    candidate=dict(text=name,address=f'86 Xóm Chiếu, {ward}',
                   href='https://www.google.com/maps/place/variant')
    result=evaluate(source,candidate)
    assert result['same_ward_name_eligible'] and result['crawl_eligible']
    assert GoogleMapsNavigator(None)._best_exact_candidate(source,[candidate]) is candidate
