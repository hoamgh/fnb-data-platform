from copy import deepcopy
import hashlib
import json
from pathlib import Path
from urllib.error import URLError

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from shapely.geometry import box

from fnb_platform import area_demographics as d, area_enrichment as a, google_silver as s


def reference_fixture():
    codes = dict(version='fixture-v1', city_code='79', city_name='Thành phố Hồ Chí Minh',
        source_url='https://fixture.invalid/official', source_pdf_sha256='fixture-sha', effective_from='2025-07-01',
        rows=[dict(ward_code='27265', ward_name='Phường Khánh Hội'),
              dict(ward_code='27286', ward_name='Phường Vĩnh Hội'),
              dict(ward_code='00001', ward_name='Phường Fixture')])
    obj = dict(cot=sorted(d.REQUIRED), dong=[dict(ma_xa=r['ward_code'], ten=r['ward_name'], ma_tinh='79',
        ten_tinh='Thành phố Hồ Chí Minh', dien_tich_km2=1.0, quy_mo_dan_so=100,
        nghi_quyet='Fixture resolution', url='https://fixture.invalid/' + r['ward_code']) for r in codes['rows']],
        giay_phep=d.LICENSE, ngay='2026-11-05', du_lieu_den='20/09/2026')
    raw = s.dump(obj).encode('utf-8')
    meta = dict(response_sha256=hashlib.sha256(raw).hexdigest(), retrieved_at_utc='2026-11-09T00:00:00+00:00',
                source_url='https://fixture.invalid/api', http_status=200)
    return obj, meta, codes


def normalized_fixture():
    obj, meta, codes = reference_fixture()
    return d.normalize(obj, meta, codes, reference_year=2025,
        population_basis='2025_REORGANIZATION_PROPOSAL_NOT_CENSUS').to_pylist()


def anchor(**changes):
    base = dict(store_id='fixture-store-1', overture_id='fixture-ov-1', store_name='Fixture POI',
        address=None, latitude=0.5, longitude=0.5, address_source='google', coordinate_source='google',
        identity_status='AUTO_ACCEPTED', is_identity_human_verified=False, integration_status='GOOGLE_LINK_ACCEPTED',
        category_validation_status='PENDING')
    return dict(base, **changes)


REFS = dict(boundary_version='fixture-boundary-v1', reference_version='fixture-reference-v1')
BOUNDARIES = [dict(ward_code='27265', polygon=box(0, 0, 1, 1))]


def assignment(**changes):
    return a.assign_one(anchor(**changes), a.reference_index(normalized_fixture()), BOUNDARIES, REFS)


def test_reference_schema_and_leading_zero_and_density():
    obj, meta, codes = reference_fixture()
    assert d.document(s.dump(obj)) == obj
    rows = normalized_fixture()
    assert rows[0]['ward_code'] == '00001' and rows[0]['population_density'] == 100
    assert all(r['legal_code_status'] == 'CODE_AND_NAME_AGREE' for r in rows)
    assert all('NOT_CENSUS' in r['population_basis'] and r['data_quality_status'] == 'WARNING' for r in rows)
    row = rows[-1]
    assert json.loads(row['raw_row_json'])['ten'] == row['ward_name']
    assert json.loads(row['provenance_json'])['snapshot_sha256'] == meta['response_sha256']
    obj['cot'].remove('ma_xa')
    with pytest.raises(ValueError, match='columns'):d.document(s.dump(obj))


@pytest.mark.parametrize('value', [None, ''])
def test_missing_values_not_zero(value):
    obj, meta, codes = reference_fixture()
    obj['dong'][0].update(dien_tich_km2=value, quy_mo_dan_so=value)
    row = next(r for r in d.normalize(obj, meta, codes, reference_year=2025, population_basis='UNKNOWN').to_pylist() if r['ward_code'] == '27265')
    assert row['area_km2'] is None and row['population'] is None and row['population_density'] is None
    assert json.loads(row['raw_row_json'])['quy_mo_dan_so'] == value


@pytest.mark.parametrize('area,pop', [(0, -1), (-1, 'bad'), ('invalid', 1.5), (True, False), (float('inf'), float('nan'))])
def test_invalid_numbers_preserved_as_raw_flagged(area, pop):
    obj, meta, codes = reference_fixture()
    obj['dong'][0].update(dien_tich_km2=area, quy_mo_dan_so=pop)
    row = next(r for r in d.normalize(obj, meta, codes, reference_year=2025, population_basis='UNKNOWN').to_pylist() if r['ward_code'] == '27265')
    assert row['area_km2'] is None and row['population'] is None
    assert {'AREA_KM2_INVALID', 'POPULATION_INVALID'} <= set(row['dq_flags'])


def test_duplicate_and_invalid_codes_hard_fail_administrative_conflicts_flagged():
    obj, meta, codes = reference_fixture()
    obj['dong'].append(deepcopy(obj['dong'][0]))
    with pytest.raises(ValueError, match='Duplicate ward_code'):d.normalize(obj, meta, codes, reference_year=2025, population_basis='UNKNOWN')
    obj['dong'].pop();obj['dong'][0]['ma_xa']=27265
    with pytest.raises(ValueError, match='ward code'):d.normalize(obj, meta, codes, reference_year=2025, population_basis='UNKNOWN')
    obj['dong'][0].update(ma_xa='27265', ten='Phường Vĩnh Hội')
    rows=d.normalize(obj, meta, codes, reference_year=2025, population_basis='UNKNOWN').to_pylist()
    row=next(r for r in rows if r['ward_code']=='27265')
    assert row['legal_code_status']=='CONFLICT' and 'ADMIN_CODE_CONFLICT' in row['dq_flags']
    assert a.address_lookup('1 Street, Khánh Hội, Hồ Chí Minh',a.reference_index(rows))['code'] is None


def test_bronze_offline_idempotency_raw_bytes_and_tamper_rejection(tmp_path, monkeypatch):
    obj, meta, _=reference_fixture();obj['dong'][0]['quy_mo_dan_so']=None
    raw=s.dump(obj).encode('utf-8');meta['response_sha256']=hashlib.sha256(raw).hexdigest()
    source=tmp_path/'inputs';source.mkdir();(source/'response.json').write_bytes(raw)
    (source/'metadata.json').write_text(s.dump(meta),encoding='utf-8')
    monkeypatch.setattr(d,'urlopen',lambda *args,**kwargs:pytest.fail('Offline replay must not use network'))
    dest=d.ingest(tmp_path/'bronze',snapshot=source/'response.json')
    assert (dest/'response.json').read_bytes()==raw and d.verify_snapshot(dest)[0]['dong'][0]['quy_mo_dan_so'] is None
    assert dest==d.ingest(tmp_path/'bronze',snapshot=source/'response.json')
    (dest/'response.json').write_bytes(b'{}')
    with pytest.raises(ValueError,match='differs'):d.ingest(tmp_path/'bronze',snapshot=source/'response.json')


def test_failed_http_ingestion_has_no_snapshot(tmp_path,monkeypatch):
    def fail(*args,**kwargs):raise URLError('fixture endpoint unavailable')
    monkeypatch.setattr(d,'urlopen',fail)
    with pytest.raises(URLError):d.ingest(tmp_path/'bronze',url='https://fixture.invalid/api')
    assert not (tmp_path/'bronze').exists()


def test_address_primary_exact_city_ward_and_spatial_fallback():
    row=assignment(address='1 Fixture Street, Phường Khánh Hội, Hồ Chí Minh, Việt Nam')
    assert row['ward_code']=='27265' and row['assignment_method']=='ADDRESS'
    assert row['address_ward_code']==row['spatial_ward_code']=='27265'
    missing=assignment()
    assert missing['ward_code']=='27265' and missing['assignment_method']=='SPATIAL'
    assert missing['address_raw'] is None
    # A ward is not looked up across all cities when no source city was observed.
    no_city=assignment(address='1 Fixture Street, Phường Khánh Hội')
    assert no_city['address_lookup_status']=='CITY_UNKNOWN' and no_city['assignment_method']=='SPATIAL'


def test_old_ward_not_guessed_and_ambiguous_address_falls_back():
    old=assignment(address='1 Fixture Street, Phường 9, Hồ Chí Minh')
    assert old['address_lookup_status']=='OLD_WARD_UNRESOLVED' and old['assignment_method']=='SPATIAL'
    ambiguous=assignment(address='1 Fixture Street, Phường Khánh Hội, Phường Vĩnh Hội, Hồ Chí Minh')
    assert ambiguous['address_lookup_status']=='AMBIGUOUS' and ambiguous['assignment_method']=='SPATIAL'
    assert ambiguous['ward_code']=='27265'


def test_address_spatial_conflict_has_no_silent_winner():
    row=assignment(address='1 Fixture Street, Phường Vĩnh Hội, Hồ Chí Minh')
    assert row['address_ward_code']=='27286' and row['spatial_ward_code']=='27265'
    assert row['assignment_status']=='AREA_CONFLICT' and row['ward_code'] is None and row['area_id'] is None
    assert row['assignment_method']=='UNRESOLVED' and 'AREA_CONFLICT' in row['flags']
    evidence=json.loads(row['provenance_json'])
    assert evidence['address_lookup']['candidate_codes']==['27286'] and evidence['spatial_lookup']['matches']==['27265']


@pytest.mark.parametrize('lat,lon', [(None,None), (91,0), (0,181), (float('nan'),0), (float('inf'),1), (True,0)])
def test_invalid_coordinates_do_not_prevent_valid_address_lookup(lat,lon):
    row=assignment(address='1 Fixture Street, Vĩnh Hội, Hồ Chí Minh',latitude=lat,longitude=lon)
    assert row['spatial_ward_code'] is None and row['ward_code']=='27286' and row['assignment_method']=='ADDRESS'
    no_address=assignment(latitude=lat,longitude=lon)
    assert no_address['ward_code'] is None and no_address['assignment_status']=='UNRESOLVED'


def test_boundary_cases_outside_coverage_and_no_nearest_ward_inference():
    boundary=assignment(latitude=0,longitude=0)
    assert boundary['spatial_ward_code']=='27265' and boundary['near_boundary']
    outside=assignment(latitude=1.000001,longitude=.5)
    assert outside['ward_code'] is None and outside['near_boundary']
    assert outside['spatial_status']=='OUTSIDE_SUPPORTED_BOUNDARIES'
    claim=assignment(address='1 Fixture Street, Khánh Hội, Hồ Chí Minh',latitude=2,longitude=2)
    assert claim['assignment_method']=='ADDRESS' and 'ADDRESS_WARD_OUTSIDE_ITS_BOUNDARY' in claim['flags']
    no_boundary=a.assign_one(anchor(),a.reference_index(normalized_fixture()),[],REFS)
    assert no_boundary['ward_code'] is None and no_boundary['spatial_status']=='NO_BOUNDARY_AVAILABLE'
    overlaps=BOUNDARIES+[dict(ward_code='27286',polygon=box(0,0,1,1))]
    ambiguous=a.assign_one(anchor(address='1 Street, Khánh Hội, Hồ Chí Minh'),a.reference_index(normalized_fixture()),overlaps,REFS)
    assert ambiguous['assignment_status']=='AMBIGUOUS' and ambiguous['ward_code'] is None


def test_ambiguous_normalized_ward_and_no_substring_matching():
    refs=normalized_fixture();refs.append(dict(refs[0],ward_code='00002',ward_name='Phường Khánh Hội'))
    index=a.reference_index(refs)
    result=a.address_lookup('1 Street, Khánh Hội, Hồ Chí Minh',index)
    assert result['code'] is None and result['status']=='AMBIGUOUS'
    assert a.address_lookup('1 Street, Phường Khánh Hội Mới, Hồ Chí Minh',index)['code'] is None


def test_lookup_requires_matching_city_no_pilot_default_and_exact_named_old_ward():
    refs=normalized_fixture()
    other=dict(refs[1],ward_code='99999',city_code='01',city_name='Thành phố Fixture City',ward_name='Phường Vĩnh Hội')
    index=a.reference_index(refs+[other])
    assert a.address_lookup(None,index,ward_name='Vĩnh Hội',city_code='01')['code']=='99999'
    assert a.address_lookup(None,index,ward_name='Vĩnh Hội')['code'] is None
    conflict=a.address_lookup('1 Street, Vĩnh Hội, Hồ Chí Minh',index,city_code='01')
    assert conflict['status']=='AMBIGUOUS' and conflict['code'] is None
    old=a.address_lookup('91A Đường số 85, Phường Tân Quy, Quận 7, Thành phố Hồ Chí Minh',index)
    assert old['code'] is None and old['status']=='NOT_FOUND'
    assert a.address_lookup('1 Street, Vĩnh Hội, Hồ Chí Minh',index)==a.address_lookup(
        '1 Street, Vĩnh Hội, Hồ Chí Minh',a.reference_index(list(reversed(refs+[other]))))


def test_official_code_extract_complete_without_pdf_headers():
    codes=d.read_json(d.DEFAULT_CODES)
    assert len(codes['rows'])==len({r['ward_code'] for r in codes['rows']})==168
    assert not any('CÔNG BÁO' in r['ward_name'] for r in codes['rows'])
    kh=next(r for r in codes['rows'] if r['ward_code']=='27265')
    assert kh['ward_name']=='Phường Khánh Hội' and kh['pdf_page_index']==46
    assert len(codes['source_pdf_sha256'])==64 and codes['city_code']=='79'


def test_assignment_unique_coverage_provenance_determinism():
    stores=[anchor(),dict(anchor(),store_id='fixture-store-2',overture_id='fixture-ov-2',latitude=2)]
    table=a.assign_all(stores,normalized_fixture(),BOUNDARIES,REFS)
    assert table.num_rows==2 and table.equals(a.assign_all(list(reversed(stores)),normalized_fixture(),BOUNDARIES,REFS))
    assert table.to_pylist()[1]['ward_code'] is None
    assert all(r['boundary_version'] and r['reference_version'] and r['provenance_json'] for r in table.to_pylist())
    with pytest.raises(ValueError,match='Duplicate POI'):a.assign_all(stores+[stores[0]],normalized_fixture(),BOUNDARIES,REFS)


def write_snapshot(tmp_path):
    obj,meta,codes=reference_fixture();root=tmp_path/'raw';root.mkdir()
    (root/'response.json').write_text(s.dump(obj),encoding='utf-8');(root/'metadata.json').write_text(s.dump(meta),encoding='utf-8')
    codes_path=root/'codes.json';codes_path.write_text(s.dump(codes),encoding='utf-8')
    return root,codes_path


def test_silver_publication_idempotent_failure_keeps_active_and_recovery(tmp_path):
    snapshot,codes=write_snapshot(tmp_path);out=tmp_path/'silver'
    dest,summary=d.publish_silver(snapshot,out,codes_path=codes)
    before=(out/'current.json').read_bytes()
    assert dest==d.publish_silver(snapshot,out,codes_path=codes)[0] and summary['rows']==3
    def crash(phase):
        if phase=='before_pointer_replace':raise RuntimeError('simulated crash')
    with pytest.raises(RuntimeError):d.publish_silver(snapshot,out,codes_path=codes,population_basis='UNKNOWN',fault=crash)
    assert (out/'current.json').read_bytes()==before and s.read_active(out)==dest
    recovered,_=d.publish_silver(snapshot,out,codes_path=codes,population_basis='UNKNOWN')
    assert recovered!=dest and s.verify_release(recovered) and pq.read_table(dest/'area_demographics.parquet').num_rows==3


def test_real_dbt_fixtures_nulls_no_fanout_and_deterministic_rebuild(tmp_path):
    inputs=tmp_path/'inputs';inputs.mkdir()
    refs=normalized_fixture();refs[0].update(area_km2=None,population=None,population_density=None)
    stores=[anchor(),dict(anchor(),store_id='fixture-store-2',overture_id='fixture-ov-2',address='1 Street, Vĩnh Hội, Hồ Chí Minh')]
    paths=dict(reference_path=inputs/'reference.parquet',catalog_path=inputs/'catalog.parquet',assignment_path=inputs/'assignment.parquet')
    pq.write_table(pa.Table.from_pylist(refs,schema=d.SCHEMA),paths['reference_path'])
    pq.write_table(pa.Table.from_pylist(stores),paths['catalog_path'])
    pq.write_table(a.assign_all(stores,refs,BOUNDARIES,REFS),paths['assignment_path'])
    stage=tmp_path/'stage';stage.mkdir()
    tables,summary=a.dbt_build(stage,paths)
    assert summary['models']==4 and summary['tests']>=20 and summary['failures']==0
    assert tables['store_area_mapping.parquet'].num_rows==2
    mart={r['ward_code']:r for r in tables['mart_fnb_by_area.parquet'].to_pylist()}
    assert mart['27265']['poi_anchor_count']==1 and mart['27286']['poi_anchor_count']==0
    assert mart['00001']['poi_anchors_per_km2'] is None and mart['00001']['poi_anchors_per_1000_population'] is None
    again=tmp_path/'again';again.mkdir();replay,_=a.dbt_build(again,paths)
    assert all(tables[k].equals(replay[k]) for k in tables)
