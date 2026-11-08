import json
from copy import deepcopy

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from fnb_platform import google_address_enrichment as a, google_silver as s
from fnb_platform.overture_incremental import _digest


META={'admin_area_name':'Phường Khánh Hội, Thành phố Hồ Chí Minh, Việt Nam'}
CONTEXT=a.label_context(META)


def parse(text,known=()):return a.parse_google_address(text,CONTEXT,set(known))


def test_complete_source_claims_no_coordinate_inference():
    p=parse('\ue3c8 301B Đ. Tôn Đản, Khánh Hội, Hồ Chí Minh 700000, Việt Nam')
    assert p['house_identifier']=='301b' and p['street_name']=='Tôn Đản'
    assert p['ward_name']=='Khánh Hội' and p['city_name']=='Hồ Chí Minh'
    assert p['legacy_district'] is None and p['street_analysis_eligible']


@pytest.mark.parametrize('house',['122','122A','122/14','183B6','80 Bis','K62','956W6'])
def test_house_identifiers_keep_meaningful_distinctions(house):
    p=parse(house+' Tôn Đản, Khánh Hội, Hồ Chí Minh, Việt Nam')
    assert p['house_identifier']==house.replace(' ','').casefold()


def test_range_is_not_single_door_and_reverse_range_is_uncertain():
    p=parse('158-160 Hoàng Diệu, Khánh Hội, Hồ Chí Minh, Việt Nam')
    assert p['house_identifier']=='158-160' and p['house_kind']=='RANGE'
    p=parse('7-1, Đường Số 10A, Khánh Hội, Hồ Chí Minh, Việt Nam')
    assert p['house_identifier'] is None and p['address_parse_status']=='AMBIGUOUS'


def test_multiple_house_candidates_do_not_pick_first():
    p=parse('198/271B, 231B Đ. Vĩnh Khánh, Khánh Hội, Hồ Chí Minh, Việt Nam')
    assert p['house_identifier'] is None and p['street_name']=='Vĩnh Khánh'
    assert p['street_analysis_eligible'] and p['address_parse_status']=='AMBIGUOUS'
    assert len(p['evidence']['house_candidates'])==2
    p=parse('500 32 Đoàn Văn Bơ, Khánh Hội, Hồ Chí Minh, Việt Nam')
    assert p['house_identifier'] is None and not p['street_analysis_eligible']


def test_alley_lot_and_plus_codes():
    p=parse('27 Hẻm 150 Bến Vân Đồn, Khánh Hội, Hồ Chí Minh, Việt Nam')
    assert p['house_identifier']=='27' and p['alley_identifier']=='150' and p['street_name']=='Bến Vân Đồn'
    p=parse('Chung cư, 016 Lô A, Tôn Thất Thuyết, Khánh Hội, Hồ Chí Minh, Việt Nam',['ton that thuyet'])
    assert p['house_identifier']=='016' and p['lot_identifier']=='a' and p['street_name']=='Tôn Thất Thuyết'
    p=parse('QP62+Q96, Nguyễn Hữu Hào, Khánh Hội, Hồ Chí Minh, Việt Nam',['nguyen huu hao'])
    assert p['house_identifier'] is None and p['street_name']=='Nguyễn Hữu Hào'


def test_lot_numbered_street_is_not_house():
    p=parse('Lô C2 Số 45, Khánh Hội, Hồ Chí Minh, Việt Nam',['so 45'])
    assert p['lot_identifier']=='c2' and p['house_identifier'] is None
    assert p['normalized_street_name']=='so 45'
    assert parse('Số 45, Khánh Hội, Hồ Chí Minh, Việt Nam',['so 45'])['house_identifier'] is None


@pytest.mark.parametrize('physical',[
    'Tân Vĩnh/83 Khánh Hội',
    'Bến Vân Đồn/G42 Đ. Số 50',
    'Đ. Số 50/G28A Đ. cư xá Vĩnh Hội',
    '40/53A, Đ. Vĩnh Khánh Đ. Vĩnh Hội',
])
def test_compound_street_does_not_become_dictionary_label(physical):
    p=parse(physical+', Khánh Hội, Hồ Chí Minh, Việt Nam',['tan vinh','khanh hoi','ben van don'])
    assert p['street_name'] is None and not p['street_analysis_eligible']
    assert p['address_parse_status']=='AMBIGUOUS'


def test_no_street_from_locality_premise_or_subarea():
    for text in ['Khánh Hội, Hồ Chí Minh, Việt Nam','Chung cư H1, Khánh Hội, Hồ Chí Minh, Việt Nam','136 Tổ 34, Khánh Hội, Hồ Chí Minh, Việt Nam']:
        assert parse(text)['street_name'] is None
    p=parse('Đ. cư xá Vĩnh Hội, Khánh Hội, Hồ Chí Minh, Việt Nam')
    assert p['street_name']=='cư xá Vĩnh Hội' and not p['street_analysis_eligible']


def test_unlabeled_locality_not_guessed_as_ward_or_district():
    p=parse('19 Cao Thắng, Phú Nhuận, Hồ Chí Minh, Việt Nam')
    assert p['ward_name'] is None and p['legacy_district'] is None
    assert p['locality_name_raw']=='Phú Nhuận' and p['street_name']=='Cao Thắng'
    p=parse('12 Tôn Đản, Phường 9, Quận 4, Hồ Chí Minh, Việt Nam')
    assert p['ward_name']=='9' and p['legacy_district']=='4'


def test_conflicting_source_admin_claims_not_silently_repaired():
    p=parse('36 Nguyễn Hữu Hào phường 9 Quận 4, Khánh Hội, Hồ Chí Minh, Việt Nam')
    assert p['ward_name'] is None and p['legacy_district']=='4'
    assert 'CONFLICTING_SOURCE_WARD_NAME_CLAIMS' in p['parse_notes']


def fixture_data():
    g=dict(google_id_kind='data_id',google_identity_value='google1',name_display='Quán Ốc 284',
        address_display='284 Tôn Đản, Khánh Hội, Hồ Chí Minh, Việt Nam',field_provenance_json='{}',
        observed_at='2026-11-05T00:00:00Z',source_run='run1',fallback_fields=[])
    e=dict(overture_id='ov1',google_id_kind='data_id',google_identity_value='google1',classification='MATCH_CANDIDATE',
        er_status='ER_PENDING',rule_version=a.ER_VERSION,observation_ref='source.sqlite3#dispatch:1')
    ov=[dict(source_id='ov1',name='Quán Ốc 284',address=None),dict(source_id='ov2',name='Ốc Tú',address='232/75 Đoàn Văn Bơ')]
    refs={k:dict(path=k+'.parquet',sha256='hash') for k in ['places','er','overture','admin_labels']}
    return [g],[e],ov,[dict(overture_id='ov2')],refs


def build(data):
    g,e,o,z,refs=data
    return a.build(g,e,o,z,CONTEXT,refs)


def test_provisional_only_and_preserve_null_source():
    tables,summary=build(fixture_data());links=tables['provisional_poi_address_links.parquet'].to_pylist()
    assert len(links)==2 and links[0]['enrichment_status']=='PROVISIONAL'
    assert links[0]['overture_address_raw'] is None and links[0]['preferred_address_source']=='google'
    assert links[0]['er_status']=='ER_PENDING'
    assert links[1]['enrichment_status']=='NOT_ENRICHED_NO_GOOGLE_CANDIDATE'
    assert links[1]['preferred_address_raw']=='232/75 Đoàn Văn Bơ' and links[1]['google_identity_value'] is None


@pytest.mark.parametrize('classification',['REVIEW','INSUFFICIENT_DATA','NON_MATCH_CANDIDATE'])
def test_no_google_address_transferred_for_other_er_classes(classification):
    data=fixture_data();data[1][0]['classification']=classification
    t,_=build(data);row=t['provisional_poi_address_links.parquet'].to_pylist()[0]
    assert row['preferred_address_source']=='overture' and row['preferred_address_raw'] is None
    assert all(row[f] is None for f in a.PARSED_FIELDS)
    assert row['google_identity_value'] is None


def test_shared_identity_does_not_merge_sources_or_double_count_dictionary():
    g,e,o,z,refs=fixture_data();e.append({**e[0],'overture_id':'ov2'})
    t,su=build((g,e,o,[],refs));links=t['provisional_poi_address_links.parquet'].to_pylist()
    assert len(links)==2 and all(r['enrichment_status']=='PROVISIONAL' and r['shared_google_identity'] for r in links)
    assert t['street_dictionary.parquet']['google_poi_count'].to_pylist()==[1]


def test_multiple_candidates_hold_and_keep_both_relationships():
    g,e,o,z,refs=fixture_data();g.append({**g[0],'google_identity_value':'google2'})
    e.append({**e[0],'google_identity_value':'google2','classification':'REVIEW'})
    t,_=build((g,e,o,z,refs));row=t['provisional_poi_address_links.parquet'].to_pylist()[0]
    assert row['enrichment_status']=='NOT_ENRICHED_MULTIPLE_CANDIDATES' and row['preferred_address_source']=='overture'
    assert len(json.loads(row['candidate_relationships_json']))==2


def test_missing_google_address_not_success_and_empty_dictionary_schema():
    data=fixture_data();data[0][0]['address_display']=None
    t,_=build(data)
    assert t['google_address_enrichment.parquet']['address_parse_status'].to_pylist()==['MISSING']
    assert t['street_dictionary.parquet'].num_rows==0
    assert t['provisional_poi_address_links.parquet']['enrichment_status'][0].as_py()=='NOT_ENRICHED_MISSING_GOOGLE_ADDRESS'


def test_identity_dedup_is_technical_only_and_order_deterministic():
    g,e,o,z,refs=fixture_data();g.append(deepcopy(g[0]))
    t,su=build((g,e,o,z,refs));assert su['technical_exact_duplicates_removed']==1
    g.append({**g[0],'google_identity_value':'google2'})
    t,su=build((g,e,o,z,refs));t2,su2=build((list(reversed(g)),list(reversed(e)),list(reversed(o)),list(reversed(z)),refs))
    assert t['google_address_enrichment.parquet'].num_rows==2
    assert su==su2 and all(t[n].to_pylist()==t2[n].to_pylist() for n in t)
    assert t['street_dictionary.parquet']['google_poi_count'].to_pylist()==[2]


def test_conflicting_identity_rows_fail_and_no_final_er_overwrite():
    data=fixture_data();data[0].append({**data[0][0],'address_display':'different'})
    with pytest.raises(ValueError,match='duplicate Silver'):build(data)
    data=fixture_data();data[1][0]['er_status']='MATCH'
    with pytest.raises(ValueError,match='unconfirmed'):build(data)


def test_dictionary_aliases_do_not_merge_house_numbers_or_impute_city():
    g,e,o,z,refs=fixture_data()
    g[0]['address_display']='79 Đường 41, Khánh Hội, Hồ Chí Minh, Việt Nam'
    g.append({**g[0],'google_identity_value':'google2','address_display':'71 Đ. Số 41, Khánh Hội, Hồ Chí Minh, Việt Nam'})
    t,su=build((g,e,o,z,refs))
    assert t['google_address_enrichment.parquet']['house_identifier'].to_pylist()==['79','71']
    assert t['street_dictionary.parquet'].num_rows==1
    assert su['top_20_streets'][0]['google_poi_count']==2
    assert su['top_20_streets'][0]['normalized_street_name']=='so 41'


@pytest.fixture
def published_inputs(tmp_path):
    g,e,o,z,_=fixture_data();ov=tmp_path/'ov/current_state.parquet';ov.parent.mkdir()
    pq.write_table(pa.Table.from_pylist(o),ov)
    def release(root,artifacts,signature):
        dest=root/'releases'/('a'*24);dest.mkdir(parents=True);specs={}
        for name,content in artifacts.items():
            path=dest/name;path.parent.mkdir(parents=True,exist_ok=True)
            if isinstance(content,list):
                pq.write_table(pa.Table.from_pylist(content),path)
                specs[name]=dict(sha256=s.sha(path),rows=len(content),logical_sha256=_digest(content))
            else:
                path.write_text(json.dumps(content),encoding='utf-8');specs[name]=dict(sha256=s.sha(path))
        (dest/'manifest.json').write_text(json.dumps(dict(signature=signature,artifacts=specs)),encoding='utf-8')
        (root/'current.json').write_text(json.dumps(dict(release_id='a'*24,manifest_sha256=s.sha(dest/'manifest.json'))),encoding='utf-8')
        return dest
    mroot=tmp_path/'m1'
    mr=release(mroot,{'silver/google_places.parquet':g,'silver/source_links.parquet':[{k:e[0][k] for k in ['overture_id',*a.KEY]}],
        'reports/google_silver_dq_summary.json':{'zero_candidate_inventory':z}},dict(inputs_sha256={str(ov.resolve()):s.sha(ov)}))
    eroot=tmp_path/'er'
    release(eroot,{'er_results.parquet':e},dict(rule_version=a.ER_VERSION,inputs_sha256={str(p.resolve()):s.sha(p) for p in [ov,mr/'silver/google_places.parquet',mr/'silver/source_links.parquet']}))
    labels=tmp_path/'admin.json';labels.write_text(json.dumps(META),encoding='utf-8')
    return mroot,eroot,labels,ov


def test_atomic_publication_and_idempotence(published_inputs,tmp_path):
    m,e,labels,ov=published_inputs;out=tmp_path/'enrichment'
    protected={p:s.sha(p) for root in [m,e] for p in root.rglob('*') if p.is_file()}
    def fail(stage):
        if stage=='after_stage_validation':raise RuntimeError('injected')
    with pytest.raises(RuntimeError):a.publish(m,e,out,labels,fault=fail)
    assert not (out/'current.json').exists() and not (out/'.publish.lock').exists()
    r,one=a.publish(m,e,out,labels);pointer=(out/'current.json').read_bytes()
    r2,two=a.publish(m,e,out,labels)
    assert r==r2 and one==two and pointer==(out/'current.json').read_bytes()
    assert all(s.sha(p)==h for p,h in protected.items())
    ov.write_bytes(b'corrupt')
    with pytest.raises(ValueError,match='lineage|mismatch'):a.publish(m,e,out,labels)
    assert pointer==(out/'current.json').read_bytes()
