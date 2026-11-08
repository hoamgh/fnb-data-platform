"""Offline experimental Overture/Google ER proposals; never final identity decisions."""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import uuid
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from . import google_silver as silver
from .overture_incremental import _digest

VERSION = "ov-google-er-proposal-v1"
DEFAULTS = dict(match_name_min=0.90, match_distance_max_m=100.0,
                nonmatch_name_max=0.60, nonmatch_distance_min_m=500.0)
KEY = ("overture_id", "google_id_kind", "google_identity_value")
QUEUE = ["overture_name", "google_name", "overture_address", "google_address",
         "er_status", "classification", "flag_code", "reason", "recommended_action",
         *KEY, "evidence_json", "observation_ref", "rule_version"]
SCHEMA = pa.schema([(k, pa.string()) for k in [*KEY, "overture_name", "google_name",
    "overture_address", "google_address", "classification", "er_status", "reason",
    "recommended_action", "evidence_json", "observation_ref", "rule_version", "flags_json"]]
    + [("name_similarity", pa.float64()), ("distance_m", pa.float64())])


def name_similarity(a, b):
    if not a or not b:
        return None
    return (SequenceMatcher(None, a, b, autojunk=False).ratio()
            + SequenceMatcher(None, b, a, autojunk=False).ratio()) / 2


def valid_coordinates(row):
    a, b = row.get("latitude"), row.get("longitude")
    return (all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
                for v in (a, b)) and -90 <= a <= 90 and -180 <= b <= 180)


def distance_m(a, b):
    if not valid_coordinates(a) or not valid_coordinates(b):
        return None
    x, y = math.radians(a["latitude"]), math.radians(b["latitude"])
    z = math.sin((y-x)/2)**2 + math.cos(x)*math.cos(y)*math.sin(math.radians(b["longitude"]-a["longitude"])/2)**2
    return 6371008.8 * 2 * math.asin(min(1, math.sqrt(z)))


def classify(score, distance, clear_conflict, cautious, thresholds):
    if score is None or distance is None:
        return "INSUFFICIENT_DATA"
    if cautious:
        return "REVIEW"
    if (clear_conflict and score < thresholds["nonmatch_name_max"]
            and distance > thresholds["nonmatch_distance_min_m"]):
        return "NON_MATCH_CANDIDATE"
    if (not clear_conflict and score >= thresholds["match_name_min"]
            and distance <= thresholds["match_distance_max_m"]):
        return "MATCH_CANDIDATE"
    return "REVIEW"


def distribution(values):
    values = sorted(v for v in values if v is not None)
    return dict(available=len(values), quantiles={str(q): values[round((len(values)-1)*q)]
        for q in (0, .1, .25, .5, .75, .9, .95, 1)} if values else {})


def build(ov_features, gg_features, pairs, overture, places, links, refs, thresholds=None, historical_details=(), version="v1"):
    if version not in {"v1", "v2", "v3"}:
        raise ValueError("Unknown ER version")
    from . import entity_resolution_v2 as v2
    from . import entity_resolution_v3 as v3
    policy = v3 if version == "v3" else v2
    rule_version = VERSION if version == "v1" else policy.VERSION
    thresholds = dict(DEFAULTS if thresholds is None else thresholds)
    if version != "v1":
        thresholds = dict(policy.DEFAULTS)
        historical_details = sorted(historical_details, key=silver.dump)
    def index(rows, fields):
        out = {tuple(r[f] for f in fields): r for r in rows}
        if len(out) != len(rows):
            raise ValueError("Duplicate input key")
        return out
    ov = index(ov_features, ["overture_id"])
    gg = index(gg_features, KEY[1:])
    rawov = index(overture, ["source_id"])
    rawgg = index(places, KEY[1:])
    li = index(links, KEY)
    pi = index(pairs, KEY)
    if set(li) != set(pi):
        raise ValueError("M1/M1.5 relationship keys differ")
    if any(r.get("er_status", "ER_PENDING") not in (None, "ER_PENDING") for r in links):
        raise ValueError("Independent ER decision present; proposal runner must not overwrite it")
    by_google, by_source = defaultdict(list), defaultdict(list)
    for key in sorted(pi):
        a, b = ov[(key[0],)], gg[key[1:]]
        related = dict(zip(KEY, key), overture_name=a["name_raw"], overture_address=a["address_raw"],
            google_name=b["name_raw"], google_address=b["address_raw"],
            observed_at=li[key]["observed_at"], source_run=li[key]["source_run"],
            selection_policies=li[key]["selection_policies"])
        by_google[key[1:]].append(related)
        by_source[key[0]].append(related)
    results, queue = [], []
    for key in sorted(pi):
        p, link = pi[key], li[key]
        a, b, x, y = ov[(key[0],)], gg[key[1:]], rawov[(key[0],)], rawgg[key[1:]]
        score, dist = name_similarity(a["name_comparison"], b["name_comparison"]), distance_m(x, y)
        parsed = a["parse_status"] == b["parse_status"] == "PARSED"
        street_conflict = parsed and p["street_component_relation"] == "DIFFERENT_LITERAL"
        house_conflict = parsed and p["house_component_relation"] in {
            "DIFFERENT_LITERAL", "DISJOINT_NUMERIC_RANGES", "RANGE_EXCLUDES_LITERAL"}
        shared, multiple = len(by_google[key[1:]]) > 1, len(by_source[key[0]]) > 1
        identity_conflicts = sorted(set(y["conflict_fields"]) & {"name_normalized", "address_normalized", "coordinates"})
        cautious = shared or multiple or bool(identity_conflicts)
        status = classify(score, dist, street_conflict or house_conflict, cautious, thresholds) if version == "v1" else None
        v2_context = None
        if version != "v1":
            status, v2_context = policy.evaluate(a, b, p, score, dist, shared, multiple, identity_conflicts)
            if version == "v3":
                street_conflict = v2_context["fully_parsed_street_difference"]
                house_conflict = v2_context["fully_parsed_house_difference"]
        source_ref = dict(m15_pair=dict(path=refs["pairs"], key=dict(zip(KEY, key))),
            m1_link=json.loads(p["m1_source_link_ref_json"]),
            historical_observation_refs=p["historical_source_observation_refs"])
        evidence = dict(key=dict(zip(KEY, key)),
            overture=dict(name_raw=a["name_raw"], name_comparison=a["name_comparison"],
                name_source_ref=json.loads(a["name_source_ref_json"]),
                latitude=x["latitude"], longitude=x["longitude"], coordinate_ref=dict(path=refs["overture"], source_id=key[0]),
                source_release=x["source_release"]),
            google=dict(name_raw=b["name_raw"], name_comparison=b["name_comparison"],
                name_source_ref=json.loads(b["name_source_ref_json"]), latitude=y["latitude"], longitude=y["longitude"],
                field_provenance=json.loads(y["field_provenance_json"]), fallback_fields=y["fallback_fields"],
                observed_at=y["observed_at"], metadata_status=y["metadata_status"],
                conflicting_values=json.loads(y["conflicting_values_json"]), metadata_ref=dict(path=refs["places"], google_key=key[1:])),
            address=json.loads(p["address_evidence_json"]),
            comparisons=dict(name_similarity=score, name_method="mean_bidirectional_SequenceMatcher_autojunk_false",
                distance_m=dist, distance_method="haversine_sphere_radius_6371008.8_m",
                coordinate_status=dict(overture="VALID" if valid_coordinates(x) else "MISSING_OR_INVALID",
                    google="VALID" if valid_coordinates(y) else "MISSING_OR_INVALID"),
                clear_street_conflict=street_conflict, clear_house_conflict=house_conflict),
            related_google_links=by_google[key[1:]], related_source_candidates=by_source[key[0]],
            historical_m1_flags=p["historical_m1_dq_flags"], historical_flag_usage=p["historical_flag_usage"],
            historical_dq_details=[d for d in historical_details if
                (not d["overture_id"] or d["overture_id"] == key[0]) and
                (d["google_id_kind"], d["google_identity_value"]) == key[1:]],
            source_refs=source_ref, input_refs=refs, thresholds=thresholds, rule_version=rule_version,
            normalization_version=p["normalization_version"], address_rule_version=p["address_rule_version"])
        if v2_context is not None:
            evidence["comparisons"][version] = v2_context
        ej, observation = silver.dump(evidence), silver.dump(source_ref)
        flags = []
        def flag(code, reason, action, kind="ER_EVIDENCE"):
            flags.append(dict(flag_code=code, reason=reason, evidence_json=ej,
                observation_ref=observation, rule_version=rule_version, recommended_action=action, flag_kind=kind))
        action = "Đối chiếu tên, địa chỉ và snapshot được dẫn; ghi nhận quyết định thủ công độc lập trước khi xác nhận thực thể."
        if score is None or dist is None:
            flag("MISSING_COMPARISON_EVIDENCE", f"Tên: {a['name_raw']!r} / {b['name_raw']!r}; similarity={score}; distance_m={dist}; kiểm tra coordinate_status và metadata_status trong evidence.",
                "Kiểm tra metadata/coordinate của hai nguồn trong snapshot được dẫn; giữ chưa xác minh nếu vẫn thiếu.", "DQ")
        if shared:
            flag("GOOGLE_ID_SHARED_BY_OVERTURE_IDS", f"Google {key[2]} được {len(by_google[key[1:]])} Overture IDs chọn: {[r['overture_id'] for r in by_google[key[1:]]]}; chưa chứng minh các POI là cùng cửa hàng.", action, "DQ")
        if multiple:
            flag("OVERTURE_MULTIPLE_GOOGLE_CANDIDATES", f"Overture {key[0]} có các Google candidates {[r['google_identity_value'] for r in by_source[key[0]]]}; cần phân biệt từng chi nhánh.", action, "DQ")
        if identity_conflicts:
            flag("CONFLICTING_METADATA", f"Google {key[2]} có nhiều giá trị non-null cho {identity_conflicts}; giá trị và snapshot nằm trong conflicting_values.", action, "DQ")
        if street_conflict:
            reason = f"Hai địa chỉ PARSED có đường khác literal: {a['street_comparison']!r} / {b['street_comparison']!r}; raw {a['address_raw']!r} / {b['address_raw']!r}."
            if version == "v3":
                reason = f"V3 so sánh đường quan sát được: {a['street_comparison']!r} / {b['street_comparison']!r}; raw {a['address_raw']!r} / {b['address_raw']!r}; parse={a['parse_status']}/{b['parse_status']}; xem street_state trong evidence."
            flag("CURRENT_PARSED_STREET_DIFFERENCE", reason, action)
        if house_conflict:
            reason = f"Hai địa chỉ PARSED có house relation={p['house_component_relation']}: {a['address_raw']!r} / {b['address_raw']!r}."
            if version == "v3":
                reason = f"V3 so sánh số nhà từ raw/component: {a['address_raw']!r} / {b['address_raw']!r}; parse={a['parse_status']}/{b['parse_status']}; xem house_state và recovery trong evidence; M1.5 relation={p['house_component_relation']}."
            flag("CURRENT_PARSED_HOUSE_DIFFERENCE", reason, action)
        metric_reason = f"Tên {a['name_raw']!r} / {b['name_raw']!r}: similarity={score}; distance_m={dist}; classification={status}; clear_address_conflict={street_conflict or house_conflict}."
        if v2_context is not None:
            metric_reason += f" {version.upper()} rule={v2_context['applied_rule']}; token_dice={v2_context['name_features']['token_dice']}; containment={v2_context['name_features']['token_containment']}; strong_address={v2_context['strong_address_agreement']}; house={p['house_component_relation']}; street={a['street_comparison']!r}/{b['street_comparison']!r}; parse={a['parse_status']}/{b['parse_status']}; branch_warnings={v2_context['name_features']['branch_warning_codes']}; review_reasons={v2_context['review_reason_codes']}."
            if version == "v3":
                metric_reason += " V3 actual support/conflict/unknown features=" + silver.dump(dict(signals=v2_context["signals"], address=v2_context["address_features_v3"], name=v2_context["name_features"]))
            for code in v2_context["review_reason_codes"]:
                flag(code, metric_reason, action)
        if not flags or status == "REVIEW":
            flag("COMPARISON_PROPOSAL", metric_reason, action)
        result = dict(zip(KEY, key), overture_name=a["name_raw"], google_name=b["name_raw"],
            overture_address=a["address_raw"], google_address=b["address_raw"], classification=status,
            er_status="ER_PENDING", reason=metric_reason + " " + " ".join(f["reason"] for f in flags),
            recommended_action=action, evidence_json=ej, observation_ref=observation,
            rule_version=rule_version, flags_json=silver.dump(flags), name_similarity=score, distance_m=dist)
        results.append(result)
        if status != "MATCH_CANDIDATE" or version != "v1" and shared:
            for f in flags:
                queue.append({**{k: result[k] for k in QUEUE if k in result}, **{k: f[k] for k in QUEUE if k in f}})
    report = dict(rule_version=rule_version, thresholds=thresholds, rows=len(results),
        classification_counts=dict(sorted(Counter(r["classification"] for r in results).items())),
        name_similarity=distribution([r["name_similarity"] for r in results]),
        distance_m=distribution([r["distance_m"] for r in results]), review_queue_rows=len(queue),
        review_queue_links=len({tuple(r[k] for k in KEY) for r in queue}),
        shared_google_links=sum(len(by_google[k[1:]]) > 1 for k in pi),
        multiple_candidate_links=sum(len(by_source[k[0]]) > 1 for k in pi),
        calibration="Exploratory only, no ground-truth labels: exact-name-heavy data; P90 distance 83.31m; 100m surrounds dense core. 500m separates long tail; nonmatch additionally requires name<0.60 and fully PARSED address difference. Short/extended names stay REVIEW.",
        anomalies=sorted([dict(overture_name=r["overture_name"], google_name=r["google_name"],
            overture_address=r["overture_address"], google_address=r["google_address"],
            distance_m=r["distance_m"], name_similarity=r["name_similarity"], classification=r["classification"])
            for r in results if r["distance_m"] is not None], key=lambda r:-r["distance_m"])[:10])
    if version != "v1":
        report["calibration"] = "Exploratory V2, no labels: preserve 100m core (P90 83m); 25m single-token anchor requires exact parsed address; 200m combined house+street contradictions, 500m house contradiction, 2000m corroborated reliable street difference. Partial components alone never establish NON_MATCH. Name extension requires exact token containment or Dice>=0.85 plus parsed address agreement; tokens/branch evidence retained."
        report["review_reason_unique_link_counts"] = dict(sorted(Counter(code for r in results if r["classification"] == "REVIEW"
            for code in json.loads(r["evidence_json"])["comparisons"][version]["review_reason_codes"]).items()))
        report["review_reason_counts_overlap"] = True
        report["shared_group_outcomes"] = [{"google_key": list(k), "links": [dict(zip(KEY, key), classification=next(r["classification"] for r in results if tuple(r[z] for z in KEY)==key))
            for key in sorted(pi) if key[1:]==k]} for k in sorted(by_google) if len(by_google[k])>1]
        if version == "v3":
            report["calibration"] = "Experimental V3: unchanged V2 distance bands; observable address equality, conservative explicit component recovery, symmetric token/core comparison and one-edit alignment. Added methods require specific address and near coordinates; branch and numeric contradictions are not overridden by closeness. No ground-truth performance claims."
    return pa.Table.from_pylist(results, schema=SCHEMA), queue, report


def publish(comparison_root, output_root, *, fault=lambda stage: None, version="v1", baseline_root=None):
    from . import entity_resolution_v2 as v2
    from . import entity_resolution_v3 as v3
    policy = v3 if version == "v3" else v2
    rule_version = VERSION if version == "v1" else policy.VERSION
    root, out = Path(comparison_root).resolve(), Path(output_root).resolve()
    if baseline_root is not None and (out.is_relative_to(Path(baseline_root).resolve()) or Path(baseline_root).resolve().is_relative_to(out)):
        raise ValueError("V2 output must be separate from V1 baseline")
    if (out/"current.json").exists():
        active = silver.read_active(out)
        if silver.verify_release(active)["signature"]["rule_version"] != rule_version:
            raise ValueError("Cannot replace a different ER version")
    c = silver.read_active(root)
    if c is None or out.is_relative_to(root) or root.is_relative_to(out):
        raise ValueError("Separate comparison input/output required")
    cm = json.loads((c/"manifest.json").read_text(encoding="utf-8"))
    hashes = dict(cm["signature"]["inputs_sha256"])
    hashes.update({str(root/"current.json"): silver.sha(root/"current.json"), str(c/"manifest.json"):silver.sha(c/"manifest.json")})
    ovf, ggf, pairs = [pq.read_table(c/"comparison"/f).to_pylist() for f in
        ("overture_features.parquet", "google_features.parquet", "address_pair_evidence.parquet")]
    ovpath = Path(json.loads(ovf[0]["name_source_ref_json"])["parquet_path"])
    lp = Path(json.loads(pairs[0]["m1_source_link_ref_json"])["parquet_path"])
    m = lp.parent.parent
    silver.verify_release(m)
    if out.is_relative_to(m.parent.parent) or m.parent.parent.is_relative_to(out) or out.is_relative_to(ovpath.parent):
        raise ValueError("Output overlaps Silver inputs")
    refs = dict(overture=str(ovpath), places=str(m/"silver/google_places.parquet"),
        links=str(lp), pairs=str(c/"comparison/address_pair_evidence.parquet"))
    for p in [*(c/"comparison").glob('*.parquet'), m/"manifest.json", m/"reports/google_silver_dq_summary.json", m/"reports/google_silver_dq_details.csv"]:
        hashes[str(p)] = silver.sha(p)
    # Validate every pinned raw/field provenance hash before evaluating evidence.
    for p, digest in hashes.items():
        if silver.sha(p) != digest:
            raise ValueError("Input provenance hash mismatch: " + p)
    with (m/"reports/google_silver_dq_details.csv").open(encoding="utf-8", newline="") as stream:
        details = list(csv.DictReader(stream))
    table, queue, report = build(ovf, ggf, pairs, pq.read_table(ovpath).to_pylist(),
        pq.read_table(refs["places"]).to_pylist(), pq.read_table(lp).to_pylist(), refs, historical_details=details, version=version)
    if baseline_root is not None:
        baseline_root = Path(baseline_root).resolve()
        baseline = silver.read_active(baseline_root)
        bm = silver.verify_release(baseline)
        expected_baseline = v2.VERSION if version == "v3" else VERSION
        if bm["signature"]["rule_version"] != expected_baseline:
            raise ValueError("Unexpected baseline rule version")
        # Pin the same M1.5 pair input, not merely a coincidentally equal row count.
        if bm["signature"]["inputs_sha256"].get(refs["pairs"]) != hashes[refs["pairs"]]:
            raise ValueError("Baseline uses different comparison inputs")
        for p in [baseline_root/"current.json", baseline/"manifest.json", baseline/"er_results.parquet"]:
            hashes[str(p)] = silver.sha(p)
        before = {tuple(r[k] for k in KEY):r for r in pq.read_table(baseline/"er_results.parquet").to_pylist()}
        after = table.to_pylist()
        if set(before) != {tuple(r[k] for k in KEY) for r in after}:
            raise ValueError("Baseline key mismatch")
        baseline_version = "v2" if version == "v3" else "v1"
        report[baseline_version+"_to_"+version+"_transitions"] = dict(sorted(Counter(before[tuple(r[k] for k in KEY)]["classification"]+" -> "+r["classification"] for r in after).items()))
        report["changed_links"] = [dict(zip(KEY, key), overture_name=r["overture_name"],google_name=r["google_name"],
            overture_address=r["overture_address"], google_address=r["google_address"],
            **{baseline_version+"_classification":before[key]["classification"],version+"_classification":r["classification"]},
            name_similarity=r["name_similarity"],distance_m=r["distance_m"], reason=r["reason"])
            for r in after for key in [tuple(r[k] for k in KEY)] if before[key]["classification"] != r["classification"]]
        if version != "v1":
            new_matches = [r for r in after if r["classification"] == "MATCH_CANDIDATE" and before[tuple(r[k] for k in KEY)]["classification"] != "MATCH_CANDIDATE"]
            contexts = [json.loads(r["evidence_json"])["comparisons"][version] for r in new_matches]
            report["new_match_audit"] = dict(links=len(new_matches),
                strong_parsed_address_agreement=sum(v["strong_address_agreement"] for v in contexts),
                branch_warning_links=sum(bool(v["name_features"]["branch_warning_codes"]) for v in contexts),
                multiple_candidate_links=sum(v["multiple_candidates"] for v in contexts),
                shared_google_links=sum(v["shared_google_identity"] for v in contexts),
                caveat="Automated evidence consistency inspection only, not human verified identity or measured precision.")
            if version == "v3":
                report["new_match_audit"]["strong_address_agreement"] = report["new_match_audit"].pop("strong_parsed_address_agreement")
                review_before=[r for r in after if before[tuple(r[k] for k in KEY)]["classification"]=="REVIEW"]
                report["all_baseline_review_audit"]=[dict(zip(KEY,tuple(r[k] for k in KEY)),overture_name=r["overture_name"],google_name=r["google_name"],classification=r["classification"],reason=r["reason"]) for r in review_before]
                report["baseline_review_outcomes"] = dict(sorted(Counter(r["classification"] for r in review_before).items()))
                report["baseline_review_reason_unique_link_counts"] = dict(sorted(Counter(code for r in before.values() if r["classification"]=="REVIEW" for code in json.loads(r["evidence_json"])["comparisons"]["v2"]["review_reason_codes"]).items()))
                report["new_match_support_profile"] = dict(
                    descriptor_or_bilingual_core=sum(v["name_features"]["descriptor_alias_support"] for v in contexts),
                    description_head=sum(v["name_features"]["descriptor_head_agreement"] for v in contexts),
                    one_edit_alignment=sum(v["name_features"]["typo_support"] for v in contexts),
                    normalized_full_address_equal=sum(v["address_features_v3"]["normalized_surface_equal"] for v in contexts),
                    identifying_prefix_equal_with_admin_tail=sum(v["address_features_v3"]["observed_prefix_equal_with_admin_tail"] for v in contexts),
                    non_parsed_input_address=sum(any(v["address_features_v3"][side]["original_parse_status"]!="PARSED" for side in ["overture","google"]) for v in contexts),
                    counts_overlap=True)
    inventory = json.loads((m/"reports/google_silver_dq_summary.json").read_text(encoding="utf-8"))["zero_candidate_inventory"]
    if {r["overture_id"] for r in inventory} & set(table["overture_id"].to_pylist()):
        raise ValueError("Zero-candidate inventory overlaps links")
    report["unchanged_zero_candidate_overture_ids"] = inventory
    report["zero_candidate_count"] = len(inventory)
    signature = dict(rule_version=rule_version, thresholds=DEFAULTS if version=="v1" else policy.DEFAULTS, inputs_sha256=dict(sorted(hashes.items())),
        code_sha256={str(Path(__file__).resolve()):silver.sha(__file__), str(Path(silver.__file__).resolve()):silver.sha(silver.__file__)},
        pyarrow_version=pa.__version__)
    if version != "v1":
        signature["code_sha256"][str(Path(v2.__file__).resolve())] = silver.sha(v2.__file__)
    if version == "v3":
        for module in [v3, __import__("fnb_platform.comparison_features",fromlist=["comparison_text"])]:
            signature["code_sha256"][str(Path(module.__file__).resolve())] = silver.sha(module.__file__)
    rid = _digest(signature)[:24]
    out.mkdir(parents=True, exist_ok=True)
    lock = out/".publish.lock"
    with lock.open("x") as stream:
        stream.write(str(os.getpid()))
    try:
        silver.read_active(out)
        dest = out/"releases"/rid
        if not dest.exists():
            stage = out/(".stage-"+uuid.uuid4().hex)
            stage.mkdir()
            pq.write_table(table, stage/"er_results.parquet", compression="zstd")
            with (stage/"review_queue.csv").open("w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=QUEUE)
                writer.writeheader(); writer.writerows(queue)
            (stage/"er_summary.json").write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2)+"\n",encoding="utf-8")
            artifacts = {n:dict(sha256=silver.sha(stage/n)) for n in ("er_results.parquet","review_queue.csv","er_summary.json")}
            artifacts["er_results.parquet"].update(rows=table.num_rows,logical_sha256=_digest(table.to_pylist()),schema=str(table.schema))
            (stage/"manifest.json").write_text(json.dumps(dict(release_id=rid,signature=signature,artifacts=artifacts),sort_keys=True,indent=2),encoding="utf-8")
            silver.verify_release(stage)
            fault("after_stage_validation")
            for p, digest in hashes.items():
                if silver.sha(p) != digest:
                    raise ValueError("Input changed before publication: "+p)
            dest.parent.mkdir(exist_ok=True)
            os.replace(stage,dest)
        else:
            silver.verify_release(dest)
        fault("before_pointer_replace")
        pointer=out/(".current-"+uuid.uuid4().hex)
        pointer.write_text(silver.dump(dict(release_id=rid,manifest_sha256=silver.sha(dest/"manifest.json"))),encoding="utf-8")
        os.replace(pointer,out/"current.json")
        return dest, report
    finally:
        lock.unlink()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--comparison-root",type=Path,required=True)
    parser.add_argument("--output-root",type=Path,required=True)
    parser.add_argument("--version",choices=["v1","v2","v3"],default="v1")
    parser.add_argument("--baseline-root",type=Path)
    args=parser.parse_args()
    if args.version != "v1" and args.baseline_root is None:
        parser.error("Experimental ER requires --baseline-root for the preceding version")
    dest, report=publish(args.comparison_root,args.output_root, version=args.version, baseline_root=args.baseline_root)
    print(silver.dump(dict(release=str(dest),counts=report["classification_counts"],rows=report["rows"],zero_candidates=report["zero_candidate_count"])))
