"""Activate unchanged ER policy for staging, sample it, and build local dbt marts.

No production Gold publication or writes to Silver/human history.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
from importlib.metadata import version
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import uuid

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from . import er_auto_accept as auto, google_silver as silver, human_decisions as human
from .overture_incremental import _digest

ACTIVE_VERSION = "er-effective-gold-staging-v1"
STAGING_VERSION = "canonical-gold-staging-v1"
PROJECT = Path(__file__).resolve().parents[2] / "pipelines/gold_staging"
ACCEPTED = {"AUTO_ACCEPTED", "HUMAN_ACCEPTED"}
ACTIVE_SCHEMA = auto.SCHEMA.append(pa.field("activation_scope", pa.string())).append(pa.field("identity_verification_status", pa.string())).append(pa.field("activation_evidence_hash", pa.string()))


def require(condition, message):
    if not condition:
        raise ValueError(message)


def store_id(overture_id):
    require(isinstance(overture_id, str) and bool(overture_id.strip()), "Missing canonical source anchor")
    return "store_" + uuid.uuid5(uuid.NAMESPACE_URL, "fnb-platform/overture/" + overture_id).hex


def check_hashes(hashes):
    for path, expected in hashes.items():
        require(silver.sha(path) == expected, "Pinned input/code changed: " + path)


def publish_release(output, signature, writer, *, guard=lambda: None, fault=lambda stage: None):
    """Reuse project release/checksum/lock conventions for complete staging artifacts."""
    output = Path(output).resolve()
    fingerprints = {**signature["inputs_sha256"], **signature["code_sha256"]}
    require(all(not Path(p).resolve().is_relative_to(output) and not output.is_relative_to(Path(p).resolve().parent) for p in fingerprints), "Output overlaps an input directory")
    rid = _digest(signature)[:24]
    output.mkdir(parents=True, exist_ok=True)
    with human.publication_lock(output):
        current = silver.read_active(output)
        if current:
            require(silver.verify_release(current)["signature"]["rule_version"] == signature["rule_version"], "Different dataset at publication root")
        dest = output / "releases" / rid
        if not dest.exists():
            stage = output / (".stage-" + uuid.uuid4().hex)
            stage.mkdir()
            tables, summary, extra = writer(stage, rid)
            artifacts = {}
            for name, table in tables.items():
                pq.write_table(table, stage / name, compression="zstd")
                artifacts[name] = dict(rows=table.num_rows, sha256=silver.sha(stage / name), logical_sha256=_digest(table.to_pylist()), schema=str(table.schema))
            (stage / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
            for name in ["summary.json", *extra]:
                require((stage / name).is_file(), "Incomplete staging artifact: " + name)
                artifacts[name] = dict(sha256=silver.sha(stage / name))
                if name.endswith('.parquet'):
                    extra_table=pq.read_table(stage/name)
                    artifacts[name].update(rows=extra_table.num_rows,logical_sha256=_digest(extra_table.to_pylist()),schema=str(extra_table.schema))
            (stage / "manifest.json").write_text(silver.dump(dict(release_id=rid, signature=signature, artifacts=artifacts,
                publication_kind=signature["publication_kind"], production_gold=False)), encoding="utf-8")
            silver.verify_release(stage); fault("after_stage_validation")
            check_hashes(fingerprints); guard()
            dest.parent.mkdir(exist_ok=True); os.replace(stage, dest)
            fault("after_release_rename")
        else:
            require(silver.verify_release(dest)["signature"] == signature, "Release signature mismatch")
        fault("before_pointer_replace"); check_hashes(fingerprints); guard()
        pointer = output / (".current-" + uuid.uuid4().hex)
        pointer.write_text(silver.dump(dict(release_id=rid, manifest_sha256=silver.sha(dest / "manifest.json"))), encoding="utf-8")
        os.replace(pointer, output / "current.json"); fault("after_pointer_replace")
        return dest, json.loads((dest / "summary.json").read_text(encoding="utf-8"))


def validation_sample(rows, category_rows, google_rows, *, size=48, seed=20261108, release_id):
    """One-per-stratum then proportional largest remainder; seeded hash random rank."""
    eligible = [r for r in rows if r["effective_status"] == "AUTO_ACCEPTED"]
    cat = {auto.key(r): r for r in category_rows if r["google_id_kind"] and r["overture_id"]}
    gp = {(g["google_id_kind"], g["google_identity_value"]): g for g in google_rows}
    strata = defaultdict(list)
    for row in eligible:
        groups = cat[auto.key(row)]["overture_categories"]
        group = groups[0] if len(groups) == 1 else "MULTI_CATEGORY"
        distance = row["distance_m"]
        band = "LE_10M" if distance <= 10 else "GT_10_LE_25M" if distance <= 25 else "GT_25M"
        strata[(group, band)].append(row)
    count = min(size, len(eligible))
    require(count >= len(strata), "Sample size must cover all nonempty category/distance strata")
    allocation = {k:1 for k in strata}
    remaining = count - len(strata)
    denominator = sum(len(v)-1 for v in strata.values())
    quota = {k:remaining*(len(v)-1)/denominator if denominator else 0 for k,v in strata.items()}
    for k in allocation:allocation[k] += math.floor(quota[k])
    for k in sorted(strata, key=lambda k:(-(quota[k]-math.floor(quota[k])), _digest([seed,list(k)])))[:count-sum(allocation.values())]:
        allocation[k] += 1
    selected = []
    for stratum in sorted(strata):
        population, n = len(strata[stratum]), allocation[stratum]
        ranked = sorted(strata[stratum], key=lambda r:_digest([seed,list(stratum),list(auto.key(r))]))
        for row in ranked[:n]:
            c = cat[auto.key(row)]; g = gp[auto.key(row)[1:]]
            selected.append(dict(validation_id="auto_validation_"+_digest(list(auto.key(row))),
                stratum_category=stratum[0], stratum_distance=stratum[1], stratum_population=population, stratum_sample=n,
                inclusion_probability=n/population, sample_weight=population/n, sample_seed=seed,
                overture_name=row["overture_name"], google_name=row["google_name"], overture_address=row["overture_address"], google_address=row["google_address"],
                google_maps_url=auto.review.observed_url(g.get("source_url")), overture_categories_raw=c["overture_categories_raw"], google_categories_raw=c["google_categories_raw"],
                overture_categories_normalized=silver.dump(c["overture_categories"]), google_categories_normalized=silver.dump(c["google_categories"]),
                distance_m=row["distance_m"], name_similarity=row["name_similarity"], effective_status=row["effective_status"],
                decision_source=row["decision_source"], policy_version=row["policy_version"], evidence_hash=row["evidence_hash"],
                evidence_json=row["evidence_json"], reason_codes=silver.dump(row["reason_codes"]), source_provenance_json=row["source_provenance_json"],
                **dict(zip(auto.KEY,auto.key(row))), er_release_id=row["er_release_id"], category_release_id=row["category_release_id"],
                effective_mapping_release_id=release_id, validation_schema_version="auto-accept-validation-v1",
                human_label="", reviewer_note="", reviewer=""))
    require(len(selected) == count and len({r["validation_id"] for r in selected}) == count, "Validation sample coverage/uniqueness failure")
    return selected, dict(seed=seed, population=len(eligible), sample_size=count, method="seeded-sha256-ranked-stratified-v1",
        strata=[dict(category=k[0], distance_band=k[1], population=len(strata[k]), sample=allocation[k]) for k in sorted(strata)],
        precision_estimate=None, limitations=["No human labels: precision is not estimated.",
            "Use stratum population weights, not an unweighted overall average; UNSURE is not a confirmed match/non-match.",
            "Small strata with one label cannot independently estimate within-stratum variance.",
            "Sample assesses accepted links only, not recall or unresolved/source-only POIs.",
            "Validation labels are not imported here; the existing human importer needs this validation contract registered before consuming it."])


def activate(shadow_root, output_root, *, size=48, seed=20261108, human_root=None, validation_root=Path('output/human-review/auto-accept-v1-validation'), fault=lambda stage:None):
    require(40<=size<=50,'Validation sample target must be 40–50 links')
    root = Path(shadow_root).resolve()
    require(not Path(output_root).resolve().is_relative_to(root) and not root.is_relative_to(Path(output_root).resolve()), "Separate active/shadow roots required")
    shadow = silver.read_active(root); require(shadow is not None, "Published shadow required")
    manifest = silver.verify_release(shadow)
    require(manifest["signature"]["rule_version"] == auto.VERSION, "Expected Auto-Accept V1 shadow")
    pins = manifest["signature"]["inputs_sha256"]
    def find(name):
        paths = [Path(p) for p in pins if Path(p).name == name]
        require(len(paths) == 1, "Ambiguous shadow input: " + name)
        return paths[0]
    # Reuse the exact policy loader/evaluator/publisher; do not replace ER rules.
    ai_path = next(Path(p) for p in pins if Path(p).suffix == ".csv")
    ai_audit = next(Path(p) for p in pins if Path(p).name == "audit_summary.json")
    human_root = Path(human_root or manifest["signature"].get("human_root", "output/human-decisions-v1")).resolve()
    if manifest["signature"].get("human_pointer_sha256"):
        human_paths = [Path(p) for p in pins if Path(p).name == "decision_history.parquet"]
        require(len(human_paths) == 1, "Ambiguous human history")
        human_root = human_paths[0].parents[2]
    require(not Path(output_root).resolve().is_relative_to(human_root) and not human_root.is_relative_to(Path(output_root).resolve()),'Separate activation/history roots required')
    review_root=Path(validation_root).resolve()
    require(all(not review_root.is_relative_to(p.resolve()) for p in (root,human_root,Path(output_root))), 'Review copy must be outside immutable releases/history')
    shadow, current = auto.publish(find("er_results.parquet").parents[2], find("category_audit.parquet").parents[2],
        human_root, ai_path, ai_audit, root, find("er_auto_accept_policy_v1.json"))
    manifest = silver.verify_release(shadow); pins = dict(manifest["signature"]["inputs_sha256"])
    check_hashes(pins)
    table = pq.read_table(shadow / "effective_identity_mapping.parquet")
    source_rows = table.to_pylist(); category = pq.read_table(find("category_audit.parquet")).to_pylist()
    google = pq.read_table(find("google_places.parquet"), columns=["google_id_kind","google_identity_value","source_url"]).to_pylist()
    def writer(stage, rid):
        rows = []
        for old in source_rows:
            row = dict(old, shadow_only=False, activation_scope="GOLD_STAGING_ONLY",
                identity_verification_status="HUMAN_VERIFIED_LINK" if old["effective_status"] == "HUMAN_ACCEPTED" else "MACHINE_ACCEPTED" if old["effective_status"] == "AUTO_ACCEPTED" else "UNVERIFIED",
                activation_evidence_hash=_digest(dict(policy_evidence_hash=old["evidence_hash"], shadow_release=shadow.name, scope="GOLD_STAGING_ONLY")))
            rows.append(row)
        sample, sample_info = validation_sample(rows, category, google, size=size, seed=seed, release_id=rid)
        fields = list(sample[0]) if sample else ["validation_id","human_label","reviewer_note","reviewer"]
        with (stage / "auto_accept_validation_sample.csv").open("x", encoding="utf-8-sig", newline="") as stream:
            w=csv.DictWriter(stream,fieldnames=fields);w.writeheader();w.writerows(sample)
        summary=dict(activation_scope="GOLD_STAGING_ONLY", production_gold=False, source_shadow_release=shadow.name,
            statuses=current["effective_status_counts"], validation=sample_info,
            no_google_candidate_ids=current["no_google_candidate_ids"], source_context={f:source_rows[0][f] for f in ("er_release_id","category_release_id","google_silver_release_id")})
        return {"effective_identity_mapping.parquet":pa.Table.from_pylist(rows,schema=ACTIVE_SCHEMA)}, summary, ["auto_accept_validation_sample.csv"]
    paths = [root/"current.json", shadow/"manifest.json",shadow/"effective_identity_mapping.parquet",*map(Path,pins)]
    hashes = {str(p.resolve()):silver.sha(p) for p in paths}
    hptr=human_root/"current.json"; hhash=manifest['signature']['human_pointer_sha256']
    signature=dict(rule_version=ACTIVE_VERSION, publication_kind="ACTIVE_EFFECTIVE_MAPPING_FOR_GOLD_STAGING", inputs_sha256=hashes,
        code_sha256={str(Path(__file__).resolve()):silver.sha(__file__)}, source_shadow_release=shadow.name, sample_size=size, sample_seed=seed,
        human_root=str(human_root),human_pointer_sha256=hhash)
    dest,summary=publish_release(output_root,signature,writer,guard=lambda:require((silver.sha(hptr) if hptr.exists() else None)==hhash,"Human state changed during activation"),fault=fault)
    # Reviewers edit a separate copy, not an immutable release artifact.
    review_dir=review_root/dest.name
    require(not review_dir.is_relative_to(Path(output_root).resolve()) and not review_dir.is_relative_to(root) and not review_dir.is_relative_to(human_root),'Review copy must be separate from immutable releases/history')
    review_dir.mkdir(parents=True,exist_ok=True)
    review_csv=review_dir/'auto_accept_validation_sample.csv'
    try:
        with review_csv.open('xb') as stream:stream.write((dest/'auto_accept_validation_sample.csv').read_bytes())
    except FileExistsError:pass
    return dest,dict(summary,validation_csv_path=str(review_csv),pristine_validation_csv_sha256=silver.sha(dest/'auto_accept_validation_sample.csv'))


def pin_inputs(active_root, category_root):
    ar,cr=Path(active_root).resolve(),Path(category_root).resolve()
    a,c=silver.read_active(ar),silver.read_active(cr)
    require(a is not None and c is not None,"Active mapping and category releases required")
    am,cm=silver.verify_release(a),silver.verify_release(c)
    require(am["publication_kind"]=="ACTIVE_EFFECTIVE_MAPPING_FOR_GOLD_STAGING" and am["signature"]["rule_version"]==ACTIVE_VERSION,"Staging requires active, not shadow mapping")
    require(cm["signature"]["rule_version"]=="category-quality-v1","Expected Category Quality V1")
    pins=cm["signature"]["inputs_sha256"]
    def one(name):
        values=[Path(p) for p in pins if Path(p).name==name]
        require(len(values)==1,"Missing/ambiguous pinned input: "+name)
        require(silver.sha(values[0])==pins[str(values[0])],"Category source checksum mismatch")
        return values[0]
    paths=dict(overture_path=one("current_state.parquet"),google_path=one("google_places.parquet"),
        category_normalization_path=c/"category_normalization.parquet",category_audit_path=c/"category_audit.parquet",
        effective_mapping_path=a/"effective_identity_mapping.parquet")
    rows=pq.read_table(paths["effective_mapping_path"]).to_pylist()
    require(all(r["category_release_id"]==c.name and not r["shadow_only"] and r["activation_scope"]=="GOLD_STAGING_ONLY" for r in rows),"Mapping/category release or activation mismatch")
    require(am['signature']['inputs_sha256'].get(str((c/'manifest.json').resolve()))==silver.sha(c/'manifest.json'),'Active mapping/category manifest lineage mismatch')
    # Overture is pinned transitively by this exact Category Quality manifest;
    # the ER shadow input projection itself does not reread Overture Silver.
    for name in ("google_path","category_audit_path","category_normalization_path"):
        path=paths[name]
        require(am["signature"]["inputs_sha256"].get(str(path.resolve()))==silver.sha(path),"Active mapping input lineage mismatch: "+name)
    hashes={str(p.resolve()):silver.sha(p) for p in [*paths.values(),a/"manifest.json",c/"manifest.json",ar/"current.json",cr/"current.json"]}
    human_pointer=Path(am['signature']['human_root'])/'current.json'
    expected_human=am['signature']['human_pointer_sha256']
    def guard_human():
        require((silver.sha(human_pointer) if human_pointer.exists() else None)==expected_human,'Human decisions changed; reactivate mapping before staging')
    guard_human()
    if human_pointer.exists():hashes[str(human_pointer.resolve())]=silver.sha(human_pointer)
    return paths,hashes,rows,guard_human


def dbt_build(stage, paths, registry):
    registry_path=stage/"store_identity_registry.parquet";pq.write_table(registry,registry_path)
    variables={k:p.resolve().as_posix() for k,p in paths.items()}
    variables["store_registry_path"]=registry_path.resolve().as_posix()
    variables["input_refs_json"]=silver.dump({k:dict(path=p.resolve().as_posix(),sha256=silver.sha(p)) for k,p in paths.items()})
    env=dict(os.environ,FNB_DBT_DATABASE=str((stage/"staging.duckdb").resolve()),DBT_SEND_ANONYMOUS_USAGE_STATS="false",DBT_PARTIAL_PARSE="false",PYTHONDONTWRITEBYTECODE="1")
    args=[sys.executable,"-B","-c","from dbt.cli.main import cli; cli()","build","--project-dir",str(PROJECT),"--profiles-dir",str(PROJECT),
        "--target-path",str((stage/"dbt-target").resolve()),"--log-path",str((stage/"dbt-logs").resolve()),"--vars",json.dumps(variables),"--no-use-colors"]
    result=subprocess.run(args,env=env,capture_output=True,text=True,encoding="utf-8",errors="replace",timeout=120)
    (stage/"dbt_console.log").write_text(result.stdout+result.stderr,encoding="utf-8")
    require(result.returncode==0,"dbt build failed; diagnostics retained at "+str(stage/"dbt_console.log")+": "+(result.stdout+result.stderr)[-2200:])
    results=json.loads((stage/"dbt-target/run_results.json").read_text(encoding="utf-8"))
    require(all(r["status"] in {"success","pass"} for r in results["results"]),"dbt quality gate failed")
    con=duckdb.connect(str(stage/"staging.duckdb"),read_only=True)
    try:
        tables={name+".parquet":con.execute('select * from gold_staging.'+name+' order by '+order).to_arrow_table()
            for name,order in (("dim_store","store_id"),("store_source_mapping","source,source_id_kind,source_id"),("source_accounting","source,source_id_kind,source_id"),("link_accounting","overture_id,google_id_kind,google_identity_value"))}
    finally:con.close()
    return tables,dict(models=sum(r["unique_id"].startswith("model.") for r in results["results"]),tests=sum(r["unique_id"].startswith("test.") for r in results["results"]),failures=0)


def validate_staging(tables, registry, effective, google_ids):
    stores=tables["dim_store.parquet"].to_pylist();mapping=tables["store_source_mapping.parquet"].to_pylist()
    accounting=tables["source_accounting.parquet"].to_pylist();links=tables["link_accounting.parquet"].to_pylist()
    anchors={r["overture_id"]:r["store_id"] for r in registry.to_pylist()}
    accepted=[r for r in effective if r["effective_status"] in ACCEPTED]
    require(len(stores)==len(anchors) and {r["overture_id"]:r["store_id"] for r in stores}==anchors,"Canonical anchor coverage/identity mismatch")
    require(len({r["store_id"] for r in stores})==len(stores),"Duplicate canonical store ID")
    require(len(mapping)==len(anchors)+len(accepted) and all(r["store_id"] in anchors.values() for r in mapping),"Mapping coverage/referential integrity")
    require({r["source_id"] for r in mapping if r["source"]=="overture"}==set(anchors),"Lost Overture source IDs")
    assigned={(r["source_id_kind"],r["source_id"]):r["store_id"] for r in mapping if r["source"]=="google"}
    require(len(assigned)==len(accepted) and assigned=={auto.key(r)[1:]:anchors[r["overture_id"]] for r in accepted},"Unauthorized Google assignment/merge")
    require({(r["source_id_kind"],r["source_id"]) for r in accounting if r["source"]=="google"}==google_ids and len(accounting)==len(anchors)+len(google_ids),"Google/source accounting loss")
    require({auto.key(r) for r in links}=={auto.key(r) for r in effective} and len(links)==len(effective),"Link accounting loss")
    require(all(r["merged_store_id"] is None for r in links if r["effective_status"] not in ACCEPTED),"Unresolved link merged")
    require(all(r["category_validation_status"] in {"PENDING","PROVISIONAL_SOURCE_AGREEMENT","SOURCE_AGREEMENT_HUMAN_LINK"} and not(r["category_relation"]=="CATEGORY_CONFLICT" and r["canonical_category"]!="unknown") for r in stores),"Unresolved category published as verified")
    return dict(accepted_mapping_count=len(accepted),canonical_store_count=len(stores),source_only_store_count=sum(r["google_identity_value"] is None for r in stores),
        unresolved_mapping_count=sum(r["effective_status"] in {"PENDING","CONFLICT"} for r in links),
        unresolved_overture_ids=len({r["overture_id"] for r in links if r["effective_status"] in {"PENDING","CONFLICT"}}),
        no_google_candidate_ids=sum(r["integration_status"]=="NO_GOOGLE_CANDIDATE" for r in stores),
        unassigned_google_identities=sum(r["source"]=="google" and not r["in_catalog"] for r in accounting),
        source_mapping_rows=len(mapping),source_accounting_rows=len(accounting),
        category_validation_counts=dict(Counter(r["category_validation_status"] for r in stores)),production_gold=False)


def stage_gold(active_root,category_root,output_root,*,fault=lambda stage:None):
    output=Path(output_root).resolve()
    for root in (active_root,category_root):
        root=Path(root).resolve();require(not output.is_relative_to(root) and not root.is_relative_to(output),"Separate staging/source roots required")
    paths,hashes,effective,guard=pin_inputs(active_root,category_root)
    norm=pq.read_table(paths["category_normalization_path"],columns=["source","source_id","baseline_fnb_candidate"]).to_pylist()
    ids=sorted(r["source_id"] for r in norm if r["source"]=="overture" and r["baseline_fnb_candidate"])
    require(len(set(ids))==len(ids),"Duplicate candidate ID")
    registry=pa.Table.from_pylist([dict(overture_id=k,store_id=store_id(k)) for k in ids],schema=pa.schema([("overture_id",pa.string()),("store_id",pa.string())]))
    google_ids={(r["google_id_kind"],r["google_identity_value"]) for r in pq.read_table(paths["google_path"],columns=["google_id_kind","google_identity_value"]).to_pylist()}
    project_files=[p for p in PROJECT.rglob("*") if p.is_file() and p.suffix in {"sql","yml"}]
    code={str(p.resolve()):silver.sha(p) for p in [Path(__file__),*project_files]}
    signature=dict(rule_version=STAGING_VERSION,publication_kind="CANONICAL_GOLD_STAGING_NOT_PRODUCTION",inputs_sha256=hashes,code_sha256=code,
        runtime_versions={p:version(p) for p in ("dbt-core","dbt-duckdb","duckdb","pyarrow")},registry_logical_sha256=_digest(registry.to_pylist()))
    def writer(stage,rid):
        tables,dbt=dbt_build(stage,paths,registry)
        metrics=validate_staging(tables,registry,effective,google_ids)
        summary=dict(metrics,dbt_build=dbt,release_id=rid,identity_grain="One Overture source anchor, not a verified physical-store count",
            blockers=["The 48-link validation sample is unlabelled; precision is not yet estimated.","Unresolved links and source-only records remain unverified.","ER-B and human resolution of shared Google identities remain deferred.","Category disagreement/missing evidence remains PENDING; no category is independently verified.","Production Gold publication is not enabled."])
        return tables,summary,["store_identity_registry.parquet","staging.duckdb","dbt_console.log","dbt-target/run_results.json","dbt-target/manifest.json"]
    return publish_release(output,signature,writer,guard=guard,fault=fault)


def main():
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest="command",required=True)
    a=sub.add_parser("activate");a.add_argument("--shadow-root",type=Path,required=True);a.add_argument("--output-root",type=Path,required=True)
    a.add_argument("--sample-size",type=int,default=48);a.add_argument("--sample-seed",type=int,default=20261108)
    a.add_argument('--human-root',type=Path,default=Path('output/human-decisions-v1'))
    a.add_argument('--validation-root',type=Path,default=Path('output/human-review/auto-accept-v1-validation'))
    b=sub.add_parser("build");b.add_argument("--active-root",type=Path,required=True);b.add_argument("--category-root",type=Path,required=True);b.add_argument("--output-root",type=Path,required=True)
    args=parser.parse_args()
    if args.command=="activate":path,summary=activate(args.shadow_root,args.output_root,size=args.sample_size,seed=args.sample_seed,human_root=args.human_root,validation_root=args.validation_root)
    else:path,summary=stage_gold(args.active_root,args.category_root,args.output_root)
    print(silver.dump(dict(release=str(path),summary=summary)))


if __name__=="__main__":main()
