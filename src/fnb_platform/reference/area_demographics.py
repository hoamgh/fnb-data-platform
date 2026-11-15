"""Immutable administrative reference ingestion and offline demographics normalization."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
from importlib.metadata import version
import hashlib
import json
import math
import os
from pathlib import Path
import re
from urllib.request import Request, urlopen
import uuid

import pyarrow as pa

from .. import google_silver as silver, publication
from ..publication import require
from ..google_address_enrichment import norm
from ..overture_incremental import _digest

VERSION = 'area-demographics-v1'
ROOT = Path(__file__).resolve().parents[3]
DEFAULT_URL = 'https://diachiviet.com/api/v1/xa/ho-chi-minh.json'
DEFAULT_CODES = ROOT / 'config/admin_areas/qd19_2025_hcm_codes.json'
LICENSE = 'CC BY 4.0'
ATTRIBUTION = 'Địa Chỉ Việt (diachiviet.com), dữ liệu mở đơn vị hành chính, CC BY 4.0'
DOCS = 'https://diachiviet.com/du-lieu-mo/'
REQUIRED = {'ma_xa', 'ten', 'ma_tinh', 'ten_tinh', 'dien_tich_km2', 'quy_mo_dan_so', 'nghi_quyet', 'url'}
SCHEMA = pa.schema([(k, pa.string()) for k in [
    'ward_code', 'ward_name', 'city_code', 'city_name', 'legal_document',
    'source_url', 'source_record_url', 'source_version', 'data_quality_status',
    'population_basis', 'area_basis', 'legal_code_status', 'legal_code_provenance_json',
    'source_build_date', 'data_until', 'retrieved_at_utc', 'raw_row_json', 'provenance_json', 'rule_version'
]] + [('area_km2', pa.float64()), ('population', pa.int64()),
      ('population_density', pa.float64()), ('reference_year', pa.int32()),
      ('dq_flags', pa.list_(pa.string()))])


def publish_release(output, signature, writer, *, guard=lambda:None, fault=lambda stage:None):
    return publication.publish_release(output, signature, writer, guard=guard, fault=fault,
        read_active=silver.read_active, verify_release=silver.verify_release, digest=_digest, dump=silver.dump)


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def document(raw):
    """Fail on structural drift; optional values may be NULL/empty, never fabricated."""
    obj = json.loads(raw)
    require(isinstance(obj, dict) and isinstance(obj.get('cot'), list)
            and isinstance(obj.get('dong'), list), 'Unexpected reference JSON schema')
    require(REQUIRED <= set(obj['cot']), 'Required API columns missing')
    require(obj.get('giay_phep') == LICENSE, 'Reference license changed: review before ingestion')
    require(all(isinstance(r, dict) and REQUIRED <= set(r) for r in obj['dong']), 'Malformed reference row')
    require(bool(obj.get('ngay')) and bool(obj.get('du_lieu_den')), 'Missing dataset version')
    return obj


def ingest(bronze_root, *, url=DEFAULT_URL, snapshot=None, metadata=None):
    """Only this explicit ingestion entry point performs HTTP; builds are offline.

    Snapshot replay requires its original retrieval metadata, not a new observation time.
    Replaying identical bytes/metadata reuses a checked immutable directory.
    """
    if snapshot is not None:
        source = Path(snapshot).resolve()
        meta_path = Path(metadata).resolve() if metadata else source.with_name('metadata.json')
        require(source.is_file() and meta_path.is_file(), 'Offline snapshot + retrieval metadata required')
        raw, meta = source.read_bytes(), read_json(meta_path)
        require(meta['response_sha256'] == hashlib.sha256(raw).hexdigest(), 'Snapshot checksum mismatch')
        require(meta['source_url'] and meta['retrieved_at_utc'], 'Missing original retrieval provenance')
    else:
        request = Request(url, headers={'User-Agent': 'FNB-Administrative-Reference/1.0'})
        with urlopen(request, timeout=45) as response:
            require(response.status == 200, 'Reference endpoint not HTTP 200')
            require('json' in response.headers.get('Content-Type', '').lower(), 'Reference is not JSON')
            raw = response.read()
            meta = dict(source_url=url, final_url=response.url, http_status=response.status,
                        retrieved_at_utc=datetime.now(timezone.utc).isoformat(),
                        response_sha256=hashlib.sha256(raw).hexdigest(), response_bytes=len(raw),
                        response_headers=dict(response.headers))
    obj = document(raw)
    meta = dict(meta, dataset_version={'ngay': obj['ngay'], 'du_lieu_den': obj['du_lieu_den']},
                license=LICENSE, attribution=ATTRIBUTION, terms_url='https://diachiviet.com/dieu-khoan-su-dung/',
                documentation_url=DOCS, ingestion_version=VERSION)
    require(meta.get('http_status') == 200, 'Snapshot did not originate from HTTP 200')
    datetime.fromisoformat(meta['retrieved_at_utc'].replace('Z', '+00:00'))
    target = Path(bronze_root).resolve() / ('snapshot-' + _digest(meta)[:24])
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        require((target / 'response.json').read_bytes() == raw and read_json(target / 'metadata.json') == meta,
                'Immutable Bronze snapshot differs')
        verify_snapshot(target)
        return target
    stage = target.parent / ('.snapshot-' + uuid.uuid4().hex)
    stage.mkdir()
    (stage / 'response.json').write_bytes(raw)
    (stage / 'metadata.json').write_text(silver.dump(meta), encoding='utf-8')
    (stage / 'manifest.json').write_text(silver.dump(dict(snapshot_version=VERSION,
        artifacts={n: silver.sha(stage / n) for n in ('response.json', 'metadata.json')})), encoding='utf-8')
    verify_snapshot(stage)
    os.rename(stage, target)
    return target


def verify_snapshot(directory):
    directory = Path(directory)
    meta = read_json(directory / 'metadata.json')
    require(silver.sha(directory / 'response.json') == meta['response_sha256'], 'Bronze checksum mismatch')
    manifest = directory / 'manifest.json'
    if manifest.exists():
        for name, digest in read_json(manifest)['artifacts'].items():
            require(silver.sha(directory / name) == digest, 'Bronze manifest mismatch')
    require(meta.get('http_status') == 200 and bool(meta.get('source_url'))
            and bool(meta.get('retrieved_at_utc')), 'Incomplete Bronze retrieval metadata')
    return document((directory / 'response.json').read_bytes()), meta


def numeric(value, field, flags, *, integer=False, positive=False):
    if value is None or value == '':
        flags.append(field.upper() + '_MISSING')
        return None
    try:
        require(not isinstance(value, bool), 'Boolean is not a demographic number')
        result = float(value)
        require(math.isfinite(result) and (result > 0 if positive else result >= 0), 'Out of range')
        if integer:
            require(result.is_integer() and result <= 2**63 - 1, 'Invalid population integer')
            return int(result)
        return result
    except (ValueError, TypeError, OverflowError):
        flags.append(field.upper() + '_INVALID')
        return None


def normalize(obj, meta, codes, *, reference_year, population_basis):
    require(reference_year > 0 and population_basis in {'2025_REORGANIZATION_PROPOSAL_NOT_CENSUS', 'UNKNOWN'},
            'Explicit supported reference basis required')
    require(population_basis != '2025_REORGANIZATION_PROPOSAL_NOT_CENSUS' or reference_year == 2025,
            '2025 proposal basis requires reference year 2025')
    official = {r['ward_code']: r for r in codes['rows']}
    require(len(official) == len(codes['rows']), 'Duplicate official reference code')
    rows = []
    source_version = f"diachiviet:{obj['ngay']}:{meta['response_sha256']}"
    for raw in obj['dong']:
        code, city = raw['ma_xa'], raw['ma_tinh']
        require(isinstance(code, str) and re.fullmatch(r'\d{5}', code), 'Invalid ward code; leading zeros must be preserved')
        require(isinstance(city, str) and re.fullmatch(r'\d{2}', city), 'Invalid city code')
        flags = []
        area = numeric(raw['dien_tich_km2'], 'area_km2', flags, positive=True)
        population = numeric(raw['quy_mo_dan_so'], 'population', flags, integer=True)
        expected = official.get(code)
        code_status = 'CODE_AND_NAME_AGREE' if (expected and city == codes['city_code']
            and norm(raw['ten']) == norm(expected['ward_name']) and norm(raw['ten_tinh']) == norm(codes['city_name'])) else 'CONFLICT' if expected else 'NOT_CHECKED'
        if code_status != 'CODE_AND_NAME_AGREE': flags.append('ADMIN_CODE_' + code_status)
        for field in ('ten', 'ten_tinh', 'nghi_quyet', 'url'):
            if not isinstance(raw[field], str) or not raw[field].strip(): flags.append(field.upper() + '_MISSING_OR_INVALID')
        # Even a complete source row is an unverified aggregator observation.
        flags.append('DEMOGRAPHICS_NOT_INDEPENDENTLY_VERIFIED')
        if population_basis == 'UNKNOWN': flags.append('POPULATION_REFERENCE_BASIS_UNKNOWN')
        provenance = dict(snapshot_sha256=meta['response_sha256'], retrieved_at_utc=meta['retrieved_at_utc'],
                          source_url=meta['source_url'], source_record_key=code, source_fields={
                              'area_km2': 'dien_tich_km2', 'population': 'quy_mo_dan_so', 'legal_document': 'nghi_quyet'},
                          license=LICENSE, attribution=ATTRIBUTION, reference_basis_source=DOCS,
                          population_basis=population_basis, raw_build_date=obj['ngay'], raw_data_until=obj['du_lieu_den'])
        rows.append(dict(ward_code=code, ward_name=raw['ten'] if isinstance(raw['ten'], str) else None,
            city_code=city, city_name=raw['ten_tinh'] if isinstance(raw['ten_tinh'], str) else None,
            area_km2=area, population=population, population_density=population / area if population is not None and area is not None else None,
            reference_year=reference_year, legal_document=raw['nghi_quyet'] if isinstance(raw['nghi_quyet'], str) else None,
            source_url=meta['source_url'], source_record_url=raw['url'] if isinstance(raw['url'], str) else None,
            source_version=source_version, data_quality_status='WARNING', population_basis=population_basis,
            area_basis='PROVIDER_REORGANIZATION_REFERENCE_NOT_INDEPENDENTLY_VERIFIED', legal_code_status=code_status,
            legal_code_provenance_json=silver.dump(dict(reference_version=codes['version'], source_url=codes['source_url'],
                source_pdf_sha256=codes['source_pdf_sha256'], effective_from=codes['effective_from'],
                expected=expected, expected_city_code=codes['city_code'], actual_city_code=city)),
            source_build_date=obj['ngay'], data_until=obj['du_lieu_den'], retrieved_at_utc=meta['retrieved_at_utc'],
            raw_row_json=silver.dump(raw), provenance_json=silver.dump(provenance), rule_version=VERSION, dq_flags=sorted(flags)))
    rows.sort(key=lambda r:r['ward_code'])
    require(len({r['ward_code'] for r in rows}) == len(rows), 'Duplicate ward_code: publication blocked')
    return pa.Table.from_pylist(rows, schema=SCHEMA)


def publish_silver(snapshot_dir, output_root, *, codes_path=DEFAULT_CODES, reference_year=2025,
                   population_basis='2025_REORGANIZATION_PROPOSAL_NOT_CENSUS', fault=lambda stage:None):
    snapshot_dir, codes_path = Path(snapshot_dir).resolve(), Path(codes_path).resolve()
    obj, meta = verify_snapshot(snapshot_dir)
    paths = [snapshot_dir / 'response.json', snapshot_dir / 'metadata.json', codes_path]
    if (snapshot_dir / 'manifest.json').exists(): paths.append(snapshot_dir / 'manifest.json')
    signature = dict(rule_version=VERSION, publication_kind='AREA_DEMOGRAPHICS_SILVER',
        inputs_sha256={str(p):silver.sha(p) for p in paths},
        code_sha256={str(p.resolve()):silver.sha(p) for p in [Path(__file__), Path(publication.__file__),
            ROOT / 'src/fnb_platform/google/address_enrichment.py', ROOT / 'src/fnb_platform/integration/comparison_features.py']},
        reference_year=reference_year, population_basis=population_basis,
        runtime_versions={p:version(p) for p in ('pyarrow',)})
    def writer(stage, rid):
        table = normalize(obj, meta, read_json(codes_path), reference_year=reference_year, population_basis=population_basis)
        rows = table.to_pylist()
        summary = dict(release_id=rid, rows=len(rows), complete_area_population=sum(r['area_km2'] is not None and r['population'] is not None for r in rows),
            legal_code_counts=dict(Counter(r['legal_code_status'] for r in rows)),
            dq_flag_counts=dict(Counter(f for r in rows for f in r['dq_flags'])), source_url=meta['source_url'],
            source_version=rows[0]['source_version'] if rows else None, reference_year=reference_year, population_basis=population_basis,
            license=LICENSE, attribution=ATTRIBUTION, production_gold=False)
        (stage / 'dq_summary.json').write_text(silver.dump(summary), encoding='utf-8')
        return {'area_demographics.parquet':table}, summary, ['dq_summary.json']
    return publish_release(output_root, signature, writer, fault=fault)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    ingest_cmd = sub.add_parser('ingest')
    ingest_cmd.add_argument('--bronze-root', type=Path, required=True)
    group = ingest_cmd.add_mutually_exclusive_group(required=True)
    group.add_argument('--url')
    group.add_argument('--snapshot', type=Path)
    ingest_cmd.add_argument('--metadata', type=Path)
    normalize_cmd = sub.add_parser('silver')
    normalize_cmd.add_argument('--snapshot-dir', type=Path, required=True)
    normalize_cmd.add_argument('--output-root', type=Path, required=True)
    normalize_cmd.add_argument('--codes', type=Path, default=DEFAULT_CODES)
    normalize_cmd.add_argument('--reference-year', type=int, default=2025)
    normalize_cmd.add_argument('--population-basis', default='2025_REORGANIZATION_PROPOSAL_NOT_CENSUS')
    args = parser.parse_args()
    if args.command == 'ingest':
        print(silver.dump(dict(snapshot_dir=str(ingest(args.bronze_root, url=args.url, snapshot=args.snapshot, metadata=args.metadata)))))
    else:
        dest, summary = publish_silver(args.snapshot_dir, args.output_root, codes_path=args.codes,
            reference_year=args.reference_year, population_basis=args.population_basis)
        print(silver.dump(dict(release=str(dest), summary=summary)))


if __name__ == '__main__': main()
