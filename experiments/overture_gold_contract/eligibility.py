"""M1: validate existing Overture inputs and publish provisional eligibility only.

Run from the pipeline worktree with explicit --input-root and --output-root.
No network access, crawler execution, ER or Gold publication.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import uuid

import pyarrow as pa
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from experiments.overture_gold_contract import m242
from experiments.overture_silver import consumer

RELEASE = "2026-10-21.0"
POLICY = "m242-hierarchy-v1-provisional"
BASELINE = {"silver_rows": 6199, "inside_ward": 2616, "original_candidates": 557,
            "hierarchy_additions": 168, "candidates": 725, "missing_from_release": 24}
SOURCE_DIR = Path("output/overture-m22/khanh-hoi-2026-09-23.1_to_2026-10-21.0-v1")
SILVER_ROOT = Path("output/overture-silver-m241")
HIST = Path("output/district4-profile/runs/15d25e5b4b9e6f1c66e9/overture_fnb_district4.parquet")
REFERENCE = Path("experiments/overture_gold_contract/results/m242-v1/manifest.json")
BOUNDARY = Path("config/admin_areas/phuong_khanh_hoi.geojson")
ARTIFACTS = {"eligibility.parquet", "candidates.parquet", "missing_from_release.parquet", "dq_metrics.json"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def safe_child(root, relative):
    path = (root / relative).resolve()
    require(path.is_relative_to(root.resolve()), "Input path escapes its root")
    return path


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    consumer.write_json(path, value)


def validate_inputs(input_root):
    """Pin once, verify manifests/checksums, then replay existing Silver rules."""
    root = Path(input_root).resolve()
    silver_root = safe_child(root, SILVER_ROOT)
    pointer = read_json(silver_root / "current.json")
    require(pointer["release_id"] == RELEASE, "M1 requires the pinned October release")
    directory = silver_root / "releases" / RELEASE
    manifest_path = directory / "manifest.json"
    require(m242.sha(manifest_path) == pointer["manifest_sha256"], "Silver pointer checksum mismatch")
    # Reject unsafe artifact names before invoking the existing verifier.
    declared = read_json(manifest_path)
    require(set(declared["artifacts"]) == {"current_state.parquet", "missing_or_out_of_scope.parquet"},
            "Unexpected Silver artifact contract")
    manifest = consumer.verify_silver(directory)
    require(manifest["release_id"] == RELEASE, "Silver release mismatch")
    source_dir = safe_child(root, SOURCE_DIR)
    source_manifest_path = source_dir / "manifest.json"
    require(m242.sha(source_manifest_path) == manifest["source_manifest_sha256"], "Source manifest mismatch")
    source_manifest = read_json(source_manifest_path)
    require(source_manifest["new_release"] == RELEASE and source_manifest["filter_scope"] == manifest["scope"],
            "Source release/scope mismatch")
    source = source_dir / "new.parquet"
    require(m242.sha(source) == source_manifest["output_sha256"]["new.parquet"], "Source checksum mismatch")
    boundary = safe_child(root, BOUNDARY)
    require(m242.sha(boundary) == manifest["ward_polygon_sha256"], "Boundary checksum mismatch")
    require(m242.sha(boundary) == m242.sha(consumer.WARD_PATH), "Local boundary differs from pinned input")
    reference_path = safe_child(root, REFERENCE)
    reference = read_json(reference_path)
    require(reference["release"] == RELEASE, "Reconciliation reference release mismatch")
    silver = directory / "current_state.parquet"
    historical = safe_child(root, HIST)
    hashes = {k.replace("\\", "/"): v for k, v in reference["inputs_sha256"].items()}
    for path in (historical, silver, source, boundary):
        key = path.relative_to(root).as_posix()
        require(hashes.get(key) == m242.sha(path), "Reconciliation input checksum mismatch: " + key)
    raw, _ = consumer.rows_by_id(source)
    expected = consumer.make_silver(raw, RELEASE, manifest["bronze_manifest_sha256"], manifest["scope"])
    actual = pq.read_table(silver).to_pylist()
    require(sorted(actual, key=lambda r: r["source_id"]) == expected,
            "Silver does not reproduce pinned normalization/geography/DQ rules")
    paths = [manifest_path, source_manifest_path, reference_path, historical, silver, source, boundary,
             directory / "missing_or_out_of_scope.parquet", directory / "dq_metrics.json",
             safe_child(root, "config/admin_areas/phuong_khanh_hoi_metadata.json")]
    fingerprints = {p.relative_to(root).as_posix(): m242.sha(p) for p in paths}
    return {"root": root, "silver": silver, "source": source, "historical": historical,
            "boundary": boundary, "manifest": manifest, "fingerprints": fingerprints, "raw": raw}


def build_tables(inputs, expected_counts):
    hist, silver, current, evidence, _, _, projected = m242.analyze(
        historical_path=inputs["historical"], silver_path=inputs["silver"],
        source_path=inputs["source"], boundary_path=inputs["boundary"])
    selected = {r["source_id"] for r in projected if r["gold_eligible"]}
    require(current <= selected, "Original candidates no longer satisfy eligibility; review required")
    require(selected == set(hist), "Historical/current hierarchy candidate identity mismatch")
    require(all(r["gold_eligible"] for r in evidence), "Historical reconciliation is not fully eligible")
    rows = []
    for decision in projected:
        key = decision["source_id"]
        s, raw = silver[key], inputs["raw"][key]
        candidate = decision["gold_eligible"]
        taxonomy = raw.get("taxonomy") or {}
        rows.append({"source": "overture", "source_id": key, "source_release": RELEASE,
                     "source_manifest_sha256": s["source_manifest_sha256"],
                     "name": s["name"], "name_normalized": s["name_normalized"],
                     "address": s["address"], "address_normalized": s["address_normalized"],
                     "longitude": s["longitude"], "latitude": s["latitude"], "geometry": raw["geometry"],
                     "inside_ward": s["inside_ward"], "on_ward_boundary": s["on_ward_boundary"],
                     "taxonomy_primary": taxonomy.get("primary"), "taxonomy_hierarchy": taxonomy.get("hierarchy") or [],
                     "taxonomy_alternates": taxonomy.get("alternates") or [], "basic_category": raw.get("basic_category"),
                     "taxonomy_reason": decision["taxonomy_reason"], "eligibility_reason": decision["eligibility_reason"],
                     "is_fnb_candidate": candidate, "original_filter_candidate": key in current,
                     "eligibility_basis": ("ORIGINAL_FILTER" if key in current else "HIERARCHY_EXTENSION") if candidate else None,
                     "candidate_status": "PROVISIONAL" if candidate else "NOT_SELECTED",
                     "identity_status": "UNVERIFIED", "verified_business_status": "UNKNOWN",
                     "source_operating_status": s["source_operating_status"], "source_presence": s["source_presence"],
                     "confidence": s["confidence"], "dq_valid": s["dq_valid"], "dq_severity": s["dq_severity"],
                     "dq_reasons": s["dq_reasons"], "eligibility_policy_version": POLICY})
    lists = {"taxonomy_hierarchy", "taxonomy_alternates", "dq_reasons"}
    bools = {"inside_ward", "on_ward_boundary", "is_fnb_candidate", "original_filter_candidate", "dq_valid"}
    doubles = {"longitude", "latitude", "confidence"}
    require(bool(rows), "Empty Silver input")
    schema = pa.schema([(k, pa.list_(pa.string()) if k in lists else pa.bool_() if k in bools else
                         pa.float64() if k in doubles else pa.binary() if k == "geometry" else pa.string()) for k in rows[0]])
    all_rows = pa.Table.from_pylist(rows, schema=schema)
    candidates = pa.Table.from_pylist([r for r in rows if r["is_fnb_candidate"]], schema=schema)
    missing = pq.read_table(inputs["silver"].with_name("missing_or_out_of_scope.parquet"))
    missing_ids = missing.column("source_id").to_pylist()
    require(len(set(missing_ids)) == len(missing_ids) and all(missing_ids) and not set(missing_ids).intersection(silver),
            "Missing-from-release keys invalid or overlap current state")
    counts = {"silver_rows": len(rows), "inside_ward": sum(r["inside_ward"] is True for r in rows),
              "original_candidates": len(current), "hierarchy_additions": len(selected - current),
              "candidates": len(selected), "missing_from_release": missing.num_rows}
    require(counts == expected_counts, "Baseline counts changed; review required: " + str(counts))
    metrics = {"counts": counts, "eligibility_reasons": dict(Counter(r["eligibility_reason"] for r in rows)),
               "candidate_missing_address": sum(not r["address"] for r in rows if r["is_fnb_candidate"]),
               "candidate_missing_operating_status": sum(not r["source_operating_status"] for r in rows if r["is_fnb_candidate"]),
               "candidate_dq_severity": dict(Counter(r["dq_severity"] for r in rows if r["is_fnb_candidate"])),
               "verified_physical_store_count": None, "interpretation": "Counts are source POIs and provisional candidates, not verified businesses."}
    return {"eligibility.parquet": all_rows, "candidates.parquet": candidates,
            "missing_from_release.parquet": missing}, metrics


def verify_release(directory):
    directory = Path(directory)
    manifest = read_json(directory / "manifest.json")
    require(set(manifest["artifacts"]) == ARTIFACTS, "Publication artifact contract mismatch")
    for name, spec in manifest["artifacts"].items():
        path = directory / name
        require(m242.sha(path) == spec["sha256"], "Published checksum mismatch: " + name)
        if name.endswith(".parquet"):
            table = pq.read_table(path)
            require(table.num_rows == spec["rows"] and str(table.schema) == spec["schema"], "Published row/schema mismatch")
            ids = table.column("source_id").to_pylist()
            require(all(ids) and len(set(ids)) == len(ids), "Published duplicate/missing IDs")
    all_rows = pq.read_table(directory / "eligibility.parquet")
    candidates = pq.read_table(directory / "candidates.parquet")
    require(all_rows.filter(all_rows.column("is_fnb_candidate")).equals(candidates), "Candidate subset mismatch")
    require(read_json(directory / "dq_metrics.json") == manifest["metrics"], "Published metrics mismatch")
    return manifest


def read_active(root):
    pointer = Path(root) / "current.json"
    if not pointer.exists():
        return None
    p = read_json(pointer)
    require(re.fullmatch(r"[0-9a-f]{24}", p["run_id"]) is not None, "Invalid publication run ID")
    directory = Path(root) / "releases" / p["run_id"]
    require(m242.sha(directory / "manifest.json") == p["manifest_sha256"], "Publication pointer checksum mismatch")
    manifest = verify_release(directory)
    require(manifest["run_id"] == p["run_id"], "Publication identity mismatch")
    return directory, manifest


def publish(input_root, output_root, *, expected_counts=None, fault=lambda stage: None):
    expected_counts = dict(BASELINE if expected_counts is None else expected_counts)
    input_root, output_root = Path(input_root).resolve(), Path(output_root).resolve()
    require(not output_root.is_relative_to(input_root) and not input_root.is_relative_to(output_root),
            "Publication root must be separate from the read-only input worktree")
    inputs = validate_inputs(input_root)
    tables, metrics = build_tables(inputs, expected_counts)
    code_paths = [Path(__file__), Path(m242.__file__), Path(consumer.__file__),
                  ROOT / "experiments/overture_incremental/run.py", ROOT / "experiments/overture_incremental/publish.py",
                  ROOT / "src/fnb_platform/overture_incremental.py"]
    signature = {"schema_version": 1, "policy": POLICY, "source_release": RELEASE,
                 "inputs_sha256": inputs["fingerprints"], "expected_counts": expected_counts,
                 "code_sha256": {p.relative_to(ROOT).as_posix(): m242.sha(p) for p in code_paths},
                 "pyarrow_version": pa.__version__}
    run_id = hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()[:24]
    output_root.mkdir(parents=True, exist_ok=True)
    lock = output_root / ".publish.lock"
    with lock.open("x", encoding="utf-8") as stream:
        stream.write(str(os.getpid()))
    try:
        active = read_active(output_root)
        destination = output_root / "releases" / run_id
        if destination.exists():
            manifest = verify_release(destination)
            require(manifest["signature"] == signature and manifest["run_id"] == run_id, "Run identity conflict")
        else:
            stage = output_root / (".stage-" + uuid.uuid4().hex)
            stage.mkdir()
            artifacts = {}
            for name, table in tables.items():
                pq.write_table(table, stage / name, compression="zstd")
                artifacts[name] = {"sha256": m242.sha(stage / name), "rows": table.num_rows,
                                   "schema": str(pq.read_schema(stage / name))}
            write_json(stage / "dq_metrics.json", metrics)
            artifacts["dq_metrics.json"] = {"sha256": m242.sha(stage / "dq_metrics.json")}
            manifest = {"run_id": run_id, "signature": signature, "metrics": metrics, "artifacts": artifacts,
                        "grain": "One Overture source_id per source_release; no entity merge",
                        "primary_key": ["source", "source_release", "source_id"],
                        "input_root": str(input_root), "identity_status": "PROVISIONAL_ONLY",
                        "limitations": ["Not Gold or a verified physical-store count", "No source completeness assertion",
                                        "Brand/contact fields absent from the pinned eight-field source projection"]}
            write_json(stage / "manifest.json", manifest)
            verify_release(stage)
            fault("after_stage_validation")
            # Recheck the input snapshot before making outputs visible.
            for name, digest in inputs["fingerprints"].items():
                require(m242.sha(safe_child(input_root, name)) == digest, "Input changed during processing")
            destination.parent.mkdir(exist_ok=True)
            os.replace(stage, destination)
            fault("after_release_rename")
        if active and active[0] == destination:
            return destination, manifest
        temporary = output_root / (".current-" + uuid.uuid4().hex + ".tmp")
        write_json(temporary, {"run_id": run_id, "manifest_sha256": m242.sha(destination / "manifest.json")})
        fault("before_pointer_replace")
        os.replace(temporary, output_root / "current.json")
        read_active(output_root)
        return destination, manifest
    finally:
        lock.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()
    destination, manifest = publish(args.input_root, args.output_root)
    print(json.dumps({"release_path": str(destination), "counts": manifest["metrics"]["counts"]}))


if __name__ == "__main__":
    main()
