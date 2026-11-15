from copy import deepcopy
import csv
import json
from pathlib import Path
import os
import subprocess
import sys

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from fnb_platform import gold_staging as g, category_quality as category, google_silver as s
from fnb_platform.overture_incremental import _digest


def test_shared_publication_has_no_domain_imports_and_preserves_old_exports():
    import ast
    from fnb_platform import publication, human_decisions, google_reviews_silver
    from fnb_platform import area_demographics, area_enrichment
    tree = ast.parse(Path(publication.__file__).read_text(encoding='utf-8'))
    imports = [node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))]
    assert all(not isinstance(node, ast.ImportFrom) or node.level == 0 for node in imports)
    assert all(not alias.name.startswith('fnb_platform') for node in imports for alias in node.names)
    assert human_decisions.publication_lock is publication.publication_lock
    assert g.require is publication.require and g.check_hashes is publication.check_hashes
    for module in (google_reviews_silver, area_demographics, area_enrichment):
        assert module.require is publication.require
        assert 'gold_staging' not in Path(module.__file__).read_text(encoding='utf-8')


def test_old_publication_entrypoint_keeps_live_verifier_and_lock(tmp_path, monkeypatch):
    from contextlib import contextmanager
    calls = []
    verify, lock = s.verify_release, g.human.publication_lock
    def checked(path):
        calls.append('verify')
        return verify(path)
    @contextmanager
    def locked(path):
        calls.append('lock')
        with lock(path):
            yield
    monkeypatch.setattr(s, 'verify_release', checked)
    monkeypatch.setattr(g.human, 'publication_lock', locked)
    signature = dict(rule_version='shared-publication-fixture', publication_kind='FIXTURE',
                     inputs_sha256={}, code_sha256={})
    def writer(stage, rid):
        return {'fixture.parquet':pa.table({'id':['fixture']})}, {'release_id':rid}, []
    root = tmp_path/'out'
    release, summary = g.publish_release(root, signature, writer)
    manifest = verify(release)
    before = {p:s.sha(p) for p in release.rglob('*') if p.is_file()}
    pointer = (root/'current.json').read_bytes()
    assert g.publish_release(root, signature, writer) == (release, summary)
    assert calls.count('lock') == 2 and calls.count('verify') >= 3
    assert manifest['release_id'] == _digest(signature)[:24]
    assert manifest['production_gold'] is False
    assert before == {p:s.sha(p) for p in before}
    assert (root/'current.json').read_bytes() == pointer


def test_shared_publication_retains_domain_schema_rejection(tmp_path):
    signature = dict(rule_version='shared-publication-fixture', publication_kind='FIXTURE',
                     inputs_sha256={}, code_sha256={})
    def writer(stage, rid):
        (stage/'silver').mkdir()
        return {'silver/google_places.parquet':pa.table({'google_id_kind':['data_id']})}, {}, []
    with pytest.raises(ValueError, match='Artifact Arrow schema mismatch'):
        g.publish_release(tmp_path/'out', signature, writer)
    assert not (tmp_path/'out/current.json').exists()


@pytest.fixture
def published_reviews(tmp_path):
    from test_google_reviews_silver import fixture
    from fnb_platform import google_reviews_silver as reviews
    places, _ = fixture(tmp_path)
    root = tmp_path/'reviews'
    release, _ = reviews.publish(places, root)
    return root, release, s.read_active(places)/'silver/google_places.parquet'


def republish_review_signature(root, release, change):
    signature = json.loads((release/'manifest.json').read_text())['signature']
    change(signature)
    def writer(stage, rid):
        summary = json.loads((release/'summary.json').read_text())
        summary['release_id'] = rid
        (stage/'review_dq_summary.json').write_text(s.dump(summary))
        return {p.name:pq.ParquetFile(p).read() for p in release.glob('*.parquet')}, summary, ['review_dq_summary.json']
    return g.publish_release(root, signature, writer)[0]


def test_reviews_admission_pins_matching_lineage_and_final_guard(published_reviews):
    root, release, places = published_reviews
    paths, hashes, context, guard = g.pin_reviews(root, places)
    assert paths['reviews_current_path'] == release/'silver_reviews_current.parquet'
    assert context['reviews_release_id'] == release.name
    assert context['places_manifest_sha256'] == s.sha(places.parent.parent/'manifest.json')
    assert hashes[str(root/'current.json')] == s.sha(root/'current.json')
    guard()
    explicit = g.pin_reviews(root, places, release_id=release.name)
    assert explicit[2] == context and str(root/'current.json') not in explicit[1]
    pointer = (root/'current.json').read_bytes()
    (root/'current.json').write_bytes(pointer+b' ')
    with pytest.raises(ValueError, match='Pinned input/code changed'):
        guard()
    explicit[3]()


@pytest.mark.parametrize('damage', ['missing_manifest','corrupt','missing_artifact','schema'])
def test_reviews_admission_rejects_invalid_release(published_reviews, damage):
    root, release, places = published_reviews
    if damage == 'missing_manifest':
        obj = json.loads((root/'current.json').read_text()); obj['release_id'] = 'f'*24
        (root/'current.json').write_text(s.dump(obj))
    elif damage == 'corrupt':
        (release/'silver_reviews_current.parquet').write_bytes(b'corrupt')
    else:
        manifest = json.loads((release/'manifest.json').read_text())
        if damage == 'missing_artifact':
            manifest['artifacts'].pop('review_identity_bridge.parquet')
        else:
            path = release/'silver_reviews_current.parquet'
            table = pq.ParquetFile(path).read()
            table = table.cast(table.schema.set(0,pa.field(table.column_names[0],pa.large_string())))
            pq.write_table(table,path)
            manifest['artifacts'][path.name].update(sha256=s.sha(path),logical_sha256=_digest(table.to_pylist()))
        (release/'manifest.json').write_text(s.dump(manifest))
        (root/'current.json').write_text(s.dump(dict(release_id=release.name,manifest_sha256=s.sha(release/'manifest.json'))))
    with pytest.raises((ValueError,FileNotFoundError)):
        g.pin_reviews(root, places)


@pytest.mark.parametrize('damage', ['release_id','same_id_different_manifest','missing','ambiguous'])
def test_reviews_places_lineage_requires_authoritative_manifest(published_reviews, tmp_path, damage):
    root, release, places = published_reviews
    if damage in {'release_id','same_id_different_manifest'}:
        clone = tmp_path/'other-places'; (clone/'silver').mkdir(parents=True)
        source = places.parent.parent
        manifest = json.loads((source/'manifest.json').read_text())
        for name in manifest['artifacts']:
            path = clone/name; path.parent.mkdir(parents=True,exist_ok=True)
            path.write_bytes((source/name).read_bytes())
        if damage == 'release_id': manifest['release_id'] = 'b'*24
        else: manifest['fixture_annotation'] = 'same release ID is insufficient'
        (clone/'manifest.json').write_text(s.dump(manifest))
        places = clone/'silver/google_places.parquet'
    else:
        def change(signature):
            inputs = signature['inputs_sha256']
            lineage = next(p for p in inputs if Path(p).name=='manifest.json')
            if damage == 'missing': inputs.pop(lineage)
            else:
                duplicate = tmp_path/'extra-manifest'/'manifest.json'; duplicate.parent.mkdir()
                duplicate.write_bytes(Path(lineage).read_bytes()); inputs[str(duplicate)] = s.sha(duplicate)
        republish_review_signature(tmp_path/'revised-reviews',release,change)
        root = tmp_path/'revised-reviews'
    with pytest.raises(ValueError, match='lineage'):
        g.pin_reviews(root,places)


def test_review_release_requires_root(tmp_path):
    with pytest.raises(ValueError, match='requires --reviews-root'):
        g.stage_gold(tmp_path/'active',tmp_path/'category',tmp_path/'out',reviews_release='a'*24)


def review_bundle(root, scenario='standard'):
    """Real immutable test releases, including Gold's production lineage checks."""
    from test_google_reviews_silver import fixture, job, event
    from fnb_platform import google_reviews_silver as reviews
    import sqlite3
    root.mkdir(parents=True)
    def modify(raw):
        job(raw,4,'0x3:0x3',[event('r4','unassigned')])
        for path in raw.glob('worker-*/job-*/lake/**/*.parquet'):
            table=pq.ParquetFile(path).read(); rows=table.to_pylist()
            for row in rows:
                if row['source_review_id']=='r2': row['observed_at']=None
                if any(p.name=='job-02' for p in path.parents): row['change_type']='UPDATE'
            # The producer hash does not include event type or observation time.
            pq.write_table(pa.Table.from_pylist(rows,schema=table.schema),path)
        if scenario=='empty':
            for path in raw.glob('worker-*/job-*/lake/**/*.parquet'):
                table=pq.ParquetFile(path).read().slice(0,0).replace_schema_metadata(None)
                pq.write_table(table,path)
            for path in raw.glob('worker-*/job-*/reviews.sqlite3'):
                with sqlite3.connect(path) as db: db.execute('delete from review_state')
    places_root,_=fixture(root,modify=modify)
    reviews_root=root/'reviews'; release, review_summary=reviews.publish(places_root,reviews_root)
    gold_inputs=root/'gold-inputs';gold_inputs.mkdir()
    paths,_,effective,_=fixture_inputs(gold_inputs)
    paths['google_path']=s.read_active(places_root)/'silver/google_places.parquet'
    raw=gold_inputs/'raw'/'current_state.parquet';raw.parent.mkdir()
    raw.write_bytes(paths['overture_path'].read_bytes())
    effective.append(dict(effective[1],overture_id='ov3',effective_status='REJECTED'))
    if scenario=='zero':
        effective[0]['effective_status']='PENDING'
    if scenario=='conflict':
        effective[1].update(effective_status='AUTO_ACCEPTED',google_identity_value='0x1:0x1')
    category_root=root/'category'
    category_pins={str(p.resolve()):s.sha(p) for p in (raw,paths['google_path'])}
    csig=dict(rule_version='category-quality-v1',publication_kind='CATEGORY_FIXTURE',inputs_sha256=category_pins,code_sha256={})
    cat,_=g.publish_release(category_root,csig,lambda stage,rid:({
        'category_normalization.parquet':pq.read_table(paths['category_normalization_path']),
        'category_audit.parquet':pq.read_table(paths['category_audit_path'])},{},[]))
    effective=[dict(r,category_release_id=cat.name) for r in effective]
    pins={str(p.resolve()):s.sha(p) for p in (paths['google_path'],cat/'manifest.json',cat/'category_audit.parquet',cat/'category_normalization.parquet')}
    active_root=root/'active'
    asign=dict(rule_version=g.ACTIVE_VERSION,publication_kind='ACTIVE_EFFECTIVE_MAPPING_FOR_GOLD_STAGING',
               inputs_sha256=pins,code_sha256={},human_root=str(root/'human'),human_pointer_sha256=None,
               fixture_logical_hash=_digest(effective))
    g.publish_release(active_root,asign,lambda stage,rid:({'effective_identity_mapping.parquet':pa.Table.from_pylist(effective)},{},[]))
    return active_root,category_root,reviews_root,release,review_summary


@pytest.fixture(scope='module')
def attached_gold(tmp_path_factory):
    root=tmp_path_factory.mktemp('attached-gold')
    bundle=review_bundle(root/'bundle')
    dest,summary=g.stage_gold(bundle[0],bundle[1],root/'gold',reviews_root=bundle[2])
    return root,bundle,dest,summary


def test_review_attachment_real_dbt_and_reuse(attached_gold):
    root,bundle,dest,summary=attached_gold
    attached=pq.read_table(dest/'review_attachment.parquet').to_pylist()
    unmatched=pq.read_table(dest/'unmatched_review_accounting.parquet').to_pylist()
    assert summary['current_logical_reviews']==3 and summary['attached_reviews']==1 and summary['unmatched_reviews']==2
    assert summary['dbt_build']['models']==6 and summary['dbt_build']['failures']==0
    assert attached[0]['source_review_id']=='r1' and attached[0]['store_id']==g.store_id('ov1')
    assert attached[0]['observation_count']==2
    assert bundle[4]['quarantined_observations']==1
    by_id={r['source_review_id']:r for r in unmatched}
    assert by_id['r3']['mapping_statuses']==['PENDING','REJECTED']
    assert by_id['r4']['mapping_statuses']==[] and by_id['r4']['unmatched_reason']=='NO_MAPPING'
    assert all('store_id' not in r for r in unmatched)
    assert json.loads(attached[0]['gold_review_lineage_json'])['reviews_release_id']==bundle[3].name
    manifest=s.verify_release(dest)
    assert manifest['signature']['rule_version']==g.REVIEWS_STAGING_VERSION
    before=(root/'gold/current.json').read_bytes();checksums={p:s.sha(p) for p in dest.rglob('*') if p.is_file()}
    assert g.stage_gold(bundle[0],bundle[1],root/'gold',reviews_root=bundle[2])[0]==dest
    assert (root/'gold/current.json').read_bytes()==before and checksums=={p:s.sha(p) for p in checksums}
    with pytest.raises(ValueError,match='Different dataset'):
        g.stage_gold(bundle[0],bundle[1],root/'gold')


@pytest.mark.parametrize('scenario',['empty','zero'])
def test_empty_and_zero_accepted_reviews_real_dbt(tmp_path,scenario):
    bundle=review_bundle(tmp_path/'bundle',scenario)
    dest,summary=g.stage_gold(bundle[0],bundle[1],tmp_path/'gold',reviews_root=bundle[2])
    assert summary['attached_reviews']==0
    assert summary['unmatched_reviews']==(0 if scenario=='empty' else 3)
    assert summary['canonical_store_count']==3
    for name in ('review_attachment','unmatched_review_accounting'):
        table=pq.read_table(dest/(name+'.parquet'))
        assert table.schema.field('source_review_id').type==pa.string()


def test_conflicting_accepted_review_identity_blocks(tmp_path):
    bundle=review_bundle(tmp_path/'bundle','conflict')
    with pytest.raises(ValueError,match='Conflicting/shared accepted review mapping'):
        g.stage_gold(bundle[0],bundle[1],tmp_path/'gold',reviews_root=bundle[2])
    assert not (tmp_path/'gold/current.json').exists()


@pytest.mark.parametrize('damage',['store','duplicate','missing','different_identity','provenance'])
def test_review_attachment_python_dq_rejects_corruption(attached_gold,damage):
    _,bundle,dest,_=attached_gold
    paths,_,effective,_=g.pin_inputs(bundle[0],bundle[1])
    review_paths,_,_,_=g.pin_reviews(bundle[2],paths['google_path'])
    current=pq.read_table(review_paths['reviews_current_path']).to_pylist()
    tables={n:pq.read_table(dest/n) for n in ('review_attachment.parquet','unmatched_review_accounting.parquet')}
    attached=tables['review_attachment.parquet'].to_pylist();unmatched=tables['unmatched_review_accounting.parquet'].to_pylist()
    if damage=='store': attached[0]['store_id']=g.store_id('ov2')
    elif damage=='duplicate': attached.append(dict(attached[0]))
    elif damage=='missing': unmatched=unmatched[:1]
    elif damage=='different_identity': unmatched[0]['source_review_id']='different'
    else: attached[0]['provenance_json']='{}'
    tables={n:pa.Table.from_pylist(rows,schema=tables[n].schema) for n,rows in [('review_attachment.parquet',attached),('unmatched_review_accounting.parquet',unmatched)]}
    with pytest.raises(ValueError,match='Review'):
        g.validate_review_attachment(tables,current,effective,pq.read_table(dest/'store_identity_registry.parquet'))


@pytest.mark.parametrize('reuse',[False,True])
def test_review_inputs_change_blocks_fresh_and_reuse(attached_gold,reuse):
    root,bundle,dest,_=attached_gold
    out=root/'gold';pointer=(out/'current.json').read_bytes()
    path=bundle[3]/'silver_reviews_current.parquet';original=path.read_bytes()
    def change(phase):
        if phase=='before_pointer_replace': path.write_bytes(original+b' ')
    try:
        with pytest.raises(ValueError,match='Pinned input/code changed'):
            g.stage_gold(bundle[0],bundle[1],out,reviews_root=bundle[2],
                         reviews_release=None if reuse else bundle[3].name,fault=change)
        assert (out/'current.json').read_bytes()==pointer and s.read_active(out)==dest
    finally: path.write_bytes(original)


def test_review_conservation_and_mapping_dbt_reject_bad_outputs(attached_gold,tmp_path):
    import duckdb
    _,bundle,dest,_=attached_gold
    database=tmp_path/'bad-fixture.duckdb';database.write_bytes((dest/'staging.duckdb').read_bytes())
    with duckdb.connect(str(database)) as con:
        con.execute('update gold_staging.review_attachment set store_id=?',[g.store_id('ov2')])
        con.execute('insert into gold_staging.review_attachment select * from gold_staging.review_attachment')
    paths,_,_,_=g.pin_inputs(bundle[0],bundle[1])
    review_paths,_,context,_=g.pin_reviews(bundle[2],paths['google_path'])
    variables={k:p.as_posix() for k,p in {**paths,**review_paths}.items()}
    variables.update(store_registry_path=(dest/'store_identity_registry.parquet').as_posix(),
                     input_refs_json='{}',reviews_enabled=True,reviews_context_json=s.dump(context))
    target=tmp_path/'dbt-target'
    result=subprocess.run([sys.executable,'-B','-c','from dbt.cli.main import cli; cli()',
        'test','--project-dir',str(g.PROJECT),'--profiles-dir',str(g.PROJECT),
        '--target-path',str(target),'--log-path',str(tmp_path/'dbt-logs'),
        '--vars',json.dumps(variables),'--select','review_conservation','review_mapping','--no-use-colors'],
        env=dict(os.environ,FNB_DBT_DATABASE=str(database),DBT_SEND_ANONYMOUS_USAGE_STATS='false',DBT_PARTIAL_PARSE='false'),
        capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=120)
    results=json.loads((target/'run_results.json').read_text())['results']
    assert len(results)==2 and all(r['status']=='fail' for r in results),result.stdout+result.stderr
    assert result.returncode!=0


def test_reviews_quarantine_does_not_block_valid_current_admission(tmp_path):
    from test_google_reviews_silver import fixture
    from fnb_platform import google_reviews_silver as reviews
    def modify(root):
        path = next((root/'worker-01/job-01').glob('lake/**/*.parquet'))
        table = pq.ParquetFile(path).read(); rows = table.to_pylist()
        rows[1]['observed_at'] = None
        pq.write_table(pa.Table.from_pylist(rows,schema=table.schema),path)
    places, _ = fixture(tmp_path,modify=modify)
    root = tmp_path/'reviews'; release, summary = reviews.publish(places,root)
    assert summary['quarantined_observations'] == 1 and summary['silver_readiness'] == 'NOT_READY'
    paths, _, _, guard = g.pin_reviews(root,s.read_active(places)/'silver/google_places.parquet')
    guard()
    assert pq.read_table(paths['reviews_current_path']).num_rows == 2


@pytest.fixture(scope='module')
def anchor_dbt_fixture(tmp_path_factory):
    root = tmp_path_factory.mktemp('exact-anchor-dbt')
    paths, _, effective, _ = fixture_inputs(root)
    registry = pa.Table.from_pylist([dict(overture_id=f'ov{i}', store_id=g.store_id(f'ov{i}')) for i in range(1,4)])
    stage = root/'stage'; stage.mkdir()
    tables, metrics = g.dbt_build(stage, paths, registry)
    assert metrics['failures'] == 0
    return stage, paths, registry, effective, tables


@pytest.mark.parametrize('damage', ['correct','swapped','wrong_valid_fk','missing','duplicate','wrong_kind'])
def test_exact_overture_anchor_python_guard(anchor_dbt_fixture, damage):
    _, _, registry, effective, original = anchor_dbt_fixture
    tables = dict(original)
    mapping = tables['store_source_mapping.parquet'].to_pylist()
    overture = [r for r in mapping if r['source'] == 'overture']
    if damage == 'swapped':
        overture[0]['store_id'], overture[1]['store_id'] = overture[1]['store_id'], overture[0]['store_id']
    elif damage == 'wrong_valid_fk':
        overture[0]['store_id'] = overture[1]['store_id']
    elif damage == 'missing':
        mapping.remove(overture[0])
    elif damage == 'duplicate':
        mapping.append(dict(overture[0]))
    elif damage == 'wrong_kind':
        overture[0]['source_id_kind'] = 'wrong-kind'
    tables['store_source_mapping.parquet'] = pa.Table.from_pylist(mapping, schema=original['store_source_mapping.parquet'].schema)
    google_ids = {('data_id',f'0x{i}:0x{i}') for i in (1,2)}
    if damage == 'correct':
        assert g.validate_staging(tables, registry, effective, google_ids)['accepted_mapping_count'] == 1
    else:
        # Swapped/wrong targets still satisfy the old count and FK checks.
        if damage in {'swapped','wrong_valid_fk'}:
            assert len(mapping) == original['store_source_mapping.parquet'].num_rows
            assert all(r['store_id'] in registry['store_id'].to_pylist() for r in mapping)
        with pytest.raises(ValueError, match='anchor mismatch|coverage/referential integrity'):
            g.validate_staging(tables, registry, effective, google_ids)


@pytest.mark.parametrize('damage', ['correct','swapped','wrong_valid_fk'])
def test_exact_overture_anchor_dbt_data_test(anchor_dbt_fixture, damage):
    import duckdb
    stage, paths, _, _, tables = anchor_dbt_fixture
    database = stage/'staging.duckdb'
    # Only mutate the materialized pytest output table, never source mappings.
    with duckdb.connect(str(database)) as con:
        con.register('original_mapping', tables['store_source_mapping.parquet'])
        con.execute('create or replace table gold_staging.store_source_mapping as select * from original_mapping')
        if damage == 'swapped':
            con.execute("update gold_staging.store_source_mapping set store_id = case source_id when 'ov1' then ? when 'ov2' then ? else store_id end where source='overture'",
                        [g.store_id('ov2'),g.store_id('ov1')])
        elif damage == 'wrong_valid_fk':
            con.execute("update gold_staging.store_source_mapping set store_id=? where source='overture' and source_id='ov1'", [g.store_id('ov2')])
    variables = {k:p.resolve().as_posix() for k,p in paths.items()}
    variables.update(store_registry_path=(stage/'store_identity_registry.parquet').as_posix(), input_refs_json='{}')
    target = stage/('test-'+damage)
    result = subprocess.run([sys.executable,'-B','-c','from dbt.cli.main import cli; cli()',
        'test','--project-dir',str(g.PROJECT),'--profiles-dir',str(g.PROJECT),
        '--target-path',str(target),'--log-path',str(stage/('logs-'+damage)),
        '--vars',json.dumps(variables),'--select','no_unauthorized_merge','--no-use-colors'],
        env=dict(os.environ,FNB_DBT_DATABASE=str(database),DBT_SEND_ANONYMOUS_USAGE_STATS='false',DBT_PARTIAL_PARSE='false'),
        capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=120)
    report = json.loads((target/'run_results.json').read_text())['results']
    assert len(report) == 1 and 'no_unauthorized_merge' in report[0]['unique_id']
    assert report[0]['status'] == ('pass' if damage == 'correct' else 'fail'), result.stdout+result.stderr
    assert (result.returncode == 0) == (damage == 'correct')


def test_zero_accepted_exact_anchor_real_dbt(anchor_dbt_fixture):
    stage, paths, registry, effective, _ = anchor_dbt_fixture
    pending = [dict(r,effective_status='PENDING') for r in effective]
    pending_path = stage/'pending-fixture.parquet'
    pq.write_table(pa.Table.from_pylist(pending),pending_path)
    zero_paths = dict(paths,effective_mapping_path=pending_path)
    zero_stage = stage/'zero-accepted'; zero_stage.mkdir()
    tables, metrics = g.dbt_build(zero_stage,zero_paths,registry)
    summary = g.validate_staging(tables,registry,pending,{('data_id',f'0x{i}:0x{i}') for i in (1,2)})
    assert metrics['failures'] == 0 and summary['accepted_mapping_count'] == 0
    assert summary['source_only_store_count'] == summary['canonical_store_count'] == 3
    assert summary['unassigned_google_identities'] == 2


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
    assert summary['dbt_build']['models']==4
    manifest=s.verify_release(d)
    assert set(manifest['signature'])=={'rule_version','publication_kind','inputs_sha256','code_sha256','runtime_versions','registry_logical_sha256'}
    assert not {'review_attachment.parquet','unmatched_review_accounting.parquet'} & set(manifest['artifacts'])
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


@pytest.mark.parametrize('empty', [False, True])
def test_activation_recomputes_policy_and_keeps_editable_sample_separate(tmp_path,monkeypatch,empty):
    from test_er_auto_accept import fixture,evaluate
    data=fixture()
    if empty:
        data=([], [], [], [], data[4])
    table,policy_summary=evaluate(data)
    sources=tmp_path/'inputs';sources.mkdir()
    pins={}
    for name,rows in [('er_results.parquet',data[0]),('category_audit.parquet',data[1]),('google_places.parquet',data[3])]:
        context_field={'er_results.parquet':'er_release_id','category_audit.parquet':'category_release_id','google_places.parquet':'google_silver_release_id'}[name]
        directory=sources/data[4][context_field]
        path=directory/('silver/'+name if name=='google_places.parquet' else name)
        path.parent.mkdir(parents=True,exist_ok=True)
        from fnb_platform import entity_resolution as er
        source_schema={'google_places.parquet':s.PLACE_SCHEMA,'er_results.parquet':er.SCHEMA,'category_audit.parquet':category.AUDIT_SCHEMA}[name]
        source_table=pa.Table.from_pylist(rows,schema=source_schema)
        pq.write_table(source_table,path);pins[str(path.resolve())]=s.sha(path)
        artifact=path.relative_to(directory).as_posix()
        sm=directory/'manifest.json'
        sm.write_text(s.dump(dict(release_id=data[4][context_field],signature={},artifacts={artifact:dict(sha256=s.sha(path),rows=source_table.num_rows,logical_sha256=_digest(source_table.to_pylist()))})))
        pins[str(sm.resolve())]=s.sha(sm)
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
    assert summary['source_context']=={f:data[4][f] for f in ('er_release_id','category_release_id','google_silver_release_id')}
    assert pq.read_table(active/'effective_identity_mapping.parquet').schema.equals(g.ACTIVE_SCHEMA)
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


@pytest.mark.parametrize('empty_mapping', [False, True])
def test_zero_accepted_gold_keeps_source_anchors_and_unassigned_google(tmp_path, monkeypatch, empty_mapping):
    # Execute the actual model SQL offline; dbt adapter availability is covered
    # separately by the existing integration tests, not simulated here.
    import duckdb
    from jinja2 import Environment, StrictUndefined
    def sql_build(stage, paths, registry):
        registry_path = stage/'store_identity_registry.parquet'; pq.write_table(registry, registry_path)
        locations = {k.removesuffix('_path'):v for k,v in paths.items()}
        locations.update(store_registry=registry_path, effective_mapping=paths['effective_mapping_path'])
        env = Environment(undefined=StrictUndefined)
        with duckdb.connect(str(stage/'staging.duckdb')) as con:
            for name in ('stg_overture_candidates','stg_accepted_links','dim_store',
                         'store_source_mapping','source_accounting','link_accounting'):
                sql = env.from_string((g.PROJECT/'models'/f'{name}.sql').read_text()).render(
                    config=lambda **kw:'', ref=lambda n:n,
                    source=lambda _,n: "read_parquet('"+locations[n].as_posix()+"')",
                    var=lambda n:'{}')
                con.execute(f'create view {name} as {sql}')
            tables = {name+'.parquet':con.execute(f'select * from {name}').to_arrow_table()
                      for name in ('dim_store','store_source_mapping','source_accounting','link_accounting')}
        (stage/'dbt-target').mkdir()
        for name in ('run_results.json','manifest.json'):
            (stage/'dbt-target'/name).write_text(json.dumps({'execution':'direct-model-sql-test; dbt not run'}))
        (stage/'dbt_console.log').write_text('Direct model SQL test; dbt not run.')
        return tables, {'execution':'direct-model-sql', 'tests':0}
    monkeypatch.setattr(g, 'dbt_build', sql_build)
    original_version = g.version
    monkeypatch.setattr(g, 'version', lambda name:'direct-sql-test' if name in {'dbt-core','dbt-duckdb'} else original_version(name))
    paths, hashes, effective, guard = fixture_inputs(tmp_path)
    if empty_mapping:
        effective = []
    else:
        effective = [dict(row, effective_status='PENDING') for row in effective]
    pq.write_table(pa.Table.from_pylist(effective, schema=g.ACTIVE_SCHEMA), paths['effective_mapping_path'])
    hashes[str(paths['effective_mapping_path'])] = s.sha(paths['effective_mapping_path'])
    monkeypatch.setattr(g, 'pin_inputs', lambda *args: (paths, hashes, effective, guard))
    out = tmp_path/'staging'
    dest, summary = g.stage_gold(tmp_path/'active', tmp_path/'category', out)
    assert summary['accepted_mapping_count'] == 0
    assert summary['canonical_store_count'] == summary['source_only_store_count'] == 3
    assert summary['unassigned_google_identities'] == 2
    stores = pq.read_table(dest/'dim_store.parquet').to_pylist()
    assert {r['overture_id']:r['store_id'] for r in stores} == {f'ov{i}':g.store_id(f'ov{i}') for i in range(1,4)}
    assert all(r['google_identity_value'] is None and r['store_name'].startswith('Overture') for r in stores)
    accounts = pq.read_table(dest/'source_accounting.parquet').to_pylist()
    assert all(not r['in_catalog'] and r['store_id'] is None for r in accounts if r['source']=='google')
    links = pq.read_table(dest/'link_accounting.parquet')
    assert links.num_rows == len(effective) and 'google_identity_value' in links.schema.names
    pointer = (out/'current.json').read_bytes()
    assert g.stage_gold(tmp_path/'active', tmp_path/'category', out)[0] == dest
    assert (out/'current.json').read_bytes() == pointer
