"""Source-neutral optional-field search and conservative branch verification."""
from copy import deepcopy
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from crawl_experiment.sources.google_maps.poi_input import search_plan
from crawl_experiment.sources.google_maps.navigator import GoogleMapsNavigator
from scripts.google_live_workers import store_for
from scripts.google_overture_workers import load_places


def record():
    return dict(source='test-source',source_id='284',name='Quán Ốc 284',address='284 Tôn Đản',
                latitude=10.76,longitude=106.70,geographic_context='Khánh Hội, Hồ Chí Minh',phone=None)


@pytest.mark.parametrize('missing,mode',[
    ([], 'NAME_ADDRESS_COORDINATES'),(['address'],'NAME_COORDINATES_CONTEXT'),
    (['latitude','longitude'],'NAME_ADDRESS'),
    (['address','latitude','longitude'],'NAME_CONTEXT'),(['phone'],'NAME_ADDRESS_COORDINATES')])
def test_four_search_fallbacks_preserve_source(missing,mode):
    r=record()
    for key in missing:r[key]=None
    before=deepcopy(r); plan=search_plan(r); store=store_for(r)
    assert r==before and plan['eligible'] and store.search_eligible
    assert plan['mode']==mode
    assert store.expected_address==r['address']
    if r['address']: assert r['address'] in store.query
    else: assert '284 Tôn Đản' not in store.query
    if r['latitude'] is not None: assert '10.7600000,106.7000000' in store.query


def test_insufficient_information_is_explicit_unresolved_not_exception():
    r=record();r.update(address=None,latitude=None,longitude=None,geographic_context=None)
    plan=search_plan(r)
    assert not plan['eligible'] and plan['reason']=='INSUFFICIENT_SEARCH_INFORMATION'
    store=store_for(r)
    nav=GoogleMapsNavigator(object())
    from crawl_experiment.core.errors import PlaceResolutionError
    with pytest.raises(PlaceResolutionError): nav.open_store(store)
    assert nav.last_resolution_diagnostics['resolution_outcome']=='UNRESOLVED'


def test_loader_retains_all_rows_nulls_dq_and_stable_ids(tmp_path):
    rows=[record(),dict(record(),source_id='285',address=None,latitude=None,longitude=None),
          dict(record(),source_id='286',name=None,address=None)]
    path=tmp_path/'pois.parquet';pq.write_table(pa.Table.from_pylist(rows),path)
    first=load_places(path,3);second=load_places(path,3)
    assert first==second and len(first)==3
    for original,loaded in zip(rows,first):
        assert all(loaded[k]==v for k,v in original.items())
        assert store_for(loaded).id==store_for(original).id
    assert 'SOURCE_ADDRESS_MISSING' in search_plan(first[1])['dq_flags']
    assert not store_for(first[2]).search_eligible


def navigator(monkeypatch,title='Quán Ốc 284',address='284 Tôn Đản'):
    nav=GoogleMapsNavigator(SimpleNamespace(find_elements=lambda *a:[object()]))
    monkeypatch.setattr(nav,'_place_title',lambda:title)
    monkeypatch.setattr(nav,'_place_address',lambda:address)
    return nav


def test_complete_and_missing_coordinates_can_verify_with_name_address_entity(monkeypatch):
    nav=navigator(monkeypatch)
    for missing_coords in [False,True]:
        r=record()
        if missing_coords:r.update(latitude=None,longitude=None)
        outcome=nav._resolved_place(store_for(r),'https://google.com/maps/place/example',
                                    SimpleNamespace(has_place_shell=True),[])
        assert outcome['resolution_outcome']=='IDENTITY_UNVERIFIED'


def test_wrong_branch_address_conflict_is_explicit(monkeypatch):
    nav=navigator(monkeypatch,address='243 Tôn Đản')
    assert nav._resolved_place(store_for(record()),'https://google.com/maps/place/example',
                               SimpleNamespace(has_place_shell=True),[]) is None
    assert nav.last_resolution_diagnostics['resolution_outcome']=='WRONG_BRANCH'


def test_coordinate_contradiction_vetoes_matching_name_address(monkeypatch):
    nav=navigator(monkeypatch)
    assert nav._resolved_place(store_for(record()),
        'https://google.com/maps/place/example/data=!3d11.76!4d106.70',
        SimpleNamespace(has_place_shell=True),[]) is None
    assert nav.last_resolution_diagnostics['resolution_outcome']=='WRONG_BRANCH'


def test_missing_address_close_coordinates_allow_collection_not_er_match(monkeypatch):
    nav=navigator(monkeypatch);r=record();r['address']=None
    outcome=nav._resolved_place(store_for(r),
        'https://google.com/maps/place/example/data=!3d10.76!4d106.70',
        SimpleNamespace(has_place_shell=True),[])
    assert outcome['identity_status']=='IDENTITY_UNVERIFIED' and outcome['crawl_eligible']


def test_insufficient_worker_finishes_checkpoint_queue_without_browser(tmp_path):
    import asyncio
    import json
    import sqlite3
    from contextlib import ExitStack
    from scripts import google_live_workers as workers
    root=tmp_path/'run';directory=root/'worker-01';directory.mkdir(parents=True)
    r=dict(record(),place_key='test-source:284',address=None,latitude=None,longitude=None,geographic_context=None)
    workers.create_queue(root/'dispatch.sqlite3',[r])
    def no_browser(job):
        raise AssertionError('Insufficient record must not start a browser')
    with asyncio.Runner() as loop, ExitStack() as cleanup:
        report=workers._crawl_shard(dict(queue_path=str(root/'dispatch.sqlite3'),max_reviews=50,
            max_scrolls=5,per_store_seconds=120),directory,factory_builder=no_browser,
            async_runner=loop,session_cleanup=cleanup)
    assert report['results'][0]['verification_status']=='UNRESOLVED'
    assert report['results'][0]['verification_reason']=='INSUFFICIENT_SEARCH_INFORMATION'
    assert not report['results'][0]['search_attempted']
    assert json.loads((directory/'job-00'/'input.json').read_text())['source_record']==r
    with sqlite3.connect(root/'dispatch.sqlite3') as db:
        assert db.execute('SELECT state FROM dispatch').fetchone()[0]=='FINISHED'
        assert db.execute('SELECT COUNT(*) FROM dispatch').fetchone()[0]==1
