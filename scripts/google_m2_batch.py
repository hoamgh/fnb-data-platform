"""Prepare 725 authoritative candidates; execute at most one resumable M2 batch.

Preparation is offline. Live execution needs --execute --operator-submit.
Never replays RUNNING/unknown outcomes; completed artifacts are validated for reuse.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys

import pyarrow as pa
import pyarrow.parquet as pq
from shapely.geometry import Point

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from experiments.overture_gold_contract import eligibility as e
from scripts import google_live_workers as workers
from crawl_experiment.sources.google_maps.poi_input import search_plan, coordinates

POLICY = dict(max_scrolls=5, max_reviews=50, review_idle_limit=2)


def progress_snapshot(places, output):
    """One reconciled row per campaign ID; keep failed/unresolved attempts visible."""
    records = {p['place_key']:dict(place_key=p['place_key'], state='PENDING', attempts=[])
               for p in places}
    if len(records) != len(places):
        raise ValueError('Duplicate campaign ID')
    import sqlite3
    for queue in sorted(output.glob('batch-*/dispatch.sqlite3')):
        with sqlite3.connect(queue.as_uri()+'?mode=ro', uri=True) as db:
            tasks=db.execute('SELECT payload,state,result FROM dispatch').fetchall()
        for payload, state, result in tasks:
            key=json.loads(payload)['place_key']
            if key not in records:
                raise ValueError('Dispatch contains a foreign campaign ID')
            result=json.loads(result) if result else {}
            status=result.get('status')
            disposition=('CRAWL_ELIGIBLE' if state=='FINISHED' and result.get('crawl_eligible') is True
                         and result.get('identity_status')=='IDENTITY_UNVERIFIED'
                         and status in {'COMPLETE','NO_REVIEWS','PARTIAL_LIMIT','PARTIAL_TIMEOUT'}
                         else 'VERIFIED' if state=='FINISHED' and result.get('place_entity_confirmed') is True
                         and status in {'COMPLETE','NO_REVIEWS','PARTIAL_LIMIT','PARTIAL_TIMEOUT'}
                         else (result.get('verification_status') if result.get('verification_status') in
                               {'REVIEW_REQUIRED','UNRESOLVED','WRONG_BRANCH'} else 'UNRESOLVED')
                         if state=='FINISHED' and status=='PLACE_RESOLUTION_FAILED'
                         else 'FAILED' if state=='FINISHED' else 'IN_PROGRESS_UNKNOWN' if state=='RUNNING' else 'PENDING')
            records[key]['attempts'].append(dict(queue=str(queue), state=state, status=status,
                                                 disposition=disposition))
            if disposition!='PENDING' or records[key]['state']=='PENDING':
                records[key]['state']=disposition
    counts={state:sum(r['state']==state for r in records.values())
            for state in ['PENDING','CRAWL_ELIGIBLE','VERIFIED','REVIEW_REQUIRED','UNRESOLVED','WRONG_BRANCH','FAILED','IN_PROGRESS_UNKNOWN']}
    assert sum(counts.values())==len(places)
    snapshot=dict(total_candidates=len(places), counts=counts, records=list(records.values()), limits=POLICY)
    workers.write_json(output/'campaign_progress.json',snapshot)
    return snapshot


def verified_result(job, place):
    """Reuse only explicit branch evidence, bounded policy and reconciled files."""
    payload = e.read_json(job / 'input.json')
    if payload['overture'] != place:
        return None
    worker_plan = e.read_json(job.parent / 'plan.json')
    if any(worker_plan.get(k) != v for k, v in POLICY.items()):
        return None
    summaries = list((job / 'crawl').glob('*/summary.json'))
    if len(summaries) != 1:
        return None
    result = e.read_json(summaries[0])['stores'][0]
    if result.get('status') not in {'COMPLETE', 'NO_REVIEWS', 'PARTIAL_LIMIT'}:
        return None
    metadata_files = list((summaries[0].parent / 'metadata').glob('*.json'))
    metadata = [e.read_json(p) for p in metadata_files if not p.name.endswith('.place_details.json')]
    if not any(m.get('place_entity_confirmed') is True for m in metadata):
        return None
    if not any(p.name.endswith('.place_details.json') for p in metadata_files):
        return None
    reconciliation = workers.reconcile(job / 'reviews.sqlite3', job / 'lake')
    if reconciliation['pending'] or reconciliation['sqlite_records'] > 50 or result.get('scroll_count', 999) > 5:
        return None
    return dict(place_key=place['place_key'], source_job=str(job), verification_status=result.get('identity_status') or 'VERIFIED',
                collection_status=result['status'], scroll_count=result.get('scroll_count', 0),
                collected_review_count=reconciliation['sqlite_records'])


def prepare(input_root, output):
    input_root, output = Path(input_root).resolve(), Path(output).resolve()
    if not output.is_relative_to(ROOT / 'output') or output.is_relative_to(input_root):
        raise ValueError('Output must be in pipeline output/, outside original inputs')
    inputs = e.validate_inputs(input_root)
    tables, metrics = e.build_tables(inputs, e.BASELINE)
    polygon=e.consumer.load_ward()
    places = [dict(place_key='overture:' + r['source_id'], source='overture', name=r['name'],
                   address=r['address'], lat=r['latitude'], lng=r['longitude'],
                   category_hierarchy=r['taxonomy_hierarchy'], admin_area_name=None,
                   identity_status='UNVERIFIED', source_release=r['source_release'],
                   source_address_missing=not bool((r['address'] or '').strip()),
                   search_mode=None,
                   dq_flags=['SOURCE_ADDRESS_MISSING'] if not (r['address'] or '').strip() else [],
                   geography_inference_basis='POLYGON_COVERS',
                   geography_boundary_sha256=e.m242.sha(inputs['boundary']))
              for r in tables['candidates.parquet'].to_pylist()]
    boundary=e.read_json(inputs['boundary'])
    context=boundary.get('properties',{}).get('admin_area_name')
    for place in places:
        lat,lon=coordinates(place)
        inside=lat is not None and polygon.covers(Point(lon,lat))
        place['admin_area_name']=context if inside else None
        place['geography_inference_basis']='POLYGON_COVERS' if inside else None
        plan=search_plan(place)
        place.update(search_mode=plan['mode'],search_eligible=plan['eligible'],
                     search_reason=plan['reason'],dq_flags=plan['dq_flags'])
        workers.store_for(place)
    signature = dict(inputs=inputs['fingerprints'], limits=POLICY,
                     code_sha256={str(p.relative_to(ROOT)):e.m242.sha(p) for p in [
                         Path(__file__), Path(workers.__file__),
                         ROOT/'crawl_experiment/sources/google_maps/crawler.py',
                         ROOT/'crawl_experiment/sources/google_maps/paginator.py',
                         ROOT/'crawl_experiment/runners/full_crawl_runner.py',
                         ROOT/'crawl_experiment/sources/google_maps/poi_input.py',
                         ROOT/'crawl_experiment/core/models.py',
                         ROOT/'scripts/google_overture_workers.py']})
    signature['branch_verifier_sha256']=e.m242.sha(ROOT/'crawl_experiment/sources/google_maps/navigator.py')
    output.mkdir(parents=True, exist_ok=True)
    manifest = output / 'manifest.json'
    if manifest.exists():
        prior = e.read_json(manifest)
        if prior['signature'] != signature or e.m242.sha(output/'catalog.parquet') != prior['catalog_sha256']:
            raise ValueError('Campaign changed; use a new output directory')
    else:
        pq.write_table(pa.Table.from_pylist(places), output / 'catalog.parquet', compression='zstd')
        workers.write_json(manifest, dict(signature=signature, counts=metrics['counts'],
                                        catalog_sha256=e.m242.sha(output/'catalog.parquet')))
    workers.write_json(output/'preparation_summary.json', dict(total_candidates=len(places),
                       limits=POLICY, live_started=False,
                       eligible_for_branch_search=sum(p['search_eligible'] for p in places),
                       address_ready=sum(not p['source_address_missing'] for p in places),
                       missing_address_fallback=sum(p['source_address_missing'] for p in places),
                       missing_address_ids=[p['place_key'] for p in places if p['source_address_missing']]))
    progress_snapshot(places,output)
    return sorted(places, key=lambda p:p['place_key'])


def execute_batch(places, output, size, profile_dirs, *, operator_submit=False):
    if not operator_submit:
        raise ValueError('--operator-submit required; confirm permitted Google access/storage/reuse first')
    # Only one controller; a hard-kill lock requires operator inspection.
    with (output/'controller.lock').open('x'):
        try:
            return _execute_batch(places, output, size, profile_dirs)
        finally:
            (output/'controller.lock').unlink()


def _execute_batch(places, output, size, profile_dirs):
    progress_snapshot(places, output)
    known, reused, unknown = set(), [], []
    for run in sorted(output.glob('batch-*')):
        queue = run/'dispatch.sqlite3'
        if not queue.exists():
            continue
        import sqlite3
        with sqlite3.connect(queue.as_uri()+'?mode=ro', uri=True) as db:
            tasks = db.execute('SELECT payload,state FROM dispatch').fetchall()
        for payload, state in tasks:
            place = json.loads(payload)
            jobs = [p for p in run.glob('worker-*/job-*') if (p/'input.json').exists()
                    and e.read_json(p/'input.json')['overture']['place_key'] == place['place_key']]
            valid = verified_result(jobs[0], place) if state == 'FINISHED' and len(jobs) == 1 else None
            if valid:
                reused.append(valid); known.add(place['place_key'])
            elif state != 'PENDING':
                unknown.append(place['place_key']); known.add(place['place_key'])
        if (run/'SOURCE_STOP.json').exists() or list(run.glob('worker-*/WORKER_STOP.json')):
            raise RuntimeError('Prior access/session stop requires operator review; no automatic continuation')
    available = [p for p in places if p['place_key'] not in known]
    if size==4:
        # Mixed pilot exercises both paths; subsequent normal batches use ID order.
        fallback=[p for p in available if not (p.get('address') or '').strip()][:2]
        addressed=[p for p in available if (p.get('address') or '').strip()][:4-len(fallback)]
        selected=fallback+addressed
    else:
        selected=available[:size]
    workers.write_json(output/'batch_selection.json', dict(total_candidates=len(places), reused=reused,
                       unknown_no_replay=unknown,
                       selected=[p['place_key'] for p in selected], limits=POLICY))
    if not selected:
        return
    number = len(list(output.glob('batch-*'))) + 1
    catalog = output/f'selection-{number:03d}.parquet'
    pq.write_table(pa.Table.from_pylist(selected), catalog)
    # Each invocation runs one bounded batch, never all 725.
    try:
        result = workers.run(catalog, output/f'batch-{number:03d}', limit=len(selected),
                             workers=min(4, len(selected)), per_store_seconds=120,
                             total_seconds=180 + 120*len(selected), operator_submit=True,
                             profile_dir=profile_dirs or None, **POLICY)
    finally:
        progress_snapshot(places,output)
    workers.write_json(output/'last_batch_summary.json', result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input-root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--pilot', action='store_true', help='Select at most four actual candidates')
    parser.add_argument('--batch-size', type=int, default=25, choices=range(25,51))
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--operator-submit', action='store_true')
    parser.add_argument('--profile-dir', type=Path, action='append')
    args = parser.parse_args()
    places = prepare(args.input_root, args.output)
    print(json.dumps(dict(prepared=len(places), limits=POLICY, live_started=False)))
    if args.execute:
        if not args.pilot:
            pilot_path=args.output/'pilot_acceptance.json'
            if not pilot_path.exists() or e.read_json(pilot_path).get('passed') is not True:
                raise ValueError('Run and validate the four-store --pilot before a 25–50 store batch')
        execute_batch(places, args.output.resolve(), 4 if args.pilot else args.batch_size,
                      args.profile_dir, operator_submit=args.operator_submit)
        if args.pilot:
            accepted=[]
            for job in args.output.glob('batch-*/worker-*/job-*'):
                if (job/'input.json').exists():
                    place=e.read_json(job/'input.json')['overture']
                    value=verified_result(job,place)
                    if value: accepted.append(value)
            last=e.read_json(args.output/'last_batch_summary.json')
            workers.write_json(args.output/'pilot_acceptance.json', dict(
                passed=last.get('completed')==4 and all(
                    any(a['place_key']==r['place_key'] for a in accepted)
                    or (r.get('status')=='PLACE_RESOLUTION_FAILED'
                        and r.get('collected_review_count',0)==0)
                    for r in last.get('results',[])) and len(last.get('results',[]))==4
                       and last.get('cleanup_verified') is True
                       and last.get('stop_reason') is None,
                validated_results=accepted,
                interpretation='Bounded collection or conservative unresolved outcome; not identity accuracy or complete review coverage'))


if __name__ == '__main__':
    main()
