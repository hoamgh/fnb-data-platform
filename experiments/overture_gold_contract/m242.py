"""M2.4.2 offline, read-only input reconciliation and proposed eligibility audit.

Run: python -B experiments/overture_gold_contract/m242.py
Outputs are isolated from the published Bronze/Silver stores.
"""
from __future__ import annotations

import csv
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from shapely import from_wkb
from shapely.geometry import shape

ROOT = Path(__file__).resolve().parents[2]
# Historical script bootstrap only; canonical modules do not mutate paths.
sys.path.insert(0, str(ROOT / "src"))
from fnb_platform.overture import eligibility as _current, eligibility_policy as _policy
HIST = ROOT / "output/district4-profile/runs/15d25e5b4b9e6f1c66e9/overture_fnb_district4.parquet"
SILVER = ROOT / "output/overture-silver-m241/releases/2026-10-21.0/current_state.parquet"
SOURCE = ROOT / "output/overture-m22/khanh-hoi-2026-09-23.1_to_2026-10-21.0-v1/new.parquet"
BOUNDARY = ROOT / "config/admin_areas/phuong_khanh_hoi.geojson"
DEST = ROOT / "experiments/overture_gold_contract/results/m242-v1"


def sha(path):
    return _current.sha(path)


def taxonomy_decision(row):
    return _policy.taxonomy_decision(row)


def eligibility(inside, taxonomy_reason, severity, status, presence="PRESENT_IN_RELEASE", confidence=None):
    return _policy.eligibility(inside, taxonomy_reason, severity, status, presence, confidence)


def historical_inside(rows, polygon):
    return _current.historical_inside(rows, polygon, _decode_wkb=from_wkb)


def analyze(*, historical_path=HIST, silver_path=SILVER, source_path=SOURCE, boundary_path=BOUNDARY):
    """Historical audit API; preserve live policy and geometry monkeypatch bindings."""
    return _current.analyze(historical_path=historical_path, silver_path=silver_path,
        source_path=source_path, boundary_path=boundary_path,
        _taxonomy_decision=taxonomy_decision, _eligibility=eligibility,
        _historical_inside=historical_inside, _shape=shape)


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
