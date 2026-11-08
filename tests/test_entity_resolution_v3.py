import json

import pytest
import pyarrow as pa

from fnb_platform import comparison_features as cf, entity_resolution as er
from fnb_platform import entity_resolution_v3 as v3
from test_entity_resolution import fixture_rows, published_features


def compare(left,right,address_left,address_right,distance=5,shared=False,multiple=False):
    a=cf.make_feature(dict(name=left,address=address_left),{}, {})
    b=cf.make_feature(dict(name=right,address=address_right),{}, {})
    p=dict(house_component_relation=cf.house_relation(a,b),
        street_component_relation=cf.component_relation(a['street_comparison'],b['street_comparison']))
    return v3.evaluate(a,b,p,er.name_similarity(a['name_comparison'],b['name_comparison']),distance,shared,multiple,[])


@pytest.mark.parametrize('left,right,address',[
    ('Pizza 777','PIZZA777 - QUẬN 4','237/42/5 Hoàng Diệu'),
    ('OcSu20k','Quán Ốc Su 20k','53 Tôn Đản'),
    ('Passio Coffee','Cà Phê Passio Quận 4','124 Khánh Hội'),
    ('Oc Qanh','Ốc Oanh','534 Vĩnh Khánh'),
    ('Kafin Coffee','KAFIN COFFFEE - XE CÀ PHÊ PHA MÁY - QUẬN 4','198 Hoàng Diệu'),
    ('Nhà hàng Tam Xuyên','Ẩm thực tam xuyên 温州卤味','375 Đường Vĩnh Khánh'),
    ('Trà Sữa Phi Phi 菲菲奶茶店','Trà sữa Phi Phi','171 Tôn Đản'),
    ('Chilli Bbq Hotpot Restaurant','Chilli Lẩu Nướng Quán','232 Vĩnh Khánh'),
])
def test_name_spacing_typos_bilingual_and_description(left,right,address):
    status,e=compare(left,right,address,address)
    assert status=='MATCH_CANDIDATE'
    assert e['signals']['name']=='SUPPORT' and e['signals']['address']=='SUPPORT'
    assert not e['name_features']['effective_branch_warning_codes']


@pytest.mark.parametrize('address',[
    '27 Hẻm 150 Bến Vân Đồn','K62 Bến Vân Đồn','Bến Vân Đồn/G42 Đ. Số 50',
    '46 lô I cư xá vĩnh hội','Lô A, Tôn Thất Thuyết/CC Khánh Hội',
])
def test_literal_address_support_despite_parser(address):
    status,e=compare('Beno','Beno - mì Ý bò Mỹ & Steak',address,address)
    assert status=='MATCH_CANDIDATE'
    assert e['address_features_v3']['normalized_surface_equal']
    assert e['address_features_v3']['strong_agreement']


def test_admin_tail_prefix_equality_is_explicit_not_full_equality():
    status,e=compare('MUI Coffee and Tea','MUI coffee & tee','K62 Bến Vân Đồn',
        '\ue3c8 K62 Bến Vân Đồn, Khánh Hội, Hồ Chí Minh, Việt Nam')
    assert status=='MATCH_CANDIDATE'
    x=e['address_features_v3'];assert x['observed_prefix_equal_with_admin_tail'] and not x['normalized_surface_equal']


@pytest.mark.parametrize('left,right',[
    ('122 Tôn Đản','122A Tôn Đản'),('122 Tôn Đản','122/14 Tôn Đản'),
    ('134/41 Đoàn Văn Bơ','134/121 Đoàn Văn Bơ'),
    ('79 Đường Số 41','71 Đ. Số 41'),('183B6 Tôn Thất Thuyết','183 Tôn Thất Thuyết'),
    ('K62 Bến Vân Đồn','K63 Bến Vân Đồn'),
    ('Lô A, Đường Số 20','Lô B, Đường Số 20'),
    ('27 Hẻm 150 Bến Vân Đồn','27 Hẻm 151 Bến Vân Đồn'),
])
def test_nearby_house_lot_alley_conflicts_not_equivalent(left,right):
    status,e=compare('GoGi House','GoGi House',left,right,distance=.5)
    assert status=='REVIEW'
    assert not e['address_features_v3']['strong_agreement']


def test_street_first_house_and_numbered_street_alias():
    assert compare('Luke’s Shop','Luke’s Shop','Đường Tôn Đản 293','293 Đ. Tôn Đản')[0]=='MATCH_CANDIDATE'
    status,e=compare('Kem Mỹ','Kem Mỹ','11 Đường 41','23 Đ. Số 41')
    assert status=='REVIEW' and e['address_features_v3']['street_state']=='SUPPORT'
    assert e['address_features_v3']['house_state']=='CONFLICT'


def test_generic_street_or_building_does_not_prove_specific_store():
    for address in ['Hoàng Diệu','Chung cư H1','Hẻm 243 Tôn Đản']:
        status,e=compare('Beno','Beno - mì Ý bò Mỹ & Steak',address,address)
        assert e['address_features_v3']['strong_agreement']
        assert not e['address_features_v3']['specific_agreement']
        assert status=='REVIEW'


def test_null_and_reference_are_not_conflicts():
    for address in [None,'Trước cửa nhà 134/41 Đoàn Văn Bơ']:
        status,e=compare('Bột Chiên Hẻm 134','Bột Chiên Hải Vân',address,'134/121 Đoàn Văn Bơ')
        assert status=='REVIEW'
        assert not e['address_features_v3']['clear_conflict']


def test_ambiguous_alley_difference_is_unknown_not_definite_conflict():
    status,e=compare('Beno','Beno','27 Hẻm 150 Bến Vân Đồn','27 Hẻm 151 Bến Vân Đồn')
    assert status=='REVIEW' and e['address_features_v3']['alley_state']=='UNKNOWN'
    assert e['address_features_v3']['uncertain_difference']
    assert not e['address_features_v3']['clear_conflict']


@pytest.mark.parametrize('left,right',[
    ('Pizza777 Quận 4','Pizza888 Quận 4'),
    ('GoGi House Chi Nhánh 1','GoGi House Chi Nhánh 2'),
    ('Chicken Plus Vĩnh Hội','Chicken Plus Tôn Đản'),
    ('Ốc Tú','Ốc Nhi'),
    ('Xoài non Cường Ngân','Xoài non - Cóc Non - Mắm Ruốc Mỹ Ngân'),
])
def test_branch_and_brand_counterexamples(left,right):
    assert compare(left,right,'241 Tôn Đản','241 Tôn Đản')[0]=='REVIEW'


def test_existing_title_metadata_warning_retained():
    status,e=compare('Merlion cháo ech Singapore','Cháo Ếch Singapore Merlion 89 Vĩnh Khánh',
        '554 Đ. Vĩnh Khánh','554 Đ. Vĩnh Khánh')
    assert status=='REVIEW' and e['signals']['name']=='CONFLICT'


def test_far_same_address_not_forced_positive_or_negative():
    assert compare('Nhà Văn Kitchen','NHÀ VĂN KITCHEN RESTAURANT','41 Hồ Xuân Hương','41 Hồ Xuân Hương',2387)[0]=='REVIEW'
    assert compare('Quan Oc So 7','Quán Ốc Số 7','232 Vĩnh Khánh','Đ. Vĩnh Lộc',16758)[0]=='NON_MATCH_CANDIDATE'


def test_fuzzy_brand_only_requires_tight_corroboration():
    assert compare('Oc Qanh','Ốc Oanh','534 Vĩnh Khánh','534 Vĩnh Khánh',25)[0]=='MATCH_CANDIDATE'
    assert compare('Oc Qanh','Ốc Oanh','534 Vĩnh Khánh','534 Vĩnh Khánh',25.01)[0]=='REVIEW'


def test_symmetric_new_comparison_signals():
    a=compare('Pizza777','Pizza 777 Quận 4','237/42/5 Hoàng Diệu','237/42/5 Hoàng Diệu')
    b=compare('Pizza 777 Quận 4','Pizza777','237/42/5 Hoàng Diệu','237/42/5 Hoàng Diệu')
    assert a[0]==b[0] and a[1]['signals']==b[1]['signals']


def test_all_original_values_preserved_contract_and_order():
    ov,gg,links=fixture_rows();ov[0].update(name='Pizza 777',address='K62 Bến Vân Đồn')
    gg[0].update(name_display='PIZZA777 - QUẬN 4',address_display='K62 Bến Vân Đồn')
    tables,_=cf.build(*(pa.Table.from_pylist(r) for r in [ov,gg,links]),{k:('test.parquet','hash') for k in ['overture','places','links']})
    args=[tables['comparison/'+n+'.parquet'].to_pylist() for n in ['overture_features','google_features','address_pair_evidence']]+[ov,gg,links]
    refs=dict(overture='ov.parquet',places='gg.parquet',pairs='pairs.parquet')
    t,q,r=er.build(*args,refs,version='v3')
    t2,q2,r2=er.build(*[list(reversed(x)) for x in args],refs,version='v3')
    assert t.to_pylist()==t2.to_pylist() and q==q2 and r==r2
    row=t.to_pylist()[0];assert row['er_status']=='ER_PENDING' and row['overture_address']=='K62 Bến Vân Đồn'
    assert row['rule_version']==v3.VERSION
    for flag in json.loads(row['flags_json']):
        assert all(flag[k] for k in ['reason','evidence_json','observation_ref','rule_version','recommended_action'])


def test_separate_versioned_atomic_output(published_features,tmp_path):
    root,_=published_features;v1=tmp_path/'v1';v2root=tmp_path/'v2'
    er.publish(root,v1);er.publish(root,v2root,version='v2',baseline_root=v1)
    protected={p:er.silver.sha(p) for p in v2root.rglob('*') if p.is_file()}
    out=tmp_path/'v3'
    def fail(stage):
        if stage=='after_stage_validation':raise RuntimeError('injected')
    with pytest.raises(RuntimeError):er.publish(root,out,version='v3',baseline_root=v2root,fault=fail)
    assert not (out/'current.json').exists()
    a,s=er.publish(root,out,version='v3',baseline_root=v2root)
    b,t=er.publish(root,out,version='v3',baseline_root=v2root)
    assert a==b and s==t and all(er.silver.sha(p)==h for p,h in protected.items())
    with pytest.raises(ValueError,match='separate'):er.publish(root,v2root,version='v3',baseline_root=v2root)
