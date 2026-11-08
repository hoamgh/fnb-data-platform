"""M2.4.2 offline, read-only input reconciliation and proposed eligibility audit.

Run: python -B experiments/overture_gold_contract/m242.py
Outputs are isolated from the published Bronze/Silver stores.
"""
from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from shapely import from_wkb
from shapely.geometry import shape

ROOT = Path(__file__).resolve().parents[2]
HIST = ROOT / "output/district4-profile/runs/15d25e5b4b9e6f1c66e9/overture_fnb_district4.parquet"
SILVER = ROOT / "output/overture-silver-m241/releases/2026-10-21.0/current_state.parquet"
SOURCE = ROOT / "output/overture-m22/khanh-hoi-2026-09-23.1_to_2026-10-21.0-v1/new.parquet"
BOUNDARY = ROOT / "config/admin_areas/phuong_khanh_hoi.geojson"
DEST = ROOT / "experiments/overture_gold_contract/results/m242-v1"


def sha(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def taxonomy_decision(row):
    """Proposed policy v1: hierarchy controls eligibility, alternates trigger review."""
    taxonomy = row.get("taxonomy") or {}
    hierarchy = taxonomy.get("hierarchy") or []
    alternates = taxonomy.get("alternates") or []
    primary = taxonomy.get("primary")
    if hierarchy and primary != hierarchy[-1]:
        return "REVIEW_INVALID_TAXONOMY_PATH"
    if "food_and_drink" in hierarchy:
        return "INCLUDE_HIERARCHY_FNB"
    if "food_and_drink" in alternates:
        return "REVIEW_ALTERNATE_FNB"
    if not primary or not hierarchy:
        return "REVIEW_MISSING_TAXONOMY"
    return "EXCLUDE_NON_FNB_HIERARCHY"


def eligibility(inside, taxonomy_reason, severity, status, presence="PRESENT_IN_RELEASE", confidence=None):
    if presence != "PRESENT_IN_RELEASE":
        return False, "NOT_PRESENT_IN_RELEASE_REVIEW"
    if inside is not True:
        return False, "OUTSIDE_OR_INVALID_GEOMETRY"
    if severity == "CRITICAL":
        return False, "CRITICAL_DQ"
    if confidence is not None and not 0 <= confidence <= 1:
        return False, "INVALID_CONFIDENCE"
    if status == "permanently_closed":
        return False, "SOURCE_REPORTED_CLOSED_REVIEW"
    if status == "temporarily_closed":
        return False, "SOURCE_REPORTED_TEMPORARILY_CLOSED_REVIEW"
    if taxonomy_reason != "INCLUDE_HIERARCHY_FNB":
        return False, taxonomy_reason
    return True, "HIERARCHY_FNB_WITH_VALID_GEOMETRY"


def historical_inside(rows, polygon):
    result = {}
    for row in rows:
        geom = from_wkb(row["geometry"]) if row.get("geometry") else None
        pt = geom if geom is not None and geom.geom_type == "Point" else (
            geom.representative_point() if geom is not None and geom.is_valid and not geom.is_empty else None)
        if pt is not None and polygon.covers(pt):
            if row["id"] in result:
                raise ValueError("Duplicate historical source ID")
            result[row["id"]] = row
    return result


def analyze(*, historical_path=HIST, silver_path=SILVER, source_path=SOURCE, boundary_path=BOUNDARY):
    """Reuse the audit rules with explicitly pinned, read-only inputs."""
    feature = json.loads(Path(boundary_path).read_text(encoding="utf-8"))
    polygon = shape(feature["geometry"] if feature["type"] == "Feature" else feature["features"][0]["geometry"])
    hist = historical_inside(pq.read_table(historical_path).to_pylist(), polygon)
    silver_list = pq.read_table(silver_path).to_pylist()
    silver = {r["source_id"]: r for r in silver_list}
    if len(silver) != len(silver_list):
        raise ValueError("Duplicate Silver source ID")
    source_list = pq.read_table(source_path, columns=["id", "taxonomy", "basic_category", "operating_status"]).to_pylist()
    source = {r["id"]: r for r in source_list}
    if len(source) != len(source_list) or set(source) != set(silver):
        raise ValueError("Source/Silver identity mismatch")
    current = {k for k, r in silver.items() if r["gold_candidate"]}
    all_ids = sorted(set(hist) | current)
    evidence = []
    categories = Counter()
    reasons = Counter()
    for key in all_ids:
        old = hist.get(key)
        now = silver.get(key)
        tx = (old or {}).get("taxonomy") or {}
        decision = taxonomy_decision(old) if old else "NO_HISTORICAL_RECORD"
        proposed, reason = eligibility(now["inside_ward"], decision, now["dq_severity"],
                                       now["source_operating_status"], now["source_presence"], now["confidence"]) if now else (False, "MISSING_CURRENT_SILVER")
        membership = "BOTH" if old and key in current else ("HISTORICAL_ONLY" if old else "CURRENT_ONLY")
        explanation = "CURRENT_PRIMARY_LIST_EXCLUDES_FNB_HIERARCHY" if membership == "HISTORICAL_ONLY" and now and now["inside_ward"] and not now["is_fnb"] and decision == "INCLUDE_HIERARCHY_FNB" else ("CURRENT_POLICY_OR_SCOPE_OTHER" if membership == "HISTORICAL_ONLY" else "SAME_ID")
        reasons[explanation] += 1
        categories[(tx.get("primary") or "UNKNOWN", membership)] += 1
        evidence.append(dict(source_id=key, membership=membership,
                             historical_primary=tx.get("primary"),
                             historical_hierarchy="|".join(tx.get("hierarchy") or []),
                             historical_alternates="|".join(tx.get("alternates") or []),
                             current_primary=now["taxonomy_primary"] if now else None,
                             current_basic=now["basic_category"] if now else None,
                             inside_serving_polygon=now["inside_ward"] if now else None,
                             dq_status=now["dq_severity"] if now else None,
                             source_operating_status=now["source_operating_status"] if now else None,
                             verification_status="UNKNOWN", is_fnb=decision == "INCLUDE_HIERARCHY_FNB",
                             taxonomy_reason=decision, gold_eligible=proposed,
                             eligibility_reason=reason, discrepancy_reason=explanation,
                             name=now["name"] if now else None))
    projected = []
    for key in sorted(silver):
        now, raw = silver[key], source[key]
        classification = taxonomy_decision(raw)
        eligible, reason = eligibility(now["inside_ward"], classification, now["dq_severity"],
                                       now["source_operating_status"], now["source_presence"], now["confidence"])
        projected.append(dict(source_id=key, source_release=now["source_release"],
                              inside_serving_polygon=now["inside_ward"],
                              is_fnb=classification == "INCLUDE_HIERARCHY_FNB", taxonomy_reason=classification,
                              taxonomy_primary=(raw.get("taxonomy") or {}).get("primary"),
                              taxonomy_hierarchy="|".join((raw.get("taxonomy") or {}).get("hierarchy") or []),
                              taxonomy_alternates="|".join((raw.get("taxonomy") or {}).get("alternates") or []),
                              basic_category=raw.get("basic_category"), dq_status=now["dq_severity"],
                              dq_reasons="|".join(now["dq_reasons"] or []),
                              source_operating_status=now["source_operating_status"],
                              verification_status="UNKNOWN", confidence=now["confidence"],
                              source_presence=now["source_presence"], gold_eligible=eligible,
                              eligibility_reason=reason))
    return hist, silver, current, evidence, categories, reasons, projected


def main():
    hist, silver, current, records, categories, reasons, projected = analyze()
    DEST.mkdir(parents=True, exist_ok=True)
    with (DEST / "record_reconciliation.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(records[0]))
        writer.writeheader(); writer.writerows(records)
    pq.write_table(pa.Table.from_pylist(records), DEST / "record_reconciliation.parquet", compression="zstd")
    pq.write_table(pa.Table.from_pylist(projected), DEST / "full_silver_eligibility.parquet", compression="zstd")
    with (DEST / "category_counts.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f); writer.writerow(["taxonomy_primary", "membership", "count"])
        writer.writerows((category, membership, count) for (category, membership), count in sorted(categories.items()))
    counts = Counter(r["membership"] for r in records)
    reasons_proposed = Counter(r["eligibility_reason"] for r in records)
    assert len(hist) == 725 and len(current) == 557 and len(silver) == 6199, "Unexpected source baseline"
    assert counts["BOTH"] + counts["HISTORICAL_ONLY"] == len(hist)
    assert counts["BOTH"] + counts["CURRENT_ONLY"] == len(current)
    assert len(projected) == len(silver)
    assert all(r["gold_eligible"] == (r["eligibility_reason"] == "HIERARCHY_FNB_WITH_VALID_GEOMETRY") for r in projected)
    classification_counts = Counter(r["taxonomy_reason"] for r in projected)
    projected_counts = Counter(r["eligibility_reason"] for r in projected)
    report = {"release": "2026-10-21.0", "counts": dict(counts),
              "discrepancy_reasons": dict(reasons), "proposed_eligibility_reasons": dict(reasons_proposed),
              "full_silver_count": len(projected), "full_silver_classification": dict(classification_counts),
              "full_silver_eligibility_reasons": dict(projected_counts),
              "full_silver_eligible_count": sum(r["gold_eligible"] for r in projected),
              "inputs_sha256": {str(p.relative_to(ROOT)): sha(p) for p in (HIST, SILVER, SOURCE, BOUNDARY)},
              "output_sha256": {p.name: sha(p) for p in (DEST / "record_reconciliation.csv", DEST / "record_reconciliation.parquet", DEST / "category_counts.csv", DEST / "full_silver_eligibility.parquet")}}
    (DEST / "manifest.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(report["counts"], sort_keys=True), json.dumps(report["discrepancy_reasons"], sort_keys=True),json.dumps(report["proposed_eligibility_reasons"],sort_keys=True))


if __name__ == "__main__":
    main()
