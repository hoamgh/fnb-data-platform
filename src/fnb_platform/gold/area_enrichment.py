"""Offline address-first ward assignment and separate local area staging models."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from importlib.metadata import version
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
from shapely.geometry import Point

from ..overture import silver as consumer
from .. import area_demographics as demo, google_silver as silver, publication
from ..publication import check_hashes, require
from ..google_address_enrichment import norm, parse_google_address
from ..overture_incremental import _digest

VERSION = 'area-enrichment-v1-address-first'
ROOT = Path(__file__).resolve().parents[3]
PROJECT = ROOT / 'pipelines/area_enrichment'
BOUNDARY = ROOT / 'config/admin_areas/phuong_khanh_hoi.geojson'
BOUNDARY_META = ROOT / 'config/admin_areas/phuong_khanh_hoi_metadata.json'
ASSIGNMENT_SCHEMA = pa.schema([(k, pa.string()) for k in [
    'store_id', 'overture_id', 'google_id_kind', 'google_identity_value', 'store_name',
    'address_raw', 'address_source', 'coordinate_source', 'city_code', 'address_ward_name',
    'area_id', 'ward_code', 'ward_name', 'assignment_method', 'assignment_status',
    'address_lookup_status', 'address_ward_code', 'spatial_ward_code', 'spatial_status',
    'boundary_version', 'reference_version', 'provenance_json', 'rule_version'
]] + [('latitude', pa.float64()), ('longitude', pa.float64()), ('boundary_distance_m', pa.float64()),
      ('near_boundary', pa.bool_()), ('flags', pa.list_(pa.string()))])


def publish_release(output, signature, writer, *, guard=lambda:None, fault=lambda stage:None):
    return publication.publish_release(output, signature, writer, guard=guard, fault=fault,
        read_active=silver.read_active, verify_release=silver.verify_release, digest=_digest, dump=silver.dump)


def label(value):
    """Comparison only; administrative prefixes removed, no fuzzy/substring matching."""
    return norm(re.sub(r'^(?:phường|phuong|ward|xã|xa|thành phố|thanh pho|tỉnh|tinh|city)\s+', '', value or '', flags=re.I))


def reference_index(rows):
    rows = sorted(rows, key=lambda r:r['ward_code'])
    by_code = {r['ward_code']:r for r in rows}
    require(len(by_code) == len(rows), 'Duplicate reference ward code')
    wards, cities, city_labels = defaultdict(set), defaultdict(set), {}
    for r in rows:
        # Invalid/conflicting administrative codes are retained in Silver, not eligible for assignment.
        if r['legal_code_status'] != 'CODE_AND_NAME_AGREE': continue
        wards[(r['city_code'], label(r['ward_name']))].add(r['ward_code'])
        cities[label(r['city_name'])].add(r['city_code'])
        city_labels[label(r['city_name'])] = re.sub(r'^(?:Thành phố|Tỉnh)\s+', '', r['city_name'], flags=re.I)
    context = dict(wards={label(r['ward_name']):re.sub(r'^(?:Phường|Xã)\s+', '', r['ward_name'], flags=re.I)
                          for r in rows if r['legal_code_status'] == 'CODE_AND_NAME_AGREE'},
                   cities=city_labels, countries={norm('Việt Nam'), norm('Vietnam')})
    return by_code, wards, cities, context


def address_lookup(address, index, *, ward_name=None, city_code=None):
    """Use observed structured labels or reusable parser, never a pilot default city."""
    _, wards, cities, context = index
    parsed = parse_google_address(address, context)
    claimed_ward = ward_name if ward_name is not None else parsed['ward_name']
    city_claim = parsed['city_name']
    city_candidates = sorted(cities.get(label(city_claim), set())) if city_claim else []
    # An explicitly supplied source city code must agree with any identifiable address city.
    if city_code is not None and city_candidates and city_candidates != [city_code]:
        return dict(code=None, status='AMBIGUOUS', ward=claimed_ward, city_code=None,
                    evidence=dict(parser=parsed, reason='SOURCE_CITY_CODE_ADDRESS_CITY_CONFLICT'))
    chosen_city = city_code if city_code is not None else city_candidates[0] if len(city_candidates) == 1 else None
    flags = parsed['parse_notes']
    if any(f.startswith('CONFLICTING_SOURCE_WARD') or f.startswith('CONFLICTING_SOURCE_CITY') for f in flags):
        status, candidates = 'AMBIGUOUS', []
    elif not claimed_ward:
        status, candidates = 'MISSING', []
    elif re.fullmatch(r'\d+[a-z]?', label(claimed_ward) or ''):
        status, candidates = 'OLD_WARD_UNRESOLVED', []
    elif chosen_city is None:
        status, candidates = 'CITY_UNKNOWN', []
    else:
        candidates = sorted(wards.get((chosen_city, label(claimed_ward)), set()))
        status = 'RESOLVED' if len(candidates) == 1 else 'AMBIGUOUS' if candidates else 'NOT_FOUND'
    return dict(code=candidates[0] if len(candidates) == 1 else None, status=status,
                ward=claimed_ward, city_code=chosen_city,
                evidence=dict(ward_claim=claimed_ward, city_claim=city_claim, source_city_code=city_code,
                              normalized_ward=label(claimed_ward), candidate_codes=candidates, parser=parsed))


def valid_coordinates(latitude, longitude):
    return (isinstance(latitude, (int, float)) and not isinstance(latitude, bool)
            and isinstance(longitude, (int, float)) and not isinstance(longitude, bool)
            and math.isfinite(latitude) and math.isfinite(longitude)
            and -90 <= latitude <= 90 and -180 <= longitude <= 180)


def spatial_lookup(latitude, longitude, boundaries, *, near_boundary_m=10.0):
    """Reuse covers(Point) policy; optional distance is a warning, never an assignment."""
    require(math.isfinite(near_boundary_m) and near_boundary_m >= 0, 'Invalid boundary warning tolerance')
    if not valid_coordinates(latitude, longitude):
        return dict(code=None, status='MISSING_COORDINATES' if latitude is None or longitude is None else 'INVALID_COORDINATES',
                    distance_m=None, near=False, matches=[])
    point = Point(longitude, latitude)
    matches = [b['ward_code'] for b in boundaries if b['polygon'].covers(point)]
    # Local equirectangular metric only for proximity-to-boundary diagnostics.
    # Membership itself is exact covers in the existing EPSG:4326 polygon.
    from shapely.ops import transform
    factor = math.pi / 180 * 6371008.8
    project = lambda x, y, z=None: (x * factor * math.cos(math.radians(latitude)), y * factor)
    distances = [transform(project, b['polygon'].boundary).distance(transform(project, point)) for b in boundaries]
    distance = min(distances) if distances else None
    return dict(code=matches[0] if len(matches) == 1 else None,
                status='RESOLVED' if len(matches) == 1 else 'AMBIGUOUS' if matches else 'OUTSIDE_SUPPORTED_BOUNDARIES' if boundaries else 'NO_BOUNDARY_AVAILABLE',
                distance_m=distance, near=distance is not None and distance <= near_boundary_m, matches=sorted(matches))


def assign_one(row, index, boundaries, refs, *, near_boundary_m=10.0):
    address = address_lookup(row.get('address'), index, ward_name=row.get('ward_name'), city_code=row.get('city_code'))
    spatial = spatial_lookup(row.get('latitude'), row.get('longitude'), boundaries, near_boundary_m=near_boundary_m)
    ac, sc = address['code'], spatial['code']
    flags = []
    if spatial['near']: flags.append('NEAR_BOUNDARY')
    if spatial['status'] != 'RESOLVED': flags.append(spatial['status'])
    if address['status'] != 'RESOLVED': flags.append('ADDRESS_' + address['status'])
    if ac and sc and ac != sc:
        code, status, method = None, 'AREA_CONFLICT', 'UNRESOLVED'
        flags.append('AREA_CONFLICT')
    elif spatial['status'] == 'AMBIGUOUS':
        code, status, method = None, 'AMBIGUOUS', 'UNRESOLVED'
    elif ac:
        code, status, method = ac, 'RESOLVED', 'ADDRESS'
    elif sc:
        code, status, method = sc, 'RESOLVED', 'SPATIAL'
    else:
        code, status, method = None, 'AMBIGUOUS' if address['status'] == 'AMBIGUOUS' else 'UNRESOLVED', 'UNRESOLVED'
    by_code = index[0]
    # Negative membership for a known address ward whose polygon is actually available
    # is recorded separately, not misrepresented as a different spatial ward code.
    if ac and not sc and spatial['status'] == 'OUTSIDE_SUPPORTED_BOUNDARIES' and ac in {b['ward_code'] for b in boundaries}:
        flags.append('ADDRESS_WARD_OUTSIDE_ITS_BOUNDARY')
    provenance = dict(address_lookup=address['evidence'], spatial_lookup=spatial,
        source_refs=refs, source_field_provenance=row.get('field_provenance_json'),
        address_source=row.get('address_source'), coordinate_source=row.get('coordinate_source'),
        policy='Unique exact city/ward lookup first; unique polygon covers fallback; no identity decision or inference from city/district/centroid.',
        boundary_warning_m=near_boundary_m, boundary_distance_method='LOCAL_EQUIRECTANGULAR_DIAGNOSTIC_ONLY', rule_version=VERSION)
    return dict(store_id=row['store_id'], overture_id=row['overture_id'], google_id_kind=row.get('google_id_kind'),
        google_identity_value=row.get('google_identity_value'), store_name=row.get('store_name'), address_raw=row.get('address'),
        address_source=row.get('address_source'), coordinate_source=row.get('coordinate_source'),
        city_code=by_code[code]['city_code'] if code else address['city_code'], address_ward_name=address['ward'],
        area_id='vn-hcm-ward-' + code if code and by_code[code]['city_code'] == '79' else f"vn-city-{by_code[code]['city_code']}-ward-{code}" if code else None,
        ward_code=code, ward_name=by_code[code]['ward_name'] if code else None,
        assignment_method=method, assignment_status=status, address_lookup_status=address['status'],
        address_ward_code=ac, spatial_ward_code=sc, spatial_status=spatial['status'],
        boundary_version=refs['boundary_version'], reference_version=refs['reference_version'],
        provenance_json=silver.dump(provenance), rule_version=VERSION, latitude=row.get('latitude'), longitude=row.get('longitude'),
        boundary_distance_m=spatial['distance_m'], near_boundary=spatial['near'], flags=sorted(set(flags)))


def assign_all(stores, reference_rows, boundaries, refs, *, near_boundary_m=10.0):
    index = reference_index(reference_rows)
    require(len({r['store_id'] for r in stores}) == len(stores), 'Duplicate POI anchor key')
    rows = [assign_one(r, index, boundaries, refs, near_boundary_m=near_boundary_m)
            for r in sorted(stores, key=lambda r:r['store_id'])]
    require(len(rows) == len(stores) and all(r['ward_code'] is None or r['ward_code'] in index[0] for r in rows),
            'Assignment coverage/referential integrity failure')
    return pa.Table.from_pylist(rows, schema=ASSIGNMENT_SCHEMA)


def pin_inputs(reference_root, catalog_root):
    roots = [Path(reference_root).resolve(), Path(catalog_root).resolve()]
    releases = [silver.read_active(root) for root in roots]
    require(all(releases), 'Published Area Silver and catalog staging releases required')
    reference, catalog = releases
    rm, cm = [silver.verify_release(p) for p in releases]
    require(rm['signature']['rule_version'] == demo.VERSION, 'Expected Area Demographics V1')
    require(cm['publication_kind'] == 'CANONICAL_GOLD_STAGING_NOT_PRODUCTION', 'Expected local catalog staging')
    # Pin the actual Overture/Google/Category/Effective inputs of this catalog, not guesses at latest paths.
    check_hashes(cm['signature']['inputs_sha256'])
    check_hashes(rm['signature']['inputs_sha256'])
    catalog_pins = cm['signature']['inputs_sha256']
    mappings = [Path(p) for p in catalog_pins if Path(p).name == 'effective_identity_mapping.parquet']
    require(len(mappings) == 1, 'Missing/ambiguous catalog active mapping lineage')
    mapping = mappings[0]
    manifest_path = mapping.parent / 'manifest.json'
    require(catalog_pins.get(str(manifest_path.resolve())) == silver.sha(manifest_path),
            'Unpinned catalog active mapping manifest')
    active = silver.verify_release(mapping.parent)
    require(active.get('publication_kind') == 'ACTIVE_EFFECTIVE_MAPPING_FOR_GOLD_STAGING',
            'Expected active Gold mapping lineage')
    policy = active['signature']
    human_root = policy.get('human_root')
    require(isinstance(human_root, str) and bool(human_root) and Path(human_root).is_absolute()
            and 'human_pointer_sha256' in policy, 'Missing/invalid pinned human state')
    expected = policy['human_pointer_sha256']
    require(expected is None or isinstance(expected, str) and re.fullmatch(r'[0-9a-f]{64}', expected),
            'Invalid pinned human pointer checksum')
    human_pointer = Path(human_root) / 'current.json'
    def guard_human():
        present = human_pointer.exists() or human_pointer.is_symlink()
        require(not present or human_pointer.is_file(), 'Invalid human pointer: expected a file')
        require((silver.sha(human_pointer) if present else None) == expected,
                'Human decisions changed; reactivate mapping and rebuild catalog before area enrichment')
        if expected is not None:
            try:
                silver.read_active(human_root)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise ValueError('Invalid human pointer or release') from exc
    guard_human()
    paths = dict(reference_path=reference / 'area_demographics.parquet', catalog_path=catalog / 'dim_store.parquet')
    hashes = {**rm['signature']['inputs_sha256'], **cm['signature']['inputs_sha256']}
    for root, release in zip(roots, releases):
        for p in [root / 'current.json', release / 'manifest.json']:
            hashes[str(p)] = silver.sha(p)
    for p in [*paths.values(), BOUNDARY, BOUNDARY_META]: hashes[str(p)] = silver.sha(p)
    return paths, hashes, dict(reference_release=reference.name, catalog_release=catalog.name,
                              human_state=dict(pointer=str(human_pointer), sha256=expected)), guard_human


def dbt_build(stage, paths):
    variables = {k:Path(v).resolve().as_posix() for k,v in paths.items()}
    env = dict(os.environ, FNB_DBT_DATABASE=str((stage / 'area_catalog.duckdb').resolve()),
               DBT_SEND_ANONYMOUS_USAGE_STATS='false', DBT_PARTIAL_PARSE='false', PYTHONDONTWRITEBYTECODE='1')
    args = [sys.executable, '-B', '-c', 'from dbt.cli.main import cli; cli()', 'build',
            '--project-dir', str(PROJECT), '--profiles-dir', str(PROJECT),
            '--target-path', str((stage / 'dbt-target').resolve()), '--log-path', str((stage / 'dbt-logs').resolve()),
            '--vars', json.dumps(variables), '--no-use-colors']
    result = subprocess.run(args, env=env, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=120)
    (stage / 'dbt_console.log').write_text(result.stdout + result.stderr, encoding='utf-8')
    require(result.returncode == 0, 'Area dbt build failed; diagnostics: ' + str(stage / 'dbt_console.log') + '\n' + (result.stdout + result.stderr)[-2200:])
    results = demo.read_json(stage / 'dbt-target/run_results.json')['results']
    require(all(r['status'] in {'success', 'pass'} for r in results), 'Area dbt quality gate failed')
    orders = dict(dim_area='ward_code', fact_area_demographics='area_id,reference_year,source_version',
                  store_area_mapping='store_id', mart_fnb_by_area='area_id,reference_year,source_version')
    con = duckdb.connect(str(stage / 'area_catalog.duckdb'), read_only=True)
    try:
        tables = {name + '.parquet':con.execute(f'select * from area_staging.{name} order by {order}').to_arrow_table()
                  for name,order in orders.items()}
    finally: con.close()
    return tables, dict(models=sum(r['unique_id'].startswith('model.') for r in results),
                        tests=sum(r['unique_id'].startswith('test.') for r in results), failures=0)


def build(reference_root, catalog_root, output_root, *, near_boundary_m=10.0, fault=lambda stage:None):
    paths, hashes, refs, guard_human = pin_inputs(reference_root, catalog_root)
    metadata = demo.read_json(BOUNDARY_META)
    polygon = consumer.load_ward()
    require(consumer.WARD_PATH.resolve() == BOUNDARY and consumer.WARD_METADATA.resolve() == BOUNDARY_META,
            'Existing spatial loader uses a different boundary')
    refs.update(boundary_version=_digest({str(p):silver.sha(p) for p in (BOUNDARY, BOUNDARY_META)}),
                boundary_metadata=metadata, reference_version=refs['reference_release'], input_hashes=hashes)
    boundaries = [dict(ward_code=metadata['official_unit_code'], polygon=polygon)]
    reference = pq.read_table(paths['reference_path']).to_pylist()
    require(any(r['ward_code'] == metadata['official_unit_code'] and r['legal_code_status'] == 'CODE_AND_NAME_AGREE'
                for r in reference), 'Supported polygon administrative code not validated against official reference')
    code_paths = [Path(__file__), Path(demo.__file__), Path(consumer.__file__), Path(publication.__file__),
                  ROOT / 'src/fnb_platform/google/address_enrichment.py', ROOT / 'src/fnb_platform/integration/comparison_features.py',
                  *sorted(p for p in PROJECT.rglob('*') if p.is_file() and p.suffix in {'.sql', '.yml'})]
    signature = dict(rule_version=VERSION, publication_kind='AREA_ENRICHMENT_GOLD_STAGING_NOT_PRODUCTION',
        human_state=refs['human_state'],
        inputs_sha256=hashes, code_sha256={str(p.resolve()):silver.sha(p) for p in code_paths},
        near_boundary_m=near_boundary_m,
        runtime_versions={package:version(package)
                          for package in ('dbt-core', 'dbt-duckdb', 'duckdb', 'pyarrow', 'shapely')})
    def writer(stage, rid):
        stores = pq.read_table(paths['catalog_path']).to_pylist()
        assigned = assign_all(stores, reference, boundaries, refs, near_boundary_m=near_boundary_m)
        assignment_path = stage / 'ward_assignment.parquet'
        pq.write_table(assigned, assignment_path, compression='zstd')
        model_paths = dict(paths, assignment_path=assignment_path)
        tables, dbt = dbt_build(stage, model_paths)
        rows = assigned.to_pylist()
        require(tables['store_area_mapping.parquet'].num_rows == len(stores), 'Catalog join fanout/record loss')
        require(sum(r['poi_anchor_count'] for r in tables['mart_fnb_by_area.parquet'].to_pylist())
                == sum(r['ward_code'] is not None for r in rows), 'Mart accounting mismatch')
        summary = dict(release_id=rid, source_releases={k:v for k,v in refs.items() if k.endswith('_release')},
            poi_anchors=len(stores), assigned=sum(r['ward_code'] is not None for r in rows),
            unassigned=sum(r['ward_code'] is None for r in rows),
            assignment_method_counts=dict(Counter(r['assignment_method'] for r in rows)),
            assignment_status_counts=dict(Counter(r['assignment_status'] for r in rows)),
            spatial_status_counts=dict(Counter(r['spatial_status'] for r in rows)),
            address_lookup_counts=dict(Counter(r['address_lookup_status'] for r in rows)),
            ward_counts=dict(Counter(r['ward_code'] for r in rows if r['ward_code'])),
            dq_flag_counts=dict(Counter(f for r in rows for f in r['flags'])),
            reference_rows=len(reference), complete_area_population=sum(r['area_km2'] is not None and r['population'] is not None for r in reference),
            boundary_version=refs['boundary_version'], boundary_warning_m=near_boundary_m,
            reference_boundary_approximate_area_km2=metadata['validation']['approximate_area_km2'],
            demographics_are_not_census=True, counts_are_poi_anchors_not_verified_businesses=True,
            catalog_scope='Existing Khánh Hội Overture F&B candidate anchors; not complete city/ward business coverage.',
            dbt_build=dbt, production_gold=False, license=demo.LICENSE, attribution=demo.ATTRIBUTION)
        (stage / 'dq_summary.json').write_text(silver.dump(summary), encoding='utf-8')
        return tables, summary, ['ward_assignment.parquet', 'dq_summary.json', 'area_catalog.duckdb',
                                'dbt_console.log', 'dbt-target/run_results.json', 'dbt-target/manifest.json']
    return publish_release(output_root, signature, writer, guard=guard_human, fault=fault)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reference-root', type=Path, required=True)
    parser.add_argument('--catalog-root', type=Path, required=True)
    parser.add_argument('--output-root', type=Path, required=True)
    parser.add_argument('--near-boundary-m', type=float, default=10.0, help='Warning distance only; never changes ward membership')
    args = parser.parse_args()
    dest, summary = build(args.reference_root, args.catalog_root, args.output_root, near_boundary_m=args.near_boundary_m)
    print(silver.dump(dict(release=str(dest), summary=summary)))


if __name__ == '__main__': main()
