import csv
import io
import json

import pyarrow as pa
import pytest

from fnb_platform import comparison_features as cf, entity_resolution as er
from fnb_platform import entity_resolution_v2 as v2
from test_entity_resolution import fixture_rows, published_features


def run(ov, gg, links, reverse=False, details=()):
    refs={k:('test.parquet','hash') for k in ('overture','places','links')}
    tables,_=cf.build(*(pa.Table.from_pylist(r) for r in (ov,gg,links)),refs)
    args=[tables['comparison/'+n+'.parquet'].to_pylist() for n in
        ('overture_features','google_features','address_pair_evidence')]+[ov,gg,links]
    if reverse:args=[list(reversed(a)) for a in args]
    return er.build(*args,dict(overture='ov.parquet',places='gg.parquet',pairs='pairs.parquet'),version='v2',historical_details=details)


@pytest.mark.parametrize('left,right,address',[
    ('Ốc Tú','ỐC TÚ - Đoàn Văn Bơ','232/75 Đoàn Văn Bơ'),
    ('Chấn Phong Mì Gia','CHẤN PHONG MÌ GIA( Mì Xá Xíu- Hoành Thánh- Sủi Cảo)','156 Tôn Đản'),
    ('GoGi House','GoGi House Đường số 41 Quận 4','69 Đ. Số 41'),
    ('Trà Sữa Mr Tea','TRÀ SỮA MR TEA | TRÀ SỮA NHÀ LÀM | TRÀ SỮA NGON QUẬN 4','34 Nguyễn Hữu Hào'),
    ('Alo Quán - Seafood & Beer','Alo Quán | Beer & Seafood','333 Vĩnh Khánh'),
    ('Sumi','Sumi - Ăn Vặt Cá Viên','148/12/25 Tôn Đản'),
    ('Quán Ốc 284','Quán ốc 284','284 Tôn Đản'),
])
def test_real_name_variations_with_same_parsed_address(left,right,address):
    ov,gg,li=fixture_rows()
    ov[0].update(name=left,address=address);gg[0].update(name_display=right,address_display=address)
    table,_,_=run(ov,gg,li)
    r=table.to_pylist()[0]
    assert r['classification']=='MATCH_CANDIDATE' and r['er_status']=='ER_PENDING'
    e=json.loads(r['evidence_json'])
    assert e['overture']['name_raw']==left and e['google']['name_raw']==right
    assert e['comparisons']['v2']['strong_address_agreement']


def test_name_features_symmetric_tokens_and_containment():
    a,b=cf.comparison_text('GoGi House'),cf.comparison_text('GoGi House Đường số 41 Quận 4')
    x=v2.name_features(a,b,er.name_similarity(a,b));y=v2.name_features(b,a,er.name_similarity(b,a))
    for field in ('token_dice','token_containment','primary_name_supported','branch_warning_codes'):
        assert x[field]==y[field]
    assert '41' in x['right_tokens'] and x['token_containment']


def test_shared_abc_independent_links_and_group_evidence():
    ov,gg,li=fixture_rows()
    ov[0].update(name='ABC Bakery',address='187 Khánh Hội',latitude=10.763)
    ov.append({**ov[0],'source_id':'ov2','name':'ABC Bakery Quận 4','address':'396 Hoàng Diệu','latitude':10.76})
    gg[0].update(name_display='ABC BAKERY QUẬN 4',address_display='396 Hoàng Diệu')
    li.append({**li[0],'overture_id':'ov2'})
    t,q,r=run(ov,gg,li);rows=t.to_pylist()
    assert [x['classification'] for x in rows]==['NON_MATCH_CANDIDATE','MATCH_CANDIDATE']
    assert all('GOOGLE_ID_SHARED_BY_OVERTURE_IDS' in {f['flag_code'] for f in json.loads(x['flags_json'])} for x in rows)
    assert r['review_queue_links']==2
    t2,q2,r2=run(ov,gg,li,reverse=True)
    assert t.to_pylist()==t2.to_pylist() and q==q2 and r==r2
    s=io.StringIO();w=csv.DictWriter(s,fieldnames=er.QUEUE);w.writeheader();w.writerows(q);s.seek(0)
    assert len(list(csv.DictReader(s)))==len(q)


def test_oc_so_7_remote_partial_street_not_ignored():
    ov,gg,li=fixture_rows();ov[0].update(name='Quan Oc So 7',address='232 Vĩnh Khánh')
    gg[0].update(name_display='Quán Ốc Số 7',address_display='Đ. Vĩnh Lộc, Tân Vĩnh Lộc, Hồ Chí Minh',latitude=10.91)
    t,_,_=run(ov,gg,li);r=t.to_pylist()[0];e=json.loads(r['evidence_json'])
    assert r['classification']=='NON_MATCH_CANDIDATE'
    assert not e['comparisons']['clear_street_conflict']
    assert e['comparisons']['v2']['corroboratable_street_difference']


@pytest.mark.parametrize('address,latitude,expected',[
    ('150 Đ. Số 41',10.76,'REVIEW'), # nearby chain, different house
    ('150 Đ. Số 41',10.80,'NON_MATCH_CANDIDATE'),
    ('69 Đ. Số 41',10.80,'REVIEW'), # far coordinates but same address
    (None,10.76,'REVIEW'), # containment alone without address
    ('Trước cửa nhà 150 Đ. Số 41',10.80,'REVIEW'), # reference-only, not certain contradiction
])
def test_chain_counterexamples(address,latitude,expected):
    ov,gg,li=fixture_rows();ov[0].update(name='GoGi House',address='69 Đ. Số 41')
    gg[0].update(name_display='GoGi House Đường số 41 Quận 4',address_display=address,latitude=latitude)
    assert run(ov,gg,li)[0]['classification'][0].as_py()==expected


def test_explicit_branch_numbers_conflict_despite_same_address():
    ov,gg,li=fixture_rows();ov[0]['name']='GoGi House Chi nhánh 1';gg[0]['name_display']='GoGi House Chi nhánh 2'
    t,_,_=run(ov,gg,li)
    assert t['classification'][0].as_py()=='REVIEW'
    assert json.loads(t['evidence_json'][0].as_py())['comparisons']['v2']['name_features']['branch_warning_codes']


def test_real_google_name_address_hint_disagrees_with_metadata():
    ov,gg,li=fixture_rows()
    ov[0].update(name='Merlion cháo ech Singapore',address='554 Đ. Vĩnh Khánh')
    gg[0].update(name_display='Cháo Ếch Singapore Merlion 89 Vĩnh Khánh',address_display='554 Đ. Vĩnh Khánh')
    t,_,_=run(ov,gg,li)
    assert t['classification'][0].as_py()=='REVIEW'
    v=json.loads(t['evidence_json'][0].as_py())['comparisons']['v2']
    assert v['named_address_hints']['google']['house_literals']==['89']
    assert 'NAME_ADDRESS_HINT_DIFFERS_FROM_GOOGLE_ADDRESS' in v['name_features']['branch_warning_codes']


def test_unrelated_same_address_and_generic_overlap_not_sufficient():
    ov,gg,li=fixture_rows();ov[0]['name']='Ốc Tú';gg[0]['name_display']='Ốc Nhi'
    assert run(ov,gg,li)[0]['classification'][0].as_py()=='REVIEW'
    ov[0]['name']='Ốc';gg[0]['name_display']='Ốc Tú'
    assert run(ov,gg,li)[0]['classification'][0].as_py()=='REVIEW'


def test_missing_metadata_and_historical_flags():
    ov,gg,li=fixture_rows();li[0]['dq_flags']=['STREET_CONFLICT']
    assert run(ov,gg,li)[0]['classification'][0].as_py()=='MATCH_CANDIDATE'
    gg[0].update(name_display=None,latitude=None)
    assert run(ov,gg,li)[0]['classification'][0].as_py()=='INSUFFICIENT_DATA'


def test_multiple_candidates_still_cautious():
    ov,gg,li=fixture_rows();gg.append({**gg[0],'google_identity_value':'google2'})
    li.append({**li[0],'google_identity_value':'google2'})
    t,_,_=run(ov,gg,li)
    assert set(t['classification'].to_pylist())=={'REVIEW'}


@pytest.mark.parametrize('distance,expected',[(199.99,'REVIEW'),(200.,'REVIEW'),(200.01,'NON_MATCH_CANDIDATE')])
def test_two_address_conflict_boundary(distance,expected):
    a=cf.make_feature({'name':'ABC Bakery','address':'187 Khánh Hội'}, {}, {})
    b=cf.make_feature({'name':'ABC Bakery','address':'396 Hoàng Diệu'}, {}, {})
    p={'house_component_relation':'DIFFERENT_LITERAL','street_component_relation':'DIFFERENT_LITERAL'}
    assert v2.evaluate(a,b,p,1,distance,False,False,[])[0]==expected


def test_remote_partial_boundary_and_missing_street():
    a=cf.make_feature({'name':'Quan Oc So 7','address':'232 Vĩnh Khánh'}, {}, {})
    b=cf.make_feature({'name':'Quán Ốc Số 7','address':'Đ. Vĩnh Lộc'}, {}, {})
    p={'house_component_relation':'MISSING_COMPONENT','street_component_relation':'DIFFERENT_LITERAL'}
    assert v2.evaluate(a,b,p,1,2000,False,False,[])[0]=='REVIEW'
    assert v2.evaluate(a,b,p,1,2000.01,False,False,[])[0]=='NON_MATCH_CANDIDATE'
    b['parse_status']='AMBIGUOUS'
    assert v2.evaluate(a,b,p,1,16758,False,False,[])[0]=='REVIEW'


def test_v2_publication_separate_atomic_idempotent(published_features,tmp_path):
    comparison,_=published_features
    baseline=tmp_path/'v1';er.publish(comparison,baseline)
    protected={p:er.silver.sha(p) for p in baseline.rglob('*') if p.is_file()}
    out=tmp_path/'v2'
    def fail(stage):
        if stage=='after_stage_validation':raise RuntimeError('injected')
    with pytest.raises(RuntimeError):er.publish(comparison,out,version='v2',baseline_root=baseline,fault=fail)
    assert not (out/'current.json').exists()
    a,r=er.publish(comparison,out,version='v2',baseline_root=baseline)
    b,s=er.publish(comparison,out,version='v2',baseline_root=baseline)
    assert a==b and r==s and all(er.silver.sha(p)==h for p,h in protected.items())
    with pytest.raises(ValueError,match='separate'):er.publish(comparison,baseline,version='v2',baseline_root=baseline)


@pytest.mark.parametrize('distance,expected',[(24.99,'MATCH_CANDIDATE'),(25.,'MATCH_CANDIDATE'),(25.01,'REVIEW')])
def test_single_token_anchor_boundary(distance,expected):
    a=cf.make_feature({'name':'Sumi','address':'148/12/25 Tôn Đản'}, {}, {})
    b=cf.make_feature({'name':'Sumi - Ăn Vặt Cá Viên','address':'148/12/25 Tôn Đản'}, {}, {})
    p={'house_component_relation':'SAME_LITERAL','street_component_relation':'SAME_LITERAL'}
    assert v2.evaluate(a,b,p,er.name_similarity(a['name_comparison'],b['name_comparison']),distance,False,False,[])[0]==expected


@pytest.mark.parametrize('distance,expected',[(499.99,'REVIEW'),(500.,'REVIEW'),(500.01,'NON_MATCH_CANDIDATE')])
def test_same_street_house_conflict_boundary(distance,expected):
    a=cf.make_feature({'name':'Quán Vú Heo Nướng','address':'2 Vĩnh Khánh'}, {}, {})
    b=cf.make_feature({'name':'Vú Heo Nướng Tiến Lên','address':'89 Vĩnh Khánh'}, {}, {})
    p={'house_component_relation':'DIFFERENT_LITERAL','street_component_relation':'SAME_LITERAL'}
    assert v2.evaluate(a,b,p,.63,distance,False,False,[])[0]==expected


def test_identity_metadata_conflict_has_review_reason():
    a=cf.make_feature({'name':'ABC Bakery','address':'396 Hoàng Diệu'}, {}, {})
    p={'house_component_relation':'SAME_LITERAL','street_component_relation':'SAME_LITERAL'}
    status,e=v2.evaluate(a,a,p,1,1,False,False,['coordinates'])
    assert status=='REVIEW' and e['review_reason_codes']==['IDENTITY_METADATA_CONFLICT']


def test_historical_detail_order_is_stable():
    ov,gg,li=fixture_rows()
    detail=dict(overture_id='ov1',google_id_kind='data_id',google_identity_value='google1')
    details=[dict(detail,issue_code='STREET_CONFLICT'),dict(detail,issue_code='HOUSE_CONFLICT')]
    a,q,r=run(ov,gg,li,details=details)
    b,q2,r2=run(ov,gg,li,reverse=True,details=list(reversed(details)))
    assert a.to_pylist()==b.to_pylist() and q==q2 and r==r2
