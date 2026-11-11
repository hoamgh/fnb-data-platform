from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from fnb_platform import google_reviews_silver as r, google_silver as s
from fnb_platform.overture_incremental import _digest
from crawl_experiment.core.models import Review
from crawl_experiment.storage.parquet_review_writer import ParquetReviewWriter
from crawl_experiment.storage.review_repository import review_content_hash


def write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(s.dump(obj), encoding='utf-8')


def event(review_id='r1', text='Fixture review', observed='2026-11-06T00:00:00+00:00', **changes):
    row=dict(source='google_maps', source_review_id=review_id, store_id='crawler-shared', run_id='same-second',
        reviewer_name='Fixture reviewer', rating=4.0, review_text=text, observed_at=observed,
        change_type='INSERT', timestamp_status='VERIFIED', timestamp_source='google_maps_network',
        published_at='2026-11-01T00:00:00+00:00', last_modified_at='2026-11-01T00:00:00+00:00',
        review_date_raw='5 days ago', owner_response=None, image_urls=[])
    row.update(changes)
    row['content_hash']=review_content_hash(Review(source=row['source'],source_review_id=row['source_review_id'],
        store_id=row['store_id'],author=row['reviewer_name'],rating=row['rating'],text=row['review_text'],
        owner_response=row['owner_response'],image_urls=row['image_urls']))
    return row


def job(root, number, google_id, rows):
    path=root/'worker-01'/f'job-{number:02d}';capture=path/'crawl'/'same-second'
    write_json(path/'input.json',dict(store={'id':'crawler-shared'},overture={'place_key':f'overture:fixture-{number}'}))
    write_json(capture/'metadata/crawler-shared.place_details.json',dict(store_id='crawler-shared',
        google_identifier=google_id, google_identifier_type='data_id', review_count=20, name='Same fixture name'))
    write_json(capture/'metadata/crawler-shared.json',dict(id='crawler-shared',place_entity_confirmed=True,
        resolved_url=f'https://www.google.com/maps/place/fixture/data=!1s{google_id}!3d0!4d0'))
    write_json(capture/'run_metadata.json',dict(sort='newest'))
    write_json(capture/'checkpoints/crawler-shared.json',dict(status='PARTIAL_LIMIT',scroll_count=5))
    result=dict(id='crawler-shared',status='PARTIAL_LIMIT',scroll_count=5,parse_errors=0,stop_reason='max_scrolls')
    write_json(capture/'summary.json',dict(stores=[result]))
    writer=ParquetReviewWriter(path/'lake','same-second','2026-11-06')
    writer.write_batch([(_digest([number,i,row]),row) for i,row in enumerate(rows)])
    with sqlite3.connect(path/'reviews.sqlite3') as db:
        db.executescript('create table pending_review_events(event_id text); create table review_state(id text);')
        db.executemany('insert into review_state values(?)',[(row['source_review_id'],) for row in rows])
    return path,result


def fixture(tmp_path, *, modify=None, legacy=False):
    base=tmp_path/'inputs';root=base/'raw/google-current'
    rows=[(1,'0x1:0x1',[event(),event('r2',None)]),
          (2,'0x1:0x1',[event(text=None,observed='2026-11-07T00:00:00+00:00')]),
          (3,'0x2:0x2',[event('r3','Other review')])]
    results=[]
    for num,key,events in rows:
        _,result=job(root,num,key,events);results.append(result)
    write_json(root/'plan.json',dict(max_scrolls=5,max_reviews=50,review_idle_limit=2))
    write_json(root/'summary.json',dict(status='FINISHED',results=results))
    with sqlite3.connect(root/'dispatch.sqlite3') as db:db.execute('create table dispatch(id integer)')
    if modify: modify(root)
    release=base/'places/releases'/('a'*24);release.mkdir(parents=True)
    places=pa.Table.from_pylist([dict(google_id_kind='data_id',google_identity_value=f'0x{i}:0x{i}',review_count=20) for i in (1,2,3)])
    pq.write_table(places,release/'places.parquet')
    write_json(release/'reports/google_silver_dq_summary.json',dict(input_runs=[{'root':str(root.resolve())}]))
    (release/'silver').mkdir();(release/'places.parquet').rename(release/'silver/google_places.parquet')
    artifacts={}
    for path in [release/'silver/google_places.parquet',release/'reports/google_silver_dq_summary.json']:
        spec={'sha256':s.sha(path)}
        if path.suffix=='.parquet':spec.update(rows=places.num_rows,logical_sha256=_digest(places.to_pylist()))
        artifacts[path.relative_to(release).as_posix()]=spec
    pins={str((root/name).resolve()):s.sha(root/name) for name in ['plan.json','summary.json','dispatch.sqlite3']}
    write_json(release/'manifest.json',dict(release_id=release.name,signature=dict(policy=s.POLICY,inputs_sha256=pins),artifacts=artifacts))
    write_json(release.parents[1]/'current.json',dict(release_id=release.name,manifest_sha256=s.sha(release/'manifest.json')))
    if legacy:
        old=base/'raw/google-old';job(old,1,'0x1:0x1',[event('old-only')]);(old/'dispatch.sqlite3').touch()
    return release.parents[1],root


def run_transform(google):
    tracked,roots,places,release,files=r.load_inputs(google)
    return r.transform(roots,places,tracked)


def test_real_file_fixture_dedup_current_no_null_text_backfill_and_coverage(tmp_path):
    google,root=fixture(tmp_path)
    before={p:s.sha(p) for p in r.capture_files([root.resolve()])}
    tables,summary=run_transform(google)
    assert summary['persisted_event_count']==4 and summary['unique_logical_review_count']==3
    assert summary['google_identities_with_reviews']==2 and summary['ids_with_multiple_content_hashes']==1
    assert summary['rating_only_observations']==2 and summary['missing_or_empty_text_current']==2
    assert summary['unresolved_review_identity_bindings']==0 and summary['silver_readiness']=='READY'
    current={row['source_review_id']:row for row in tables['silver_reviews_current.parquet'].to_pylist()}
    assert current['r1']['review_text'] is None and current['r1']['observation_count']==2
    assert current['r1']['first_seen_at']<current['r1']['last_observed_at']
    assert 'CONTENT_VARIATION_NOT_VERIFIED_EDIT' in current['r1']['dq_flags']
    inventory=tables['review_collection_inventory.parquet'].to_pylist()
    assert len(inventory)==4 and {row['google_identity_value'] for row in inventory}=={'0x1:0x1','0x2:0x2','0x3:0x3'}
    assert all(not row['is_complete_google_review_coverage'] for row in inventory)
    unattempted=next(row for row in inventory if row['google_identity_value']=='0x3:0x3')
    assert unattempted['collection_attempt_id'] is None and unattempted['collection_status']=='NO_RECORDED_ATTEMPT'
    # Identical crawler store_id + run_id in different jobs must not cross-bind.
    observations=tables['silver_review_observations.parquet'].to_pylist()
    assert next(row for row in observations if row['source_review_id']=='r3')['google_identity_value']=='0x2:0x2'
    assert len({row['bridge_id'] for row in observations})==3
    assert before=={p:s.sha(p) for p in before}


def test_cross_google_review_conflict_quarantined_without_reassignment(tmp_path):
    def modify(root):
        path=next((root/'worker-01/job-03').glob('lake/**/*.parquet'))
        table=pq.ParquetFile(path).read();data=table.to_pylist();data[0]['source_review_id']='r1'
        pq.write_table(pa.Table.from_pylist(data,schema=table.schema),path)
    google,_=fixture(tmp_path,modify=modify)
    tables,summary=run_transform(google)
    assert summary['cross_identity_review_conflicts']==1 and summary['quarantined_observations']==3
    assert summary['unique_logical_review_count']==1 and summary['silver_readiness']=='NOT_READY'
    assert tables['silver_reviews_current.parquet'].to_pylist()[0]['source_review_id']=='r2'
    affected=[x for x in tables['silver_review_observations.parquet'].to_pylist() if x['source_review_id']=='r1']
    assert all(not x['current_eligible'] and 'CROSS_IDENTITY_REVIEW_CONFLICT' in x['dq_flags'] for x in affected)
    assert {x['google_identity_value'] for x in affected}=={'0x1:0x1','0x2:0x2'}


def test_ambiguous_capture_metadata_does_not_use_names_or_selected_candidates(tmp_path):
    def modify(root):
        path=root/'worker-01/job-01/crawl/same-second/metadata/crawler-shared.json'
        write_json(path,dict(id='crawler-shared',place_entity_confirmed=True,
            resolved_url='https://www.google.com/maps/place/fixture/data=!1s0x2:0x2!3d0!4d0',
            selected_candidate={'candidate_id':'0x1:0x1'},name='Same fixture name'))
    google,_=fixture(tmp_path,modify=modify);tables,summary=run_transform(google)
    assert summary['unresolved_review_identity_bindings']==2
    assert any(b['binding_status']=='AMBIGUOUS' for b in tables['review_identity_bridge.parquet'].to_pylist())


@pytest.mark.parametrize('mode',['invalid_kind','unknown_google_id'])
def test_typed_google_binding_must_match_metadata_and_pinned_places(tmp_path,mode):
    def modify(root):
        capture=root/'worker-01/job-01/crawl/same-second/metadata'
        if mode=='invalid_kind':
            path=capture/'crawler-shared.place_details.json';obj=json.loads(path.read_text());obj['google_identifier_type']='place_id';write_json(path,obj)
        else:
            path=capture/'crawler-shared.place_details.json';obj=json.loads(path.read_text());obj['google_identifier']='0x9:0x9';write_json(path,obj)
            path=capture/'crawler-shared.json';obj=json.loads(path.read_text());obj['resolved_url']='https://www.google.com/maps/place/fixture/data=!1s0x9:0x9!3d0!4d0';write_json(path,obj)
    google,_=fixture(tmp_path,modify=modify);tables,summary=run_transform(google)
    assert summary['unresolved_review_identity_bindings']==2 and summary['silver_readiness']=='NOT_READY'
    assert not any(row['google_identity_value']=='0x9:0x9' for row in tables['silver_reviews_current.parquet'].to_pylist())
    assert summary['inventory_google_identity_count']==3


def test_equal_observation_time_has_deterministic_tie_breaker_and_warning(tmp_path):
    def modify(root):
        path=next((root/'worker-01/job-02').glob('lake/**/*.parquet'));table=pq.ParquetFile(path).read();rows=table.to_pylist()
        rows[0]['observed_at']='2026-11-06T00:00:00+00:00';pq.write_table(pa.Table.from_pylist(rows,schema=table.schema),path)
    google,_=fixture(tmp_path,modify=modify);tables,_=run_transform(google);again,_=run_transform(google)
    assert tables['silver_reviews_current.parquet'].equals(again['silver_reviews_current.parquet'])
    row=next(x for x in tables['silver_reviews_current.parquet'].to_pylist() if x['source_review_id']=='r1')
    assert 'SAME_OBSERVED_AT_CONTENT_CONFLICT' in row['dq_flags']


@pytest.mark.parametrize('change,flag', [('store','EVENT_STORE_ID_MISMATCH'),('id','STABLE_REVIEW_ID_MISSING_OR_INVALID'),('hash','CONTENT_HASH_MISMATCH')])
def test_invalid_review_bindings_and_content_are_kept_but_quarantined(tmp_path,change,flag):
    def modify(root):
        path=next((root/'worker-01/job-01').glob('lake/**/*.parquet'));table=pq.ParquetFile(path).read();rows=table.to_pylist()
        rows[0][{'store':'store_id','id':'source_review_id','hash':'review_text'}[change]]=None if change=='id' else 'changed'
        pq.write_table(pa.Table.from_pylist(rows,schema=table.schema),path)
    google,_=fixture(tmp_path,modify=modify);tables,summary=run_transform(google)
    assert summary['observation_count']==4 and summary['quarantined_observations']>=1
    assert any(flag in row['dq_flags'] and not row['current_eligible'] for row in tables['silver_review_observations.parquet'].to_pylist())


def test_event_id_fallback_and_duplicate_file_replay(tmp_path):
    def modify(root):
        path=next((root/'worker-01/job-01').glob('lake/**/*.parquet'))
        table=pq.ParquetFile(path).read();pq.write_table(table.replace_schema_metadata(None),path)
        other=next((root/'worker-01/job-02').glob('lake/**/*.parquet'));other.with_name('copy.parquet').write_bytes(other.read_bytes())
    google,_=fixture(tmp_path,modify=modify);tables,summary=run_transform(google)
    assert summary['persisted_event_count']==5 and summary['observation_count']==4 and summary['duplicate_persisted_events']==1
    observations=tables['silver_review_observations.parquet'].to_pylist()
    assert sum(x['observation_id_basis']=='FILE_SHA256_ROW_INDEX' for x in observations)==2
    replay,_=run_transform(google)
    assert all(tables[name].equals(replay[name]) for name in tables)


@pytest.mark.parametrize('mode',['payload','footer'])
def test_corrupt_event_membership_hard_fails(tmp_path,mode):
    def modify(root):
        path=next((root/'worker-01/job-01').glob('lake/**/*.parquet'));table=pq.ParquetFile(path).read()
        if mode=='payload':
            rows=table.to_pylist();rows[0]['review_text']='Different payload, same source event ID'
            pq.write_table(pa.Table.from_pylist(rows,schema=table.schema),path.with_name('copy.parquet'))
        else:pq.write_table(table.slice(0,1),path)
    google,_=fixture(tmp_path,modify=modify)
    with pytest.raises(ValueError,match='Conflicting payloads|Invalid review event footer'):run_transform(google)


def test_invalid_rating_timestamps_preserve_raw_without_relative_date_guessing(tmp_path):
    def modify(root):
        path=next((root/'worker-01/job-01').glob('lake/**/*.parquet'));table=pq.ParquetFile(path).read();rows=table.to_pylist()
        rows[0]=event(rating=6,observed='not-a-date',published_at=None,last_modified_at=None)
        pq.write_table(pa.Table.from_pylist(rows,schema=table.schema),path)
    google,_=fixture(tmp_path,modify=modify);tables,summary=run_transform(google)
    row=next(x for x in tables['silver_review_observations.parquet'].to_pylist() if x['observed_at_raw']=='not-a-date')
    assert row['rating'] is None and row['published_at'] is None and row['last_modified_at'] is None and row['observed_at'] is None
    assert row['review_date_raw']=='5 days ago' and row['timestamp_status']=='PARSE_FAILED'
    assert {'RATING_INVALID','VERIFIED_TIMESTAMP_PAIR_INVALID','OBSERVED_AT_MISSING_OR_INVALID'}<=set(row['dq_flags'])


def test_legacy_files_inventory_only_and_publication_idempotent(tmp_path):
    google,_=fixture(tmp_path,legacy=True);out=tmp_path/'derived'
    dest,summary=r.publish(google,out);before=(out/'current.json').read_bytes()
    assert summary['persisted_event_count']==4 and summary['excluded_legacy_file_count']==1 and summary['excluded_legacy_persisted_rows']==1
    assert 'old-only' not in pq.read_table(dest/'silver_reviews_current.parquet')['source_review_id'].to_pylist()
    assert s.verify_release(dest)['publication_kind']=='GOOGLE_REVIEWS_SILVER_OFFLINE'
    assert r.publish(google,out)[0]==dest and (out/'current.json').read_bytes()==before
    other,_=r.publish(google,tmp_path/'independent')
    for filename in s.verify_release(dest)['artifacts']:
        if filename.endswith('.parquet'):assert pq.read_table(dest/filename).equals(pq.read_table(other/filename))


def test_unflushed_outbox_and_changed_pinned_inputs_rejected(tmp_path):
    google,root=fixture(tmp_path)
    with sqlite3.connect(root/'worker-01/job-01/reviews.sqlite3') as db:db.execute("insert into pending_review_events values('pending')")
    with pytest.raises(ValueError,match='Unflushed review outbox'):r.publish(google,tmp_path/'derived')
    write_json(root/'plan.json',dict(max_scrolls=9))
    with pytest.raises(ValueError,match='Pinned input/code changed'):r.publish(google,tmp_path/'derived')


def test_crash_recovery_preserves_previous_release_and_source_files(tmp_path):
    google,root=fixture(tmp_path);out=tmp_path/'derived'
    dest,_=r.publish(google,out);before=(out/'current.json').read_bytes()
    path=next((root/'worker-01/job-02').glob('lake/**/*.parquet'));path.with_name('copy.parquet').write_bytes(path.read_bytes())
    snapshots={p:s.sha(p) for p in r.capture_files([root.resolve()])}
    for phase in ['after_stage_validation','after_release_rename','before_pointer_replace']:
        def fault(actual):
            if actual==phase:raise RuntimeError('simulated crash')
        with pytest.raises(RuntimeError,match='simulated crash'):r.publish(google,out,fault=fault)
        assert (out/'current.json').read_bytes()==before and s.read_active(out)==dest
    final,summary=r.publish(google,out)
    assert final!=dest and summary['duplicate_persisted_events']==1
    assert snapshots=={p:s.sha(p) for p in snapshots}


def test_source_membership_change_during_publication_does_not_activate(tmp_path):
    google,root=fixture(tmp_path);out=tmp_path/'derived'
    def fault(phase):
        if phase=='after_stage_validation':
            path=next((root/'worker-01/job-02').glob('lake/**/*.parquet'));path.with_name('late.parquet').write_bytes(path.read_bytes())
    with pytest.raises(ValueError,match='Source file inventory changed'):r.publish(google,out,fault=fault)
    assert not (out/'current.json').exists()
