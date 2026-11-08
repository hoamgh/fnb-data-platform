from dataclasses import replace

import pytest

from crawl_experiment.core.models import Store
from crawl_experiment.sources.google_maps.candidate_ranking import RankingConfig, evaluate, rank, normalize


def source(address='141 Tôn Đản'):
    return Store('source','Hồng Trà Ngô Gia','query',expected_address=address)


def candidate(name='Hồng Trà Ngô Gia H103',address='141 Tôn Đản',url='https://www.google.com/maps/place/store'):
    return dict(text=name,address=address,href=url)


def test_unknown_weights_renormalized_and_real_name_variant_exact_address():
    e=evaluate(source(),candidate())
    assert e['crawl_eligible'] and e['distance_m'] is None
    assert e['available_weight']==pytest.approx(.85)
    assert e['base_score']==pytest.approx((.45*e['name_similarity']+.4)/.85)
    assert e['identity_status']=='IDENTITY_UNVERIFIED'


def test_name_same_street_without_house_is_eligible():
    assert evaluate(source(),candidate(address='Tôn Đản'))['crawl_eligible']


def test_same_brand_different_house_is_penalized():
    e=evaluate(source(),candidate(address='143 Tôn Đản'))
    assert e['house_conflict'] and e['penalty']==.3 and not e['crawl_eligible']


def test_missing_address_uses_name_and_nearby_entity_point():
    store=replace(source(None),expected_latitude=10.76,expected_longitude=106.70)
    c=candidate(address=None,url='https://www.google.com/maps/place/store/data=!3d10.76!4d106.70')
    e=evaluate(store,c)
    assert e['crawl_eligible'] and e['address_compatibility'] is None and e['available_weight']==.6
    assert evaluate(store,candidate(address=None))['crawl_eligible']  # exact name, collection only


def test_unrelated_name_nearby_is_not_sufficient():
    store=replace(source(None),expected_latitude=10.76,expected_longitude=106.70)
    assert not evaluate(store,candidate(name='Ốc Loan',address=None,
        url='https://www.google.com/maps/place/store/data=!3d10.76!4d106.70'))['crawl_eligible']


def test_zero_or_one_best_and_dedup_candidate_ids():
    bad=candidate(address='143 Tôn Đản',url='https://www.google.com/maps/place/wrong')
    good=candidate()
    rows,selected=rank(source(),[bad,good,dict(good)])
    assert len(rows)==2 and selected[0]==good and rows[0][1]['rank']==1
    assert rank(source(),[bad])[1] is None


def test_permanently_closed_flag_does_not_change_eligibility():
    c=candidate(); before=evaluate(source(),c)
    c['business_status']='CLOSED_PERMANENTLY'
    assert evaluate(source(),c)==before


def test_vietnamese_normalization_preserves_originals():
    assert normalize('Đ. Tôn Đản')==normalize('Đường Tôn Đản')
    c=candidate(address='\n141 Đường Tôn Đản, Khánh Hội')
    original=dict(c)
    assert evaluate(source(),c)['address_compatibility']==1
    assert c==original


def test_configurable_weights_and_threshold():
    assert not evaluate(source(),candidate(),RankingConfig(eligibility_threshold=1))['crawl_eligible']


def test_saved_real_examples_and_negative_controls():
    import json
    from pathlib import Path
    examples=json.loads((Path(__file__).parent/'fixtures/google_moderate_real_examples.json').read_text(encoding='utf-8'))
    negatives={'same_brand_different_branch','unrelated_nearby_negative_control'}
    for example in examples:
        raw=example['source']
        store=Store(raw['source_id'],raw['name'],'offline',expected_address=raw['address'],
                    expected_latitude=raw['latitude'],expected_longitude=raw['longitude'])
        result=evaluate(store,example['candidate'])
        assert result['crawl_eligible']==(example['case'] not in negatives)
        assert result['identity_status']=='IDENTITY_UNVERIFIED'


def test_viewport_camera_does_not_supply_geographic_evidence():
    store=replace(source(None),expected_latitude=10.76,expected_longitude=106.70)
    e=evaluate(store,candidate(address=None,url='https://www.google.com/maps/place/store/@10.76,106.70,17z'))
    assert e['distance_m'] is None and e['crawl_eligible']  # name evidence, never camera evidence


def test_saved_nested_house_number_is_not_exact_equality_requirement():
    store=Store('source','Quán Khói','offline',expected_address='122/37/9 Vĩnh Khánh')
    c=candidate(name='Quán Khói - Ốc Thuyền',address='mặt tiền, 37/9 Đ. Vĩnh Khánh, Khánh Hội')
    evidence=evaluate(store,c)
    assert evidence['same_street'] and not evidence['house_conflict']
    assert evidence['house_suffix_compatible_unverified'] and evidence['crawl_eligible']
    assert evidence['identity_status']=='IDENTITY_UNVERIFIED'
