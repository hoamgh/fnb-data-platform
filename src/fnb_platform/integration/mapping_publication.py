"""Publish canonical V3 evidence and current human decisions for Gold; no AI gate."""
import argparse
from collections import Counter
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from . import er_auto_accept as legacy, er_category_review_export as review
from . import human_decisions as human, matching_policy, matching_evidence
from ..google import places_silver as silver
from ..gold import staging
from .. import publication
from ..overture_incremental import _digest

VERSION = 'canonical-er-mapping-v1'


decision = matching_policy.final_decision


def evaluate(data, google, history, provenance):
    # Reuse the existing human precedence/accounting implementation. The strict
    # evidence gates have one owner: matching_policy. No AI candidate entry.
    table, summary = legacy.build(data, google, history, {},
        matching_policy.EVIDENCE_REQUIREMENTS, provenance)
    rows = [dict(r, shadow_only=False, activation_scope='GOLD_STAGING_ONLY',
        identity_verification_status='HUMAN_VERIFIED_LINK' if r['effective_status']=='HUMAN_ACCEPTED'
        else 'MACHINE_ACCEPTED' if r['effective_status']=='AUTO_ACCEPTED' else 'UNVERIFIED',
        activation_evidence_hash=_digest(['canonical-mapping',r['evidence_hash']]),
        final_decision=decision(r), rule_version=matching_policy.VERSION,
        reason=r['reason'].replace('shadow=', 'mapping=')) for r in table.to_pylist()]
    summary.update(mode='CANONICAL_MAPPING_NO_AI', production_gold=False,
        activation_scope='GOLD_STAGING_ONLY', final_decisions=dict(Counter(r['final_decision'] for r in rows)))
    summary['limitations'] = ['Unresolved links remain separate.',
        'Machine acceptance is not human verification or measured precision.']
    schema=staging.ACTIVE_SCHEMA.append(pa.field('final_decision',pa.string()))
    return pa.Table.from_pylist(rows,schema=schema), summary


def publish(er_root, category_root, human_root, output_root, *, fault=lambda stage:None):
    data, hashes, roots = review.load(er_root, category_root)
    # Validate transitive source bytes, not just the immediate manifest hashes.
    for root in (Path(er_root),Path(category_root)):
        release=silver.read_active(root)
        pins=silver.verify_release(release)['signature']['inputs_sha256']
        publication.check_hashes(pins)
        publication.require(all(p not in hashes or hashes[p]==h for p,h in pins.items()),
            'Inconsistent upstream fingerprints')
        hashes.update(pins)
    human_root, output = Path(human_root).resolve(), Path(output_root).resolve()
    publication.require(all(not output.is_relative_to(r) and not r.is_relative_to(output)
        for r in [*roots,human_root]),'Separate mapping/source/history roots required')
    pointer=human_root/'current.json'
    before=silver.sha(pointer) if pointer.exists() else None
    active, history=human.read_history(human_root)
    if active:
        for p in (pointer,active/'manifest.json',active/'decision_history.parquet'):
            hashes[str(p.resolve())]=silver.sha(p)
    google=pq.read_table(data[4]['refs']['google_places']['path']).to_pylist()
    provenance=dict(input_hashes=hashes,human_release=str(active) if active else None,human_history_unchanged=True)
    table,summary=evaluate(data,google,history,provenance)
    code=[Path(m.__file__).resolve() for m in
        (legacy,review,human,matching_policy,matching_evidence,staging,publication,silver)] + [Path(__file__).resolve()]
    signature=dict(rule_version=staging.ACTIVE_VERSION,mapping_version=VERSION,
        publication_kind='ACTIVE_EFFECTIVE_MAPPING_FOR_GOLD_STAGING',
        inputs_sha256=hashes,code_sha256={str(p):silver.sha(p) for p in code},
        human_root=str(human_root),human_pointer_sha256=before,
        source_context={k:data[4][k] for k in ('er_release_id','category_release_id','google_silver_release_id')},
        logical_sha256=_digest(table.to_pylist()))
    def guard():
        publication.check_hashes({**hashes,**signature['code_sha256']})
        publication.require((silver.sha(pointer) if pointer.exists() else None)==before,
            'Human state changed; republish mapping')
    guard()
    return staging.publish_release(output,signature,
        lambda stage,rid:({'effective_identity_mapping.parquet':table},summary,[]),guard=guard,fault=fault)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('er-root','category-root','human-root','output-root'):
        parser.add_argument('--'+name,type=Path,required=True)
    args=parser.parse_args()
    path,summary=publish(args.er_root,args.category_root,args.human_root,args.output_root)
    print(silver.dump(dict(release=str(path),summary=summary)))


if __name__=='__main__':
    main()
