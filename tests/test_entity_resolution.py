import csv
import io
import json
from copy import deepcopy

import pytest

from fnb_platform.comparison_features import build as features
from fnb_platform.entity_resolution import DEFAULTS, QUEUE, build, classify, distance_m, name_similarity


def fixture_rows():
    ov = dict(source_id="ov1", name="Ốc Cúc", name_normalized="ốc cúc", address="129 Vĩnh Khánh",
        address_normalized="129 vĩnh khánh", latitude=10.76, longitude=106.70, source_release="test")
    gg = dict(google_id_kind="data_id", google_identity_value="google1", name_display="Ốc Cúc",
        name_normalized="oc cuc", address_display="129 Vĩnh Khánh", address_normalized="129 vinh khanh",
        field_provenance_json="{}", latitude=10.76, longitude=106.70, fallback_fields=[],
        observed_at="2026-11-05T00:00:00Z", metadata_status="INCOMPLETE_METADATA",
        conflicting_values_json="{}", conflict_fields=[])
    link = dict(overture_id="ov1", google_id_kind="data_id", google_identity_value="google1",
        dq_flags=[], source_observation_refs=["test.sqlite3#dispatch:1"], selection_policies=[],
        observed_at="2026-11-05T00:00:00Z", source_run="run1")
    return [ov], [gg], [link]


def run(ov, gg, links):
    import pyarrow as pa
    refs={k:("test.parquet","hash") for k in ("overture","places","links")}
    tables, _ = features(*(pa.Table.from_pylist(r) for r in (ov,gg,links)),refs)
    return build(*(tables["comparison/"+n+".parquet"].to_pylist() for n in
        ("overture_features","google_features","address_pair_evidence")),ov,gg,links,
        dict(overture="ov.parquet",places="gg.parquet",pairs="pairs.parquet"))


def test_symmetric_and_missing_names():
    assert name_similarity("tide", "diet") == name_similarity("diet", "tide")
    assert name_similarity(None,"abc") is None


def test_distance_and_invalid():
    assert distance_m(dict(latitude=0,longitude=0),dict(latitude=0,longitude=1)) == pytest.approx(111195.08,abs=.1)
    assert distance_m(dict(latitude=91,longitude=0),dict(latitude=0,longitude=0)) is None
    assert distance_m(dict(latitude=float('nan'),longitude=0),dict(latitude=0,longitude=0)) is None


def test_proposal_never_confirmed_and_contract():
    table, queue, _=run(*fixture_rows())
    r=table.to_pylist()[0]
    assert r['classification']=='MATCH_CANDIDATE' and r['er_status']=='ER_PENDING'
    assert not queue
    for f in json.loads(r['flags_json']):
        assert all(f[k] for k in ('flag_code','reason','evidence_json','observation_ref','rule_version','recommended_action'))
        assert 'Ốc Cúc' in f['reason']
        assert json.loads(f['evidence_json'])['overture']['name_raw']=='Ốc Cúc'


def test_wrong_branch_and_address_difference():
    ov,gg,li=fixture_rows()
    gg[0].update(name_display="Pizza Hut",address_display="397 Nguyễn Tri Phương",latitude=10.80)
    table,queue,_=run(ov,gg,li)
    assert table['classification'].to_pylist()==['NON_MATCH_CANDIDATE']
    assert 'CURRENT_PARSED_STREET_DIFFERENCE' in {r['flag_code'] for r in queue}


def test_null_metadata_and_address_not_conflict():
    ov,gg,li=fixture_rows()
    gg[0].update(name_display=None,address_display=None,latitude=None)
    table,queue,_=run(ov,gg,li)
    assert table['classification'].to_pylist()==['INSUFFICIENT_DATA']
    assert not any('DIFFERENCE' in r['flag_code'] for r in queue)


@pytest.mark.parametrize('address',['Hẻm 128 Vĩnh Khánh','Trước cửa nhà 128 Vĩnh Khánh'])
def test_partial_ambiguous_never_clear_conflict(address):
    ov,gg,li=fixture_rows();gg[0]['address_display']=address
    table,_,_=run(ov,gg,li)
    assert not json.loads(table['evidence_json'][0].as_py())['comparisons']['clear_house_conflict']


def test_historical_conflict_not_current_conclusion():
    ov,gg,li=fixture_rows();li[0]['dq_flags']=['STREET_CONFLICT']
    t,_,_=run(ov,gg,li)
    assert t['classification'][0].as_py()=='MATCH_CANDIDATE'
    assert json.loads(t['evidence_json'][0].as_py())['historical_m1_flags']==['STREET_CONFLICT']


def test_shared_and_multiple_links_determinism_csv_roundtrip():
    ov,gg,li=fixture_rows()
    ov.append({**ov[0],'source_id':'ov2','name':'Ốc "Cúc", hai'})
    li.append({**li[0],'overture_id':'ov2'})
    gg.append({**gg[0],'google_identity_value':'google2'})
    li.append({**li[0],'google_identity_value':'google2'})
    t,q,r=run(ov,gg,li)
    assert set(t['classification'].to_pylist())=={'REVIEW'}
    t2,q2,r2=run(list(reversed(ov)),list(reversed(gg)),list(reversed(li)))
    assert t.to_pylist()==t2.to_pylist() and q==q2 and r==r2
    stream=io.StringIO();w=csv.DictWriter(stream,fieldnames=QUEUE);w.writeheader();w.writerows(q)
    stream.seek(0);rows=list(csv.DictReader(stream))
    assert rows==[{k:'' if v is None else str(v) for k,v in row.items()} for row in q]
    assert all(json.loads(row['evidence_json']) for row in rows)


def test_key_loss_rejected():
    ov,gg,li=fixture_rows()
    li.append(deepcopy(li[0]))
    with pytest.raises(ValueError):run(ov,gg,li)


def test_threshold_boundaries_and_combined_evidence():
    assert classify(.9,100,False,False,DEFAULTS)=='MATCH_CANDIDATE'
    assert classify(.59,501,False,False,DEFAULTS)=='REVIEW'
    assert classify(.59,501,True,False,DEFAULTS)=='NON_MATCH_CANDIDATE'
    assert classify(1,0,True,False,DEFAULTS)=='REVIEW'
    assert classify(1,0,False,True,DEFAULTS)=='REVIEW'


@pytest.fixture
def published_features(tmp_path):
    import pyarrow as pa
    import pyarrow.parquet as pq
    from fnb_platform import google_silver as silver, comparison_features as cf
    ov,gg,li=fixture_rows()
    source=tmp_path/'overture'/'state.parquet';source.parent.mkdir()
    pq.write_table(pa.Table.from_pylist(ov),source)
    root=tmp_path/'m1';release=root/'releases'/('a'*24)
    (release/'silver').mkdir(parents=True);(release/'reports').mkdir()
    artifacts={}
    for name, rows in [('silver/google_places.parquet',gg),('silver/source_links.parquet',li)]:
        pq.write_table(pa.Table.from_pylist(rows),release/name)
        from fnb_platform.overture_incremental import _digest
        artifacts[name]={'sha256':silver.sha(release/name),'rows':len(rows),'logical_sha256':_digest(rows)}
    for name,value in [('reports/google_silver_dq_summary.json',json.dumps({'zero_candidate_inventory':[]})),
                       ('reports/google_silver_dq_details.csv','issue_code,overture_id,google_id_kind,google_identity_value,observation_ref,field,detail_json\n')]:
        (release/name).write_text(value,encoding='utf-8');artifacts[name]={'sha256':silver.sha(release/name)}
    (release/'manifest.json').write_text(json.dumps({'signature':{'inputs_sha256':{str(source.resolve()):silver.sha(source)}},'artifacts':artifacts}),encoding='utf-8')
    (root/'current.json').write_text(json.dumps({'release_id':'a'*24,'manifest_sha256':silver.sha(release/'manifest.json')}),encoding='utf-8')
    comparison=tmp_path/'features';cf.publish(source,root,comparison)
    return comparison,source


def test_publication_atomic_rerun_and_hash_mismatch(published_features,tmp_path):
    from fnb_platform import entity_resolution as er, google_silver as silver
    comparison,source=published_features
    out=tmp_path/'er'
    def fail(stage):
        if stage=='after_stage_validation':raise RuntimeError('injected')
    with pytest.raises(RuntimeError):er.publish(comparison,out,fault=fail)
    assert not (out/'current.json').exists() and not (out/'.publish.lock').exists()
    a,one=er.publish(comparison,out);before=(out/'current.json').read_bytes()
    b,two=er.publish(comparison,out)
    assert a==b and one==two and before==(out/'current.json').read_bytes()
    silver.verify_release(a)
    source.write_bytes(b'corrupt')
    with pytest.raises(ValueError,match='hash mismatch'):er.publish(comparison,out)
    assert before==(out/'current.json').read_bytes()


def test_existing_independent_decision_not_overwritten():
    ov,gg,li=fixture_rows();li[0]['er_status']='MATCH'
    with pytest.raises(ValueError,match='Independent ER'):run(ov,gg,li)
