"""M2.2: reproducible offline benchmark over two cached Overture extracts.

Run: python -B -m fnb_platform.overture.comparison_run
Never modifies existing Bronze or cached input files.
"""
from __future__ import annotations

import csv
import argparse
import hashlib
import json
import math
import os
import shutil
import time
from collections import Counter
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from shapely import from_wkb
from shapely.geometry import shape

ROOT = Path(__file__).resolve().parents[3]
from ..overture_incremental import compare_releases, selected_comparison_schema
from .snapshot import validate_snapshot, validate_pair_scope

OLD = ROOT / "data/bronze/overture/places.parquet"
OLD_META = ROOT / "data/bronze/overture/source_metadata.json"
NEW = ROOT / "output/district4-profile/cache/overture-2026-10-21.0-7667bf6c7576.parquet"
NEW_META = NEW.with_suffix(".json")
WARD = ROOT / "config/admin_areas/phuong_khanh_hoi.geojson"
DEST = ROOT / "output/overture-m22/khanh-hoi-2026-09-23.1_to_2026-10-21.0-v1"
FIELDS = ("id", "names", "basic_category", "taxonomy", "operating_status", "addresses", "geometry", "confidence")
BUSINESS_FIELDS = frozenset({"names", "basic_category", "taxonomy", "operating_status", "addresses", "geometry"})
FNB_CATEGORIES = {"restaurant", "coffee_shop", "cafe", "diner", "bakery", "bubble_tea_shop", "fast_food_restaurant", "food_and_drink", "bar", "pub", "ice_cream_shop", "juice_bar", "tea_house", "dessert_shop", "seafood_restaurant"}
BUFFER_DEGREES = 0.003  # documented approximation, not an exact 300 m geodesic buffer
BOUNDARY_ARTIFACT = "boundary.geojson"


def boundary_polygon(content):
    """Parse the exact pinned GeoJSON bytes, using the existing covers semantics."""
    document = json.loads(content)
    if document.get("type") == "FeatureCollection" and len(document.get("features", [])) == 1:
        feature = document["features"][0]
    elif document.get("type") == "Feature":
        feature = document
    else:
        raise ValueError("Boundary must contain exactly one GeoJSON feature")
    for item in (document, feature):
        crs = item.get("crs")
        if crs and crs.get("properties", {}).get("name") not in (
            "EPSG:4326", "urn:ogc:def:crs:OGC:1.3:CRS84", "urn:ogc:def:crs:EPSG::4326",
        ):
            raise ValueError("Boundary must use WGS84 coordinates")
    polygon = shape(feature["geometry"])
    if not polygon.is_valid or polygon.is_empty or polygon.geom_type not in ("Polygon", "MultiPolygon"):
        raise ValueError("Invalid pilot polygon")
    w, s, e, n = polygon.bounds
    if not all(math.isfinite(v) for v in polygon.bounds) or not (-180 <= w <= e <= 180 and -90 <= s <= n <= 90):
        raise ValueError("Boundary coordinates must be WGS84 longitude/latitude")
    return polygon


def checksum(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def canonical(value):
    if isinstance(value, dict):
        return {key: canonical(val) for key, val in sorted(value.items())}
    if isinstance(value, list):
        return [canonical(v) for v in value]
    if isinstance(value, bytes):
        return value.hex()
    return value


def fnb(record):
    taxonomy = record.get("taxonomy") or {}
    primary = taxonomy.get("primary") if isinstance(taxonomy, dict) else None
    return primary in FNB_CATEGORIES or record.get("basic_category") == "food_and_drink"


def relevant(record, ward, buffer):
    geom = record.get("geometry")
    if geom is None:
        return None
    try:
        point = from_wkb(geom)
    except Exception:
        return None
    if point is None or point.is_empty or not point.is_valid or point.geom_type != "Point":
        return None
    if not buffer.covers(point):
        return None
    return {"inside_ward": ward.covers(point), "coordinates": (point.x, point.y)}


def extract(path, ward, buffer):
    parquet = pq.ParquetFile(path)
    if not set(FIELDS).issubset(parquet.schema_arrow.names):
        raise ValueError(f"Missing required columns: {set(FIELDS) - set(parquet.schema_arrow.names)}")
    rows, spatial, invalid, ids = [], {}, 0, set()
    for batch in parquet.iter_batches(batch_size=5000, columns=list(FIELDS)):
        for row in batch.to_pylist():
            if not isinstance(row["id"], str) or not row["id"].strip():
                raise ValueError("Missing source ID")
            if row["id"] in ids:
                raise ValueError("Duplicate ID in raw input")
            ids.add(row["id"])
            loc = relevant(row, ward, buffer)
            if not loc:
                if row["geometry"] is None:
                    invalid += 1
                continue
            rows.append(row)
            spatial[row["id"]] = loc
    return rows, spatial, {"raw_rows": parquet.metadata.num_rows, "within_buffer": len(rows),
                           "missing_geometry": invalid, "unique_source_ids": len(ids)}


def field_changes(before, after):
    return [col for col in FIELDS if col != "id" and canonical(before.get(col)) != canonical(after.get(col))]


def deletion_guard(old_count, missing_count, *, maximum_fraction=0.20):
    """Project-specific fail-closed review gate, not a global Overture threshold."""
    if old_count < 1:
        raise ValueError("Cannot assess disappearance against empty prior release")
    if missing_count / old_count > maximum_fraction:
        raise ValueError("Unexpected mass disappearance: manual review required")


def interpret(before, after, old_loc, new_loc):
    changed = field_changes(before, after) if before and after else []
    if before and after:
        kind = "UPDATED" if changed else "UNCHANGED"
    elif after:
        kind = "NEW"
    else:
        kind = "MISSING_FROM_RELEASE"
    if before and after:
        if old_loc["inside_ward"] and not new_loc["inside_ward"]:
            explanation = "MOVED_OUTSIDE_WARD_WITHIN_BUFFER"
        elif new_loc["inside_ward"] and not old_loc["inside_ward"]:
            explanation = "MOVED_INTO_WARD_WITHIN_BUFFER"
        elif fnb(before) != fnb(after):
            explanation = "CATEGORY_ENTERED_FNB" if fnb(after) else "CATEGORY_LEFT_FNB"
        else:
            explanation = "PRESENT_IN_BOTH"
    elif before:
        explanation = "ABSENT_IN_NEW_BUFFER_UNRESOLVED"
    else:
        explanation = "ABSENT_IN_OLD_BUFFER_UNRESOLVED"
    return {
        "id": (after or before)["id"], "status": kind,
        "old_inside_ward": old_loc["inside_ward"] if old_loc else None,
        "new_inside_ward": new_loc["inside_ward"] if new_loc else None,
        "old_fnb": fnb(before) if before else None,
        "new_fnb": fnb(after) if after else None,
        "changed_fields": changed, "substantive_change": bool(BUSINESS_FIELDS.intersection(changed)),
        "explanation": explanation,
    }


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def main(destination=DEST, *, old_path=None, old_metadata=None, new_path=None,
         new_metadata=None, ward_path=None):
    started = time.perf_counter()
    destination = Path(destination)
    # No-argument invocation remains the frozen historical benchmark. Explicit
    # snapshots use validated release labels without changing historical paths.
    frozen = all(p is None for p in (old_path, old_metadata, new_path, new_metadata, ward_path))
    if not frozen and any(p is None for p in (old_path, old_metadata, new_path, new_metadata, ward_path)):
        raise ValueError("Explicit mode requires OLD/NEW snapshots, metadata and ward")
    old_path, old_metadata, new_path, new_metadata, ward_path = map(Path,
        (OLD, OLD_META, NEW, NEW_META, WARD) if frozen else
        (old_path, old_metadata, new_path, new_metadata, ward_path))
    meta_old = validate_snapshot(old_path, old_metadata,
        expected_release="2026-09-23.1" if frozen else None)
    meta_new = validate_snapshot(new_path, new_metadata,
        expected_release="2026-10-21.0" if frozen else None)
    schema, schema_policy = selected_comparison_schema(
        pq.read_schema(old_path), pq.read_schema(new_path), FIELDS)
    boundary_bytes = ward_path.read_bytes()
    polygon = boundary_polygon(boundary_bytes)
    buffer = polygon.buffer(BUFFER_DEGREES)
    validate_pair_scope(meta_old, meta_new, required_bbox=buffer.bounds)
    comparison_bbox = [106.693, 10.749, 106.715, 10.769] if frozen else list(buffer.bounds)
    if frozen:
        w, s, e, n = comparison_bbox
        a, b, c, d = buffer.bounds
        if not (w <= a and s <= b and e >= c and n >= d):
            raise ValueError("Frozen benchmark boundary exceeds historical extraction scope")
        validate_pair_scope(meta_old, meta_new, required_bbox=comparison_bbox)
    input_hashes = {"old": meta_old["artifact_sha256"], "new": meta_new["artifact_sha256"],
                    "polygon": hashlib.sha256(boundary_bytes).hexdigest()}
    if destination.exists():
        manifest_path = destination / "manifest.json"
        if not manifest_path.exists():
            raise ValueError("Existing incomplete destination; never overwrite")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (manifest["input_sha256"] != input_hashes or
                manifest["old_release"] != meta_old["source_release"] or
                manifest["new_release"] != meta_new["source_release"]):
            raise ValueError("Existing comparison input/release mismatch; never overwrite")
        for file, digest in manifest["output_sha256"].items():
            if checksum(destination / file) != digest:
                raise ValueError("Existing published output differs from manifest")
        print(json.dumps({"status": "REUSED_VERIFIED", "output": str(destination)}, ensure_ascii=False))
        return
    old_rows, old_geo, old_counts = extract(old_path, polygon, buffer)
    new_rows, new_geo, new_counts = extract(new_path, polygon, buffer)
    if not old_rows or not new_rows:
        raise ValueError("Empty study area")
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = destination.parent / ("." + destination.name + ".stage")
    if stage.exists():
        raise ValueError("Previous partial stage exists; manual recovery required")
    stage.mkdir()
    try:
        # Both raw schemas passed structural validation before any output writes.
        # Persist the same bytes used for extraction, never reread mutable geometry.
        (stage / BOUNDARY_ARTIFACT).write_bytes(boundary_bytes)
        pq.write_table(pa.Table.from_pylist(old_rows, schema=schema), stage / "old.parquet", compression="zstd")
        pq.write_table(pa.Table.from_pylist(new_rows, schema=schema), stage / "new.parquet", compression="zstd")
        release_meta = {"extraction_bbox": comparison_bbox,
                        "spatial_filter": "point within Khánh Hội polygon buffered by 0.003 degrees" if frozen else "point within supplied polygon buffered by 0.003 degrees",
                        "taxonomy_policy": "same F&B classification applied after retaining all Places",
                        "source_schema": "local common 8-field projection; not raw Overture schema"}
        old_info = dict(release_meta, release=meta_old["source_release"], rows=len(old_rows),
                        sha256=checksum(stage / "old.parquet"))
        new_info = dict(release_meta, release=meta_new["source_release"], rows=len(new_rows),
                        sha256=checksum(stage / "new.parquet"))
        write_json(stage / "old.json", old_info)
        write_json(stage / "new.json", new_info)
        diff = compare_releases(stage / "old.parquet", stage / "old.json",
                                stage / "new.parquet", stage / "new.json")
        old_idx = {r["id"]: r for r in old_rows}
        new_idx = {r["id"]: r for r in new_rows}
        changes = [interpret(old_idx.get(i), new_idx.get(i), old_geo.get(i), new_geo.get(i))
                   for i in sorted(old_idx.keys() | new_idx.keys())]
        measured = Counter(r["status"] for r in changes)
        if measured != Counter({k: v for k, v in diff["counts"].items() if v}):
            raise ValueError("Detailed output disagrees with M2.1 comparator")
        deletion_guard(len(old_rows), measured["MISSING_FROM_RELEASE"])
        pq.write_table(pa.Table.from_pylist(changes), stage / "poi_changes.parquet", compression="zstd")
        with (stage / "release_comparison_metrics.csv").open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["metric", "value"])
            for key, value in sorted(diff["counts"].items()):
                writer.writerow([key, value])
            for key, value in sorted(Counter(r["explanation"] for r in changes).items()):
                writer.writerow([key, value])
            for key, value in sorted(Counter(f for r in changes for f in r["changed_fields"]).items()):
                writer.writerow(["changed_field_" + key, value])
        metrics = {"comparison": diff["counts"], "old": old_counts, "new": new_counts,
                   "explanations": dict(Counter(r["explanation"] for r in changes)),
                   "changed_fields": dict(Counter(f for r in changes for f in r["changed_fields"])),
                   "metadata_only_updates": sum(r["status"] == "UPDATED" and not r["substantive_change"] for r in changes),
                   "substantive_updates": sum(r["status"] == "UPDATED" and r["substantive_change"] for r in changes),
                   "inside_ward_old_fnb": sum(bool(r["old_inside_ward"] and r["old_fnb"]) for r in changes),
                   "inside_ward_new_fnb": sum(bool(r["new_inside_ward"] and r["new_fnb"]) for r in changes)}
        write_json(stage / "metrics.json", metrics)
        elapsed = time.perf_counter() - started
        size_read = old_path.stat().st_size + new_path.stat().st_size
        write_json(stage / "performance.json", {"elapsed_seconds": elapsed,
                    "source_file_bytes": size_read, "method": "sequential local full-Parquet scan; cached input",
                    "changelog_bytes_read": None, "changelog_elapsed_seconds": None})
        artifacts = [BOUNDARY_ARTIFACT, "old.parquet", "new.parquet", "old.json", "new.json", "poi_changes.parquet",
                     "metrics.json", "performance.json", "release_comparison_metrics.csv"]
        manifest = {"old_release": meta_old["source_release"], "new_release": meta_new["source_release"],
                    "old_source": str(old_path), "new_source": str(new_path),
                    "input_sha256": input_hashes,
                    "boundary_artifact": BOUNDARY_ARTIFACT,
                    "comparison_schema_policy": schema_policy,
                    "output_sha256": {name: checksum(stage / name) for name in artifacts},
                    "projection_limitation": "Old September snapshot lacks source/version/contact/bbox fields",
                    "filter_scope": release_meta, "rows": metrics}
        write_json(stage / "manifest.json", manifest)
        os.replace(stage, destination)
        print(json.dumps({"status": "PUBLISHED", "destination": str(destination), "counts": metrics,
                          "elapsed_seconds": elapsed}, ensure_ascii=False))
    except Exception:
        # Preserve failed stage for evidence and explicit manual recovery.
        raise


def cli():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("old-path", "old-metadata", "new-path", "new-metadata", "ward-path", "destination"):
        parser.add_argument("--" + name, type=Path)
    args = parser.parse_args()
    explicit = any(getattr(args, name) is not None for name in
                   ("old_path", "old_metadata", "new_path", "new_metadata", "ward_path"))
    if explicit and args.destination is None:
        parser.error("Explicit snapshots require --destination; historical paths are not reused")
    main(args.destination or DEST, old_path=args.old_path, old_metadata=args.old_metadata,
         new_path=args.new_path, new_metadata=args.new_metadata, ward_path=args.ward_path)


if __name__ == "__main__":
    cli()
