import json
from copy import deepcopy

import pytest

from fnb_platform.integration import mapping_publication as mapping
from test_er_auto_accept import fixture, history


def evaluate(data=None, ledger=()):
    data=data or fixture()
    return mapping.evaluate(data,data[3],list(ledger),{})


def test_no_ai_accepts_strict_er_and_keeps_uncertain_separate():
    data=fixture(); before=deepcopy(data)
    table,summary=evaluate(data)
    rows={r['overture_id']:r for r in table.to_pylist()}
    assert rows['ov-clean']['final_decision']=='MATCH'
    assert rows['ov-clean']['identity_verification_status']=='MACHINE_ACCEPTED'
    assert all(r['final_decision']!='MATCH' for k,r in rows.items() if k.startswith('ov-shared'))
    assert all(not r['ai_suggested_label'] and not r['shadow_only'] for r in rows.values())
    assert summary['mode']=='CANONICAL_MAPPING_NO_AI' and data==before


@pytest.mark.parametrize('label,expected',[('MATCH','MATCH'),('NON_MATCH','NON_MATCH'),('UNSURE','REVIEW_REQUIRED')])
def test_current_human_precedence_and_provenance(label,expected):
    data=fixture(); ledger=history(data,[('ov-review',label)])
    table,_=evaluate(data,ledger)
    row=next(r for r in table.to_pylist() if r['overture_id']=='ov-review')
    assert row['final_decision']==expected
    assert row['human_decision_id']==ledger[0]['decision_id']


@pytest.mark.parametrize('mode',['missing','metadata','address','branch','category'])
def test_missing_or_conflicting_evidence_cannot_flow_to_gold(mode):
    data=fixture(); er=next(r for r in data[0] if r['overture_id']=='ov-clean')
    google=next(r for r in data[3] if r['google_identity_value']==er['google_identity_value'])
    evidence=json.loads(er['evidence_json'])
    if mode=='missing': google['latitude']=None
    if mode=='metadata': google['has_metadata_conflict']=True
    if mode=='address': evidence['comparisons']['v3']['address_features_v3']['clear_conflict']=True
    if mode=='branch': evidence['comparisons']['v3']['name_features']['branch_warning_codes']=['WARNING']
    if mode=='category':
        next(r for r in data[1] if mapping.legacy.key(r)==mapping.legacy.key(er))['category_relation']='CATEGORY_CONFLICT'
    er['evidence_json']=json.dumps(evidence)
    table,_=evaluate(data)
    row=next(r for r in table.to_pylist() if r['overture_id']=='ov-clean')
    assert row['final_decision']!='MATCH' and row['effective_status'] not in mapping.staging.ACCEPTED


def test_direct_publication_replay_integrity_and_failure_safety(tmp_path,monkeypatch):
    import pyarrow as pa
    import pyarrow.parquet as pq
    data=fixture(); source=tmp_path/'source'; source.mkdir()
    gp=source/'google_places.parquet'; pq.write_table(pa.Table.from_pylist(data[3]),gp)
    data[4]['refs']['google_places']=dict(path=str(gp),sha256=mapping.silver.sha(gp))
    monkeypatch.setattr(mapping.review,'load',lambda *args:(data,{str(gp):mapping.silver.sha(gp)},[source]))
    original_read=mapping.silver.read_active
    monkeypatch.setattr(mapping.silver,'read_active',lambda root: source if str(root) in ('er','cat') else original_read(root))
    out=tmp_path/'mapping'; human=tmp_path/'human'
    (source/'manifest.json').write_text(json.dumps(dict(signature=dict(inputs_sha256={str(gp):mapping.silver.sha(gp)}),artifacts={})))
    args=('er','cat',human,out)
    release,summary=mapping.publish(*args)
    original=(out/'current.json').read_bytes()
    assert mapping.publish(*args)[0]==release
    assert (out/'current.json').read_bytes()==original and not human.exists()
    manifest=mapping.silver.verify_release(release)
    assert manifest['signature']['mapping_version']==mapping.VERSION
    assert summary['mode']=='CANONICAL_MAPPING_NO_AI'
    def fault(stage):
        if stage=='before_pointer_replace': raise RuntimeError('injected failure')
    with pytest.raises(RuntimeError,match='injected'):
        mapping.publish(*args,fault=fault)
    assert (out/'current.json').read_bytes()==original
    (release/'effective_identity_mapping.parquet').write_bytes(b'corrupt fixture')
    with pytest.raises(ValueError,match='checksum'):
        mapping.publish(*args)


@pytest.mark.parametrize('classification,expected',[('NON_MATCH_CANDIDATE','NON_MATCH'),('INSUFFICIENT_DATA','INSUFFICIENT_DATA'),('REVIEW','REVIEW_REQUIRED')])
def test_explicit_nonaccepted_decisions(classification,expected):
    row=dict(effective_status='PENDING',human_label=None,er_classification=classification,support_gaps=[])
    assert mapping.decision(row)==expected
