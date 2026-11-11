from copy import deepcopy
import csv
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from fnb_platform import gold_staging as g, category_quality as category, google_silver as s
from fnb_platform.overture_incremental import _digest


def fixture_inputs(tmp_path):
    root=tmp_path/'inputs';root.mkdir()
    overture=[dict(source_id=f'ov{i}',name=f'Overture fixture {i}',address=f'{i} Fixture Street',latitude=10.75,longitude=106.7,
        source_operating_status=None,source_release='fixture',source_manifest_sha256='fixture') for i in range(1,4)]
    google=[dict(google_id_kind='data_id',google_identity_value=f'0x{i}:0x{i}',name_display=f'Google fixture {i}',address_display=f'{i} Fixture Street',
        latitude=10.75001,longitude=106.7,business_status='UNKNOWN',field_provenance_json='{}',source_url='https://www.google.com/maps/place/observed') for i in range(1,3)]
    norm=[]
    for o in overture:
        norm.append(dict(source='overture',source_id=o['source_id'],google_id_kind=None,google_identity_value=None,baseline_fnb_candidate=True,
            normalized_categories=['restaurant'],source_provenance_json='{"fixture":"overture"}',dq_flags=[]))
    for place in google:
        norm.append(dict(source='google',source_id=None,google_id_kind=place['google_id_kind'],google_identity_value=place['google_identity_value'],baseline_fnb_candidate=None,
            normalized_categories=['restaurant'],source_provenance_json='{"fixture":"google"}',dq_flags=[]))
    effective=[];cats=[]
    for i in range(1,3):
        effective.append(dict(overture_id=f'ov{i}',google_id_kind='data_id',google_identity_value=f'0x{i}:0x{i}',
            effective_status='AUTO_ACCEPTED' if i==1 else 'PENDING',decision_source='AUTO_POLICY' if i==1 else 'ER_PROPOSAL',policy_version='er-auto-accept-v1',
            evidence_hash=f'evidence-{i}',source_provenance_json='{"fixture":"mapping"}',shadow_only=False,activation_scope='GOLD_STAGING_ONLY',
            is_identity_human_verified=False,category_release_id='cat-fixture'))
        cats.append(dict(overture_id=f'ov{i}',google_id_kind='data_id',google_identity_value=f'0x{i}:0x{i}',category_relation='CONSISTENT'))
    paths={}
    for name,rows in [('overture',overture),('google',google),('category_normalization',norm),('category_audit',cats),('effective_mapping',effective)]:
        path=root/(name+'.parquet');pq.write_table(pa.Table.from_pylist(rows),path);paths[name+'_path']=path
    hashes={str(p):s.sha(p) for p in paths.values()}
    return paths,hashes,effective,lambda:None


def sample_fixture():
    rows=[];cats=[];places=[]
    for i in range(60):
        row=dict(overture_id=f'ov{i}',google_id_kind='data_id',google_identity_value=f'0x1:0x{i+1:x}',
            effective_status='AUTO_ACCEPTED',distance_m=(i%3)*15+1,name_similarity=.95,
            overture_name=f'Overture fixture {i}',google_name=f'Google fixture {i}',overture_address=None if i%5==0 else f'{i} Fixture Street',google_address=f'{i} Fixture Street',
            decision_source='AUTO_POLICY',policy_version='er-auto-accept-v1',evidence_hash=f'evidence-{i}',evidence_json='{}',reason_codes=['FIXTURE'],source_provenance_json='{}',er_release_id='er-fixture',category_release_id='cat-fixture')
        rows.append(row)
        cats.append(dict(overture_id=row['overture_id'],google_id_kind='data_id',google_identity_value=row['google_identity_value'],
            overture_categories=['restaurant' if i%2 else 'cafe_coffee'],google_categories=['restaurant'],overture_categories_raw='{"fixture":"raw"}',google_categories_raw='{"fixture":"google"}'))
        places.append(dict(google_id_kind='data_id',google_identity_value=row['google_identity_value'],source_url='https://www.google.com/maps/place/observed'))
    return rows,cats,places


def test_stratified_sample_reproducible_unique_unlabelled_preserves_values():
    rows,cats,places=sample_fixture();original=deepcopy(rows)
    sample,summary=g.validation_sample(rows,cats,places,size=48,seed=123,release_id='release-fixture')
    again,_=g.validation_sample(list(reversed(rows)),cats,places,size=48,seed=123,release_id='release-fixture')
    assert sample==again and len(sample)==48 and len({r['validation_id'] for r in sample})==48
    assert all(not r['human_label'] and not r['reviewer_note'] and not r['reviewer'] for r in sample)
    assert len(summary['strata'])==6 and sum(s['sample'] for s in summary['strata'])==48
    assert all(r['inclusion_probability']==r['stratum_sample']/r['stratum_population'] for r in sample)
    assert summary['precision_estimate'] is None and rows==original
    assert any(r['overture_address'] is None for r in sample)
    other,_=g.validation_sample(rows,cats,places,size=48,seed=321,release_id='release-fixture')
    assert {r['validation_id'] for r in sample}!={r['validation_id'] for r in other}


def test_store_ids_independent_of_order_and_accepted_google_identity():
    assert g.store_id('ov1')==g.store_id('ov1') and g.store_id('ov1')!=g.store_id('ov2')
    assert not g.store_id('ov1').endswith('1')


def test_real_dbt_fixture_build_coverage_no_unresolved_transfer_and_replay(tmp_path,monkeypatch):
    pytest.importorskip('dbt.cli.main')
    data=fixture_inputs(tmp_path);monkeypatch.setattr(g,'pin_inputs',lambda *args:data)
    active,cat,out=tmp_path/'active',tmp_path/'category',tmp_path/'staging'
    d,summary=g.stage_gold(active,cat,out)
    assert summary['accepted_mapping_count']==1 and summary['canonical_store_count']==3
    assert summary['source_only_store_count']==2 and summary['no_google_candidate_ids']==1
    assert summary['unresolved_mapping_count']==1 and summary['unassigned_google_identities']==1
    assert summary['dbt_build']['failures']==0 and summary['dbt_build']['tests']>=10
    stores={r['overture_id']:r for r in pq.read_table(d/'dim_store.parquet').to_pylist()}
    assert stores['ov1']['store_name']=='Google fixture 1' and stores['ov1']['category_validation_status']=='PROVISIONAL_SOURCE_AGREEMENT'
    assert stores['ov2']['store_name']=='Overture fixture 2' and stores['ov2']['google_identity_value'] is None
    assert not stores['ov1']['is_identity_human_verified'] and not stores['ov2']['is_identity_human_verified']
    assert stores['ov3']['store_id']==g.store_id('ov3') and stores['ov3']['business_status']=='UNKNOWN'
    assert json.loads(stores['ov1']['field_provenance_json'])['address_source']=='google'
    snapshot=(out/'current.json').read_bytes();again,_=g.stage_gold(active,cat,out)
    assert d==again and snapshot==(out/'current.json').read_bytes()
    # Independent rebuild also produces identical logical Parquet outputs.
    other,_=g.stage_gold(active,cat,tmp_path/'independent')
    for name in ('dim_store','store_source_mapping','link_accounting','source_accounting'):
        assert pq.read_table(d/(name+'.parquet')).equals(pq.read_table(other/(name+'.parquet')))


def test_publication_failure_recovery_keeps_active_and_history(tmp_path):
    inputs=tmp_path/'inputs';inputs.mkdir()
    source=inputs/'source.txt';source.write_text('immutable')
    output=tmp_path/'derived';signature=dict(rule_version='fixture-v1',publication_kind='STAGING_FIXTURE',inputs_sha256={str(source):s.sha(source)},code_sha256={})
    def writer(stage,rid):return {'table.parquet':pa.Table.from_pylist([dict(id='fixture')])},{'fixture':True},[]
    d,_=g.publish_release(output,signature,writer);before=(output/'current.json').read_bytes()
    signature['revision']=2
    for target in ('after_stage_validation','after_release_rename','before_pointer_replace'):
        def fault(stage):
            if stage==target:raise RuntimeError('injected')
        with pytest.raises(RuntimeError,match='injected'):g.publish_release(output,signature,writer,fault=fault)
        assert (output/'current.json').read_bytes()==before and s.read_active(output)==d
    final,_=g.publish_release(output,signature,writer)
    assert final!=d and source.read_text()=='immutable'
    assert g.publish_release(output,signature,writer)[0]==final


def test_quality_gate_rejects_dangling_or_unauthorized_google_assignment():
    registry=pa.Table.from_pylist([dict(overture_id='ov1',store_id=g.store_id('ov1'))])
    tables={'dim_store.parquet':pa.Table.from_pylist([dict(overture_id='ov1',store_id='wrong')]),'store_source_mapping.parquet':pa.Table.from_pylist([]),
        'source_accounting.parquet':pa.Table.from_pylist([]),'link_accounting.parquet':pa.Table.from_pylist([])}
    with pytest.raises(ValueError,match='anchor coverage'):g.validate_staging(tables,registry,[],set())


def test_dbt_rejects_shared_google_merge_and_preserves_active(tmp_path,monkeypatch):
    pytest.importorskip('dbt.cli.main')
    data=fixture_inputs(tmp_path);monkeypatch.setattr(g,'pin_inputs',lambda *args:data)
    out=tmp_path/'staging'
    prior,_=g.stage_gold(tmp_path/'active',tmp_path/'category',out)
    before=(out/'current.json').read_bytes()
    data[2][1].update(effective_status='AUTO_ACCEPTED',google_identity_value='0x1:0x1')
    path=data[0]['effective_mapping_path'];pq.write_table(pa.Table.from_pylist(data[2]),path)
    data[1][str(path)]=s.sha(path)
    with pytest.raises(ValueError,match='dbt build failed'):
        g.stage_gold(tmp_path/'active',tmp_path/'category',out)
    assert (out/'current.json').read_bytes()==before and s.read_active(out)==prior


def test_human_identity_acceptance_does_not_verify_conflicting_category(tmp_path,monkeypatch):
    pytest.importorskip('dbt.cli.main')
    data=fixture_inputs(tmp_path)
    data[2][0].update(effective_status='HUMAN_ACCEPTED',decision_source='HUMAN')
    path=data[0]['effective_mapping_path'];pq.write_table(pa.Table.from_pylist(data[2]),path);data[1][str(path)]=s.sha(path)
    cp=data[0]['category_audit_path'];cats=pq.read_table(cp).to_pylist();cats[0]['category_relation']='CATEGORY_CONFLICT'
    pq.write_table(pa.Table.from_pylist(cats),cp);data[1][str(cp)]=s.sha(cp)
    monkeypatch.setattr(g,'pin_inputs',lambda *args:data)
    dest,_=g.stage_gold(tmp_path/'active',tmp_path/'category',tmp_path/'staging')
    row=next(r for r in pq.read_table(dest/'dim_store.parquet').to_pylist() if r['overture_id']=='ov1')
    assert row['is_identity_human_verified'] and row['canonical_category']=='unknown' and row['category_validation_status']=='PENDING'


def test_activation_recomputes_policy_and_keeps_editable_sample_separate(tmp_path,monkeypatch):
    from test_er_auto_accept import fixture,evaluate
    data=fixture();table,policy_summary=evaluate(data)
    sources=tmp_path/'inputs';sources.mkdir()
    pins={}
    for name,rows in [('er_results.parquet',data[0]),('category_audit.parquet',data[1]),('google_places.parquet',data[3])]:
        path=sources/name;pq.write_table(pa.Table.from_pylist(rows),path);pins[str(path.resolve())]=s.sha(path)
    for name,content in [('identity_review_AI_triage.csv','fixture'),('audit_summary.json','{}'),('er_auto_accept_policy_v1.json',g.auto.DEFAULT_POLICY.read_text())]:
        path=sources/name;path.write_text(content,encoding='utf-8');pins[str(path.resolve())]=s.sha(path)
    root=tmp_path/'shadow';sd=root/'releases'/('f'*24);sd.mkdir(parents=True)
    pq.write_table(table,sd/'effective_identity_mapping.parquet')
    spec=dict(rows=table.num_rows,sha256=s.sha(sd/'effective_identity_mapping.parquet'),logical_sha256=_digest(table.to_pylist()))
    manifest=dict(release_id=sd.name,signature=dict(rule_version=g.auto.VERSION,inputs_sha256=pins,human_pointer_sha256=None),artifacts={'effective_identity_mapping.parquet':spec})
    (sd/'manifest.json').write_text(s.dump(manifest));(root/'current.json').write_text(s.dump(dict(release_id=sd.name,manifest_sha256=s.sha(sd/'manifest.json'))))
    calls=[]
    def recompute(*args):calls.append(args);return sd,policy_summary
    monkeypatch.setattr(g.auto,'publish',recompute)
    active,summary=g.activate(root,tmp_path/'active',human_root=tmp_path/'human',validation_root=tmp_path/'reviews')
    assert len(calls)==1
    promoted=pq.read_table(active/'effective_identity_mapping.parquet').to_pylist()
    assert all(not r['shadow_only'] and r['activation_scope']=='GOLD_STAGING_ONLY' for r in promoted)
    assert all(r['identity_verification_status']!='HUMAN_VERIFIED_LINK' for r in promoted)
    assert [r['evidence_hash'] for r in promoted]==[r['evidence_hash'] for r in table.to_pylist()]
    review_copy=Path(summary['validation_csv_path'])
    assert not review_copy.is_relative_to(active) and review_copy.read_bytes().startswith(b'\xef\xbb\xbf')
    with review_copy.open(encoding='utf-8-sig',newline='') as f:rows=list(csv.DictReader(f))
    assert len(rows)==summary['validation']['sample_size'] and all(not r['human_label'] for r in rows)
    review_copy.write_text('Existing human annotations must survive',encoding='utf-8')
    again,_=g.activate(root,tmp_path/'active',human_root=tmp_path/'human',validation_root=tmp_path/'reviews')
    assert again==active and review_copy.read_text(encoding='utf-8')=='Existing human annotations must survive'
    assert not (tmp_path/'human').exists() and s.sha(sd/'effective_identity_mapping.parquet')==spec['sha256']
    before=(tmp_path/'active/current.json').read_bytes()
    hp=tmp_path/'human/current.json';hp.parent.mkdir();hp.write_text('new human state after evaluation')
    with pytest.raises(ValueError,match='Human state changed'):
        g.activate(root,tmp_path/'active',human_root=tmp_path/'human',validation_root=tmp_path/'reviews')
    assert (tmp_path/'active/current.json').read_bytes()==before


def test_manifest_pinning_checks_transitive_overture_and_live_human_state(tmp_path):
    paths,hashes,effective,_=fixture_inputs(tmp_path)
    raw=paths['overture_path'].with_name('current_state.parquet');raw.write_bytes(paths['overture_path'].read_bytes())
    google=paths['google_path'].with_name('google_places.parquet');google.write_bytes(paths['google_path'].read_bytes())
    csig=dict(rule_version='category-quality-v1',publication_kind='CATEGORY_FIXTURE',code_sha256={},
        inputs_sha256={str(raw):s.sha(raw),str(google):s.sha(google)})
    def category_writer(stage,rid):
        return {name:pq.read_table(paths[name.replace('.parquet','')+'_path']) for name in ('category_audit.parquet','category_normalization.parquet')},{},[]
    cr=tmp_path/'category';c,_=g.publish_release(cr,csig,category_writer)
    hroot=tmp_path/'human'
    apins={str(p.resolve()):s.sha(p) for p in (google,c/'category_audit.parquet',c/'category_normalization.parquet',c/'manifest.json')}
    asign=dict(rule_version=g.ACTIVE_VERSION,publication_kind='ACTIVE_EFFECTIVE_MAPPING_FOR_GOLD_STAGING',code_sha256={},inputs_sha256=apins,human_root=str(hroot),human_pointer_sha256=None)
    def active_writer(stage,rid):
        data=[dict(r,category_release_id=c.name) for r in effective]
        return {'effective_identity_mapping.parquet':pa.Table.from_pylist(data)},{},[]
    ar=tmp_path/'active';a,_=g.publish_release(ar,asign,active_writer)
    pinned,_,_,guard=g.pin_inputs(ar,cr)
    assert pinned['overture_path']==raw and str(raw) not in asign['inputs_sha256']
    hroot.mkdir();(hroot/'current.json').write_text('new human state')
    with pytest.raises(ValueError,match='Human decisions changed'):guard()
    (hroot/'current.json').unlink();raw.write_bytes(b'changed-source')
    with pytest.raises(ValueError,match='source checksum mismatch'):g.pin_inputs(ar,cr)
