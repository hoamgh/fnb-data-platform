"""Offline candidate-only spatial pre-dedup; no identity merges or publication."""
from __future__ import annotations

import argparse
from collections import Counter
from difflib import SequenceMatcher
import json
import math
from pathlib import Path
import re
import sys
import time

import pyarrow as pa
import pyarrow.parquet as pq
from shapely.geometry import Point, box, shape
from shapely.strtree import STRtree

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from experiments.overture_gold_contract import eligibility as e

EARTH_M = 6371008.8
PAIR_SCHEMA = pa.schema([
    ('left_id', pa.string()), ('right_id', pa.string()), ('distance_m', pa.float64()),
    ('name_similarity', pa.float64()), ('address_similarity', pa.float64()),
    ('likely_duplicate', pa.bool_()), ('evidence', pa.string()), ('reason', pa.string()),
])


def valid_coordinates(lon, lat):
    return (isinstance(lon, (int, float)) and not isinstance(lon, bool)
            and isinstance(lat, (int, float)) and not isinstance(lat, bool)
            and math.isfinite(lon) and math.isfinite(lat)
            and -180 <= lon <= 180 and -90 <= lat <= 90)


def distance_m(a, b):
    """Spherical great-circle metres, not survey-grade ellipsoidal distance."""
    x1, y1, x2, y2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((y2-y1)/2)**2 + math.cos(y1)*math.cos(y2)*math.sin((x2-x1)/2)**2
    return 2 * EARTH_M * math.asin(math.sqrt(min(1, max(0, h))))


def similarity(a, b):
    a, b = e.consumer.normalize(a), e.consumer.normalize(b)
    return SequenceMatcher(None, a, b, autojunk=False).ratio() if a and b else None


def name_similarity(a, b):
    """Mean of both directional scores; independent of source-ID ordering."""
    forward, reverse = similarity(a, b), similarity(b, a)
    return (forward + reverse) / 2 if forward is not None and reverse is not None else None


def house_number(address):
    match = re.match(r'^(\d+[a-z]?(?:/\d+[a-z]?)*)\b', address or '', re.I)
    return match.group(1).casefold() if match else None


def annotate(rows, raw, polygon, boundary_hash):
    """Append DQ and inferred geography; leave every existing column untouched."""
    counts = Counter(r.get('source_id') for r in rows)
    result = []
    for row in rows:
        key = row.get('source_id')
        issues = []
        if not key: issues.append('MISSING_ID')
        elif counts[key] > 1: issues.append('DUPLICATE_ID')
        if not e.consumer.normalize(row.get('name')): issues.append('MISSING_NAME')
        good = valid_coordinates(row.get('longitude'), row.get('latitude'))
        if not good: issues.append('INVALID_COORDINATES')
        tx = e.m242.taxonomy_decision({'taxonomy': {
            'primary': row.get('taxonomy_primary'), 'hierarchy': row.get('taxonomy_hierarchy'),
            'alternates': row.get('taxonomy_alternates')}})
        if tx != 'INCLUDE_HIERARCHY_FNB': issues.append(tx)
        inside = good and polygon.covers(Point(row['longitude'], row['latitude']))
        addresses = raw.get(key, {}).get('addresses') or []
        # locality is often an old district, not a ward or city: do not relabel it.
        address = next((a for a in addresses if a), {})
        ward, city = address.get('ward'), address.get('city')
        result.append(dict(row, prededup_dq_reasons=issues,
                           spatial_index_eligible=good and bool(key) and counts[key] == 1,
                           ward_original=ward, city_original=city,
                           ward_inferred='Phường Khánh Hội' if inside and not ward else None,
                           city_inferred='Thành phố Hồ Chí Minh' if inside and not city else None,
                           geography_inference_basis='POLYGON_COVERS' if inside and (not ward or not city) else None,
                           geography_boundary_sha256=boundary_hash))
    return result


def candidate_pairs(rows, radius_m=50):
    """STRtree envelope blocking followed by exact spherical radius filtering.

    Query one point at a time; never materialize the N x N distance matrix.
    Longitude envelopes include dateline copies for completeness.
    """
    if not math.isfinite(radius_m) or not 0 < radius_m <= 100000:
        raise ValueError('radius_m must be finite and in (0, 100000]')
    ids = Counter(r.get('source_id') for r in rows)
    indexed = sorted((r for r in rows if r.get('source_id') and ids[r['source_id']] == 1
                      and valid_coordinates(r.get('longitude'), r.get('latitude'))), key=lambda r: r['source_id'])
    points = [Point(r['longitude'], r['latitude']) for r in indexed]
    tree = STRtree(points)
    angular = radius_m / EARTH_M
    dy = math.degrees(angular)
    output, comparisons = [], 0
    for i, row in enumerate(indexed):
        lon, lat = row['longitude'], row['latitude']
        dx = 180 if abs(lat) + dy >= 90 else math.degrees(math.asin(min(1, math.sin(angular)/math.cos(math.radians(lat)))))
        neighbors = set()
        for shift in (-360, 0, 360):
            neighbors.update(int(j) for j in tree.query(box(lon-dx+shift, lat-dy, lon+dx+shift, lat+dy)))
        for j in sorted(neighbors):
            if j <= i: continue
            other = indexed[j]
            comparisons += 1
            d = distance_m((lon, lat), (other['longitude'], other['latitude']))
            if d > radius_m + 1e-8: continue
            ns = name_similarity(row.get('name'), other.get('name'))
            ads = similarity(row.get('address'), other.get('address'))
            left_house, right_house = house_number(row.get('address')), house_number(other.get('address'))
            contradiction = bool(left_house and right_house and left_house != right_house)
            likely = ns is not None and ns >= .9 and ads is not None and ads >= .85 and not contradiction
            output.append(dict(left_id=row['source_id'], right_id=other['source_id'], distance_m=d,
                               name_similarity=ns, address_similarity=ads, likely_duplicate=likely,
                               evidence=json.dumps({'within_radius_m': radius_m, 'house_number_conflict': contradiction,
                                                    'left_house_number': left_house, 'right_house_number': right_house,
                                                    'name_threshold': .9, 'address_threshold': .85}, sort_keys=True),
                               reason='HOUSE_NUMBER_CONFLICT' if contradiction else ('LIKELY_DUPLICATE_REVIEW' if likely else 'SPATIAL_NEIGHBOR_REVIEW')))
    return output, comparisons


def run(input_root, output_dir, radius_m=50):
    started = time.perf_counter()
    input_root, output_dir = Path(input_root).resolve(), Path(output_dir).resolve()
    if not output_dir.is_relative_to(ROOT / 'output') or output_dir.is_relative_to(input_root):
        raise ValueError('Output must be under pipeline output/ and outside read-only input root')
    inputs = e.validate_inputs(input_root)
    tables, metrics = e.build_tables(inputs, e.BASELINE)
    feature = e.read_json(inputs['boundary'])
    polygon = shape(feature['geometry'] if feature['type'] == 'Feature' else feature['features'][0]['geometry'])
    original = tables['candidates.parquet']
    rows = annotate(original.to_pylist(), inputs['raw'], polygon, e.m242.sha(inputs['boundary']))
    pairs, comparisons = candidate_pairs(rows, radius_m)
    # Preserve typed original columns, including all 725 rows, even if DQ flags exist.
    result = original
    for name in rows[0]:
        if name not in original.column_names:
            typ = pa.list_(pa.string()) if name == 'prededup_dq_reasons' else pa.bool_() if name == 'spatial_index_eligible' else pa.string()
            result = result.append_column(name, pa.array([r[name] for r in rows], type=typ))
    for name, digest in inputs['fingerprints'].items():
        e.require(e.m242.sha(input_root / name) == digest, 'Input changed during pre-dedup')
    output_dir.mkdir(parents=True, exist_ok=False)
    pq.write_table(result, output_dir / 'candidates.parquet', compression='zstd')
    pq.write_table(pa.Table.from_pylist(pairs, schema=PAIR_SCHEMA), output_dir / 'candidate_pairs.parquet', compression='zstd')
    summary = dict(counts=metrics['counts'], radius_m=radius_m, candidate_pairs=len(pairs),
                   likely_duplicate_pairs=sum(p['likely_duplicate'] for p in pairs),
                   spatial_distance_comparisons=comparisons,
                   dq_reasons=dict(Counter(x for r in rows for x in r['prededup_dq_reasons'])),
                   ward_inferred=sum(r['ward_inferred'] is not None for r in rows),
                   city_inferred=sum(r['city_inferred'] is not None for r in rows),
                   runtime_seconds=time.perf_counter()-started,
                   input_sha256=inputs['fingerprints'], eligibility_policy=e.POLICY,
                   code_sha256={p.name: e.m242.sha(p) for p in [Path(__file__), Path(e.__file__), Path(e.m242.__file__), Path(e.consumer.__file__)]},
                   distance_model='haversine sphere R=6371008.8m; inclusive radius',
                   similarity_model='Silver NFKC/casefold/whitespace + difflib SequenceMatcher; names=mean(forward,reverse) v2; addresses=directional v1 unchanged',
                   interpretation='Provisional pair flags only; no merge, deletion, Google verification or Gold publication',
                   output_sha256={p.name: e.m242.sha(p) for p in output_dir.glob('*.parquet')})
    e.write_json(output_dir / 'summary.json', summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input-root', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--radius-m', type=float, default=50)
    args = parser.parse_args()
    run(args.input_root, args.output_dir, args.radius_m)
