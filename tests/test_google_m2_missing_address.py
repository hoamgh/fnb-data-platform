"""Missing-address search context must not become fabricated branch evidence."""
import json
import sqlite3
from types import SimpleNamespace

import pytest

from scripts import google_live_workers as workers
from scripts import google_m2_batch as batch
from crawl_experiment.sources.google_maps.navigator import GoogleMapsNavigator


def place():
    return dict(place_key='overture:023f3b54-3a1b-4d70-b4ad-66d51787154a',
                name='Phá Lấu Cô Mai', address=None, lat=10.75848792, lng=106.70333577,
                admin_area_name='Phường Khánh Hội, Thành phố Hồ Chí Minh')


def test_null_address_fallback_preserves_source_and_uses_coordinates_context():
    original=place(); before=dict(original)
    store=workers.store_for(original)
    assert original==before and original['address'] is None
    assert store.expected_address is None
    assert all(s in store.query for s in ['Phá Lấu Cô Mai','10.7584879,106.7033358','Khánh Hội','Hồ Chí Minh'])


@pytest.mark.parametrize('field,value',[('name',''),('lat',None),('lat',float('nan')),('lng',181)])
def test_missing_fields_are_planned_without_rejecting_record(field,value):
    original=place(); original[field]=value
    store=workers.store_for(original)
    assert store.search_eligible == (field!='name')
    assert store.expected_address is None


def test_missing_address_allows_collection_without_claiming_verified_identity(monkeypatch):
    store=workers.store_for(place())
    navigator=GoogleMapsNavigator(SimpleNamespace(find_elements=lambda *a:[object()]))
    monkeypatch.setattr(navigator,'_place_title',lambda:store.name)
    monkeypatch.setattr(navigator,'_place_address',lambda:'Google observed address')
    result=navigator._resolved_place(store,'https://www.google.com/maps/place/entity',
                                     SimpleNamespace(has_place_shell=True),[])
    assert result['crawl_eligible']
    assert result['identity_status']=='IDENTITY_UNVERIFIED'
    assert store.expected_address is None
    assert navigator._best_exact_candidate(store,[dict(text=store.name,address='Google observed address')]) is None
    assert navigator._inspection_candidate(store,[dict(text=store.name,address='Google observed address')]) is not None


def test_missing_address_selected_and_progress_covers_unresolved_failed_pending(tmp_path,monkeypatch):
    places=[place(),dict(place(),place_key='overture:second'),dict(place(),place_key='overture:third')]
    calls=[]
    def run(catalog,output,**kwargs):
        import pyarrow.parquet as pq
        selected=pq.read_table(catalog).to_pylist(); calls.extend(selected)
        output.mkdir(); queue=output/'dispatch.sqlite3'
        workers.create_queue(queue,selected)
        first=workers.claim_next(queue,'worker-01')
        workers.finish_dispatch(queue,first[0],'worker-01',dict(status='PLACE_RESOLUTION_FAILED',coverage='UNKNOWN'))
        second=workers.claim_next(queue,'worker-01')
        workers.finish_dispatch(queue,second[0],'worker-01',dict(status='ERROR',coverage='UNKNOWN'))
        return dict(completed=2)
    monkeypatch.setattr(batch.workers,'run',run)
    batch._execute_batch(places,tmp_path,25,None)
    assert len(calls)==3 and all(p['address'] is None for p in calls)
    progress=json.loads((tmp_path/'campaign_progress.json').read_text())
    assert len(progress['records'])==3
    assert progress['counts']==dict(PENDING=1,CRAWL_ELIGIBLE=0,VERIFIED=0,REVIEW_REQUIRED=0,UNRESOLVED=1,WRONG_BRANCH=0,FAILED=1,IN_PROGRESS_UNKNOWN=0)
    # Failed and unresolved IDs are recorded, not selected for automatic replay.
    calls.clear()
    monkeypatch.setattr(batch.workers,'run',lambda catalog,output,**kw:dict(completed=0))
    batch._execute_batch(places,tmp_path,25,None)
    selection=json.loads((tmp_path/'batch_selection.json').read_text())
    assert selection['selected']==['overture:third']
