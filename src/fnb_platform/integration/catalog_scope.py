"""Gold catalog scope admission, separate from unchanged canonical ER decisions."""
from collections import Counter
import json
import math
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from shapely.geometry import shape, Point

from . import mapping_publication as mapping
from ..publication import require, check_hashes
from ..overture_incremental import _digest

POLICY = 'khanh-hoi-google-coordinate-covers-v1'


def apply(table, google, boundary_path):
    """Hold out-of-catalog attachments; do not change ER or invent human decisions."""
    feature=json.loads(Path(boundary_path).read_text(encoding='utf-8-sig'))
    polygon=shape(feature['geometry'])
    require(polygon.is_valid and not polygon.is_empty, 'Invalid catalog boundary')
    places={(r['google_id_kind'],r['google_identity_value']):r for r in google}
    rows=[]
    for old in table.to_pylist():
        row=dict(old)
        if row['effective_status'] in mapping.staging.ACCEPTED:
            place=places[(row['google_id_kind'],row['google_identity_value'])]
            lat,lon=place.get('latitude'),place.get('longitude')
            valid=all(isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v) for v in (lat,lon))
            valid=valid and -90<=lat<=90 and -180<=lon<=180
            inside=valid and polygon.covers(Point(lon,lat))
            if not inside:
                evidence=json.loads(row['evidence_json'])
                evidence['catalog_scope']=dict(policy=POLICY,boundary_sha256=mapping.silver.sha(boundary_path),
                    google_coordinates=[lat,lon],previous_status=row['effective_status'],
                    identity_decision_unchanged=True,inside=False)
                row.update(effective_status='PENDING',final_decision='REVIEW_REQUIRED',
                    decision_source='CATALOG_SCOPE_GUARD',evidence_json=mapping.silver.dump(evidence),
                    evidence_hash=_digest(evidence),reason=row['reason']+' Catalog attachment held: Google point outside supported boundary.')
                row['reason_codes']=sorted(set(row['reason_codes']+['CATALOG_SCOPE_OUTSIDE_OR_MISSING']))
                row['activation_evidence_hash']=_digest(['canonical-mapping',row['evidence_hash']])
        rows.append(row)
    return pa.Table.from_pylist(rows,schema=table.schema)


def publish(er_root,category_root,human_root,output_root,boundary_path):
    data,hashes,roots=mapping.review.load(er_root,category_root)
    for root in (Path(er_root),Path(category_root)):
        release=mapping.silver.read_active(root)
        pins=mapping.silver.verify_release(release)['signature']['inputs_sha256']
        check_hashes(pins)
        require(all(p not in hashes or hashes[p]==h for p,h in pins.items()),'Inconsistent input pins')
        hashes.update(pins)
    human_root=Path(human_root).resolve();output=Path(output_root).resolve()
    require(all(not output.is_relative_to(r) and not r.is_relative_to(output) for r in [*roots,human_root]),'Separate publication roots required')
    pointer=human_root/'current.json';before=mapping.silver.sha(pointer) if pointer.exists() else None
    history_release,history=mapping.human.read_history(human_root)
    if history_release:
        for p in (pointer,history_release/'manifest.json',history_release/'decision_history.parquet'):
            hashes[str(p)]=mapping.silver.sha(p)
    boundary_path=Path(boundary_path).resolve()
    hashes[str(boundary_path)]=mapping.silver.sha(boundary_path)
    google=pq.read_table(data[4]['refs']['google_places']['path']).to_pylist()
    provenance=dict(input_hashes=hashes,human_release=str(history_release) if history_release else None,human_history_unchanged=True)
    raw,raw_summary=mapping.evaluate(data,google,history,provenance)
    table=apply(raw,google,boundary_path)
    counts=dict(Counter(r['effective_status'] for r in table.to_pylist()))
    summary=dict(mode='CANONICAL_MAPPING_WITH_CATALOG_SCOPE',production_gold=False,activation_scope='GOLD_STAGING_ONLY',
        effective_status_counts=counts,final_decisions=dict(Counter(r['final_decision'] for r in table.to_pylist())),
        raw_canonical_status_counts=raw_summary['effective_status_counts'],
        catalog_scope_held=sum(r['decision_source']=='CATALOG_SCOPE_GUARD' for r in table.to_pylist()),
        human_history_rows=len(history),source_context={k:data[4][k] for k in
            ('er_release_id','category_release_id','google_silver_release_id')})
    code=[Path(m.__file__).resolve() for m in (mapping,mapping.legacy,mapping.review,mapping.human,
        mapping.matching_policy,mapping.matching_evidence,mapping.staging,mapping.publication,mapping.silver)]+[Path(__file__).resolve()]
    signature=dict(rule_version=mapping.staging.ACTIVE_VERSION,mapping_version=mapping.VERSION,
        publication_kind='ACTIVE_EFFECTIVE_MAPPING_FOR_GOLD_STAGING',inputs_sha256=hashes,
        code_sha256={str(p):mapping.silver.sha(p) for p in code},human_root=str(human_root),
        human_pointer_sha256=before,source_context=summary['source_context'],logical_sha256=_digest(table.to_pylist()),
        catalog_scope=dict(policy=POLICY,boundary_path=str(boundary_path),boundary_sha256=hashes[str(boundary_path)]))
    def guard():
        check_hashes({**hashes,**signature['code_sha256']})
        require((mapping.silver.sha(pointer) if pointer.exists() else None)==before,'Human decisions changed')
    guard()
    return mapping.staging.publish_release(output,signature,
        lambda stage,rid:({'effective_identity_mapping.parquet':table},summary,[]),guard=guard)
