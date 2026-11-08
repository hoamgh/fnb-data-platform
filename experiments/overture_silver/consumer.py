"""Offline, pinned M2.3 change-set consumer into versioned M2.4 Silver Parquet.

python -B experiments/overture_silver/consumer.py --source output/overture-m22/khanh-hoi-2026-09-23.1_to_2026-10-21.0-v1 --bronze-publication output/overture-m23 --silver-root output/overture-silver-m24
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import unicodedata
import uuid
from collections import Counter
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
from shapely import from_wkb
from shapely.geometry import Point, shape

ROOT = Path(__file__).resolve().parents[2]
WARD_PATH = ROOT / "config/admin_areas/phuong_khanh_hoi.geojson"
WARD_METADATA = ROOT / "config/admin_areas/phuong_khanh_hoi_metadata.json"
DQ_SEVERITY = {"GEOMETRY_INVALID": "CRITICAL", "CONFIDENCE_INVALID": "WARNING",
               "NAME_MISSING": "WARNING", "TAXONOMY_MISSING": "WARNING",
               "ADDRESS_MISSING": "INFORMATIONAL"}
sys.path.insert(0, str(ROOT / "experiments" / "overture_incremental"))
import publish as bronze  # noqa: E402
from run import FIELDS, canonical, checksum, field_changes, fnb  # noqa: E402


class SilverError(ValueError):
    pass


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def normalize(value):
    if not isinstance(value, str):
        return None
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split()) or None


def rows_by_id(path):
    table = pq.read_table(path)
    if tuple(table.schema.names) != FIELDS:
        raise SilverError("Source projection schema incompatible")
    rows = table.to_pylist()
    ids = [r["id"] for r in rows]
    if any(not isinstance(x, str) or not x for x in ids) or len(set(ids)) != len(ids):
        raise SilverError("Source contains duplicate/missing IDs")
    return {r["id"]: r for r in rows}, table.schema


def pin_and_validate(publication, source):
    publication, source = Path(publication), Path(source)
    pinned = read_json(publication / "current.json")  # exactly one pointer resolution
    name = pinned.get("release_id")
    bronze.release_key(name)
    directory = publication / "releases" / name
    if checksum(directory / "manifest.json") != pinned.get("manifest_sha256"):
        raise SilverError("Bronze pointer/manifest checksum invalid")
    manifest = bronze.verify_release(directory)
    source_manifest_path = source / "manifest.json"
    if checksum(source_manifest_path) != manifest["source_manifest_sha256"]:
        raise SilverError("Source M2.2 manifest lineage mismatch")
    source_manifest = read_json(source_manifest_path)
    if (source_manifest["filter_scope"] != manifest["scope"] or
            source_manifest["old_release"] != manifest["old_release"] or
            source_manifest["new_release"] != manifest["new_release"] or
            source_manifest["input_sha256"] != manifest["source_input_sha256"] or
            manifest["new_release"] != name):
        raise SilverError("Source scope/release/lineage mismatch")
    for filename, expected in source_manifest["output_sha256"].items():
        if Path(filename).name != filename or checksum(source / filename) != expected:
            raise SilverError("M2.2 source checksum mismatch")
    for suffix, label in bronze.CHANGE_SETS.items():
        table = pq.read_table(directory / f"{suffix}.parquet")
        if any(row["status"] != ("UPDATED" if label.endswith("UPDATED") else label) for row in table.to_pylist()):
            raise SilverError("Change-set status does not match artifact")
    return directory, manifest, source_manifest, pinned


def reconstruct(old, new, published, manifest):
    current = dict(old)
    groups = {}
    seen = set()
    for group in bronze.CHANGE_SETS:
        group_rows = pq.read_table(published / f"{group}.parquet").to_pylist()
        group_ids = {r["id"] for r in group_rows}
        if len(group_ids) != len(group_rows) or seen.intersection(group_ids):
            raise SilverError("Overlapping/duplicate change identifiers")
        seen.update(group_ids)
        groups[group] = group_rows
    if (len(groups["new"]) != manifest["counts"]["NEW"] or
            len(groups["business_updated"]) + len(groups["metadata_updated"]) != manifest["counts"]["UPDATED"] or
            len(groups["missing_or_out_of_scope"]) != manifest["counts"]["MISSING_FROM_RELEASE"]):
        raise SilverError("Change set counts inconsistent")
    for group, records in groups.items():
        for change in records:
            key = change["id"]
            before, after = old.get(key), new.get(key)
            if group == "new" and (before is not None or after is None):
                raise SilverError("Invalid NEW identity")
            if group == "missing_or_out_of_scope" and (before is None or after is not None):
                raise SilverError("Invalid missing identity")
            if group in ("business_updated", "metadata_updated"):
                changed = field_changes(before, after) if before and after else None
                if (not changed or change["changed_fields"] != changed or
                        bool(set(changed) - {"confidence"}) != (group == "business_updated")):
                    raise SilverError("Business/metadata classification mismatch")
            if group == "missing_or_out_of_scope":
                current.pop(key)
            else:
                current[key] = after
    if current.keys() != new.keys() or any(canonical(current[k]) != canonical(new[k]) for k in new):
        raise SilverError("Incremental reconstruction differs from full rebuild")
    unchanged = set(old) & set(new) - seen
    if len(unchanged) != manifest["counts"]["UNCHANGED"] or any(field_changes(old[k], new[k]) for k in unchanged):
        raise SilverError("UNCHANGED reconciliation failed")
    return current, groups


def load_ward():
    document = read_json(WARD_PATH)
    if document.get("type") == "FeatureCollection":
        features = document["features"]
        if len(features) != 1:
            raise SilverError("Expected one ward polygon")
        geometry = features[0]["geometry"]
    elif document.get("type") == "Feature":
        geometry = document["geometry"]
    else:
        raise SilverError("Unexpected ward geometry format")
    polygon = shape(geometry)
    metadata = read_json(WARD_METADATA)
    if (not polygon.is_valid or polygon.is_empty or polygon.geom_type not in ("Polygon", "MultiPolygon")
            or not metadata["validation"]["geometry_valid"] or "4326" not in metadata["crs"]):
        raise SilverError("Ward polygon/CRS invalid")
    return polygon


def make_silver(rows, release, manifest_hash, scope):
    ward = load_ward()
    result = []
    for key in sorted(rows):
        row = rows[key]
        name = (row.get("names") or {}).get("primary")
        addresses = row.get("addresses") or []
        address = next((a.get("freeform") for a in addresses if a and a.get("freeform")), None)
        taxonomy = (row.get("taxonomy") or {}).get("primary")
        lon = lat = None
        dq = []
        if not normalize(name):
            dq.append("NAME_MISSING")
        if not normalize(address):
            dq.append("ADDRESS_MISSING")
        if not taxonomy:
            dq.append("TAXONOMY_MISSING")
        try:
            geom = from_wkb(row.get("geometry"))
            if (geom is None or geom.geom_type != "Point" or geom.is_empty or not geom.is_valid or
                    not (-180 <= geom.x <= 180 and -90 <= geom.y <= 90)):
                dq.append("GEOMETRY_INVALID")
            else:
                lon, lat = geom.x, geom.y
        except Exception:
            dq.append("GEOMETRY_INVALID")
        confidence = row.get("confidence")
        if confidence is not None and not 0 <= confidence <= 1:
            dq.append("CONFIDENCE_INVALID")
        point = Point(lon, lat) if lon is not None and lat is not None else None
        on_boundary = bool(point is not None and ward.boundary.covers(point))
        inside = None if point is None else bool(ward.covers(point))
        is_food = fnb(row)
        severity = "CRITICAL" if "GEOMETRY_INVALID" in dq else (
            "WARNING" if any(DQ_SEVERITY[x] == "WARNING" for x in dq) else (
                "INFORMATIONAL" if dq else "NONE"))
        result.append({"source_id": key, "source": "overture", "source_release": release,
                       "source_manifest_sha256": manifest_hash, "name": name, "name_normalized": normalize(name),
                       "address": address, "address_normalized": normalize(address),
                       "basic_category": row.get("basic_category"), "taxonomy_primary": taxonomy,
                       "is_fnb": is_food, "longitude": lon, "latitude": lat,
                       "inside_ward": inside, "on_ward_boundary": on_boundary,
                       "recommendation_category": taxonomy if is_food else None,
                       "dq_severity": severity,
                       "gold_candidate": inside is True and is_food and severity != "CRITICAL",
                       "source_operating_status": row.get("operating_status"),
                       "verified_business_status": "UNKNOWN", "confidence": confidence,
                       "dq_valid": not dq, "dq_reasons": dq,
                       "scope_policy": scope["spatial_filter"],
                       "source_presence": "PRESENT_IN_RELEASE"})
    return result


def verify_silver(directory):
    directory = Path(directory)
    manifest = read_json(directory / "manifest.json")
    if manifest.get("ward_polygon_sha256") and (checksum(WARD_PATH) != manifest["ward_polygon_sha256"] or
            checksum(WARD_METADATA) != manifest.get("ward_metadata_sha256")):
        raise SilverError("Silver ward boundary provenance mismatch")
    if checksum(directory / "dq_metrics.json") != manifest["dq_metrics_sha256"]:
        raise SilverError("Silver quality metrics checksum mismatch")
    if read_json(directory / "dq_metrics.json") != manifest["metrics"]:
        raise SilverError("Silver quality metrics content mismatch")
    for filename, item in manifest["artifacts"].items():
        if checksum(directory / filename) != item["sha256"]:
            raise SilverError("Silver artifact checksum mismatch")
        table = pq.read_table(directory / filename)
        if table.num_rows != item["rows"] or table.schema.names != item["fields"]:
            raise SilverError("Silver artifact row/schema mismatch")
    current = pq.read_table(directory / "current_state.parquet", columns=["source_id"])
    ids = current.column("source_id").to_pylist()
    if len(set(ids)) != len(ids) or any(not x for x in ids):
        raise SilverError("Silver duplicate/missing IDs")
    return manifest


def read_silver_active(root):
    root = Path(root)
    pointer = root / "current.json"
    if not pointer.exists():
        return None
    value = read_json(pointer)
    bronze.release_key(value["release_id"])
    path = root / "releases" / value["release_id"]
    if checksum(path / "manifest.json") != value["manifest_sha256"]:
        raise SilverError("Silver active pointer mismatch")
    return path, verify_silver(path)


def publish(source, publication, silver_root, *, fault=lambda stage: None):
    source, publication, silver_root = map(lambda p: Path(p).resolve(), (source, publication, silver_root))
    for a, b in ((silver_root, source), (silver_root, publication)):
        if a == b or a in b.parents or b in a.parents:
            raise SilverError("Silver root must be isolated from inputs")
    directory, upstream, source_manifest, pointer = pin_and_validate(publication, source)
    old, old_schema = rows_by_id(source / "old.parquet")
    new, new_schema = rows_by_id(source / "new.parquet")
    if old_schema != new_schema:
        raise SilverError("Projection schemas differ")
    current, changes = reconstruct(old, new, directory, upstream)
    if len(old) != source_manifest["rows"]["old"]["within_buffer"] or len(current) != source_manifest["rows"]["new"]["within_buffer"]:
        raise SilverError("Source manifest row count mismatch")
    silver = make_silver(current, upstream["new_release"], pointer["manifest_sha256"], upstream["scope"])
    missing = [{"source_id": r["id"], "source_release": upstream["new_release"],
                "source_presence": "MISSING_FROM_RELEASE", "verified_business_status": "UNKNOWN",
                "explanation": r["explanation"]} for r in changes["missing_or_out_of_scope"]]
    silver_root.mkdir(parents=True, exist_ok=True)
    lock = silver_root / ".publication.lock"
    try:
        with lock.open("x") as writer:
            writer.write(str(os.getpid()))
    except FileExistsError:
        raise SilverError("Silver publication locked; manual review required") from None
    try:
        active = read_silver_active(silver_root)
        release = upstream["new_release"]
        if active and bronze.release_key(active[1]["release_id"]) > bronze.release_key(release):
            raise SilverError("Silver release regression")
        destination = silver_root / "releases" / release
        if destination.exists():
            existing = verify_silver(destination)
            if existing["bronze_manifest_sha256"] != pointer["manifest_sha256"] or existing["source_manifest_sha256"] != upstream["source_manifest_sha256"]:
                raise SilverError("Silver release identity collision")
        else:
            stage = silver_root / "staging" / (release + "-" + uuid.uuid4().hex)
            stage.mkdir(parents=True)
            fault("before_write")
            pq.write_table(pa.Table.from_pylist(silver), stage / "current_state.parquet", compression="zstd")
            pq.write_table(pa.Table.from_pylist(missing), stage / "missing_or_out_of_scope.parquet", compression="zstd")
            # DuckDB serves as the local model/query engine without a new service.
            con = duckdb.connect(":memory:")
            stats = con.execute("SELECT count(*) total, count(DISTINCT source_id) unique_ids, count(*) FILTER (WHERE dq_valid) dq_valid, count(*) FILTER (WHERE is_fnb) fnb, count(*) FILTER (WHERE NOT dq_valid) dq_flagged FROM read_parquet(?)", [str(stage / "current_state.parquet")]).fetchone()
            con.close()
            if stats[0] != len(current) or stats[1] != stats[0] or stats[2] + stats[4] != stats[0]:
                raise SilverError("Silver DuckDB validation failed")
            spatial = Counter("INVALID" if r["inside_ward"] is None else "INSIDE" if r["inside_ward"] else "OUTSIDE" for r in silver)
            flags = Counter(flag for r in silver for flag in r["dq_reasons"])
            severity_counts = Counter(r["dq_severity"] for r in silver)
            combinations = Counter("|".join(sorted(r["dq_reasons"])) or "NONE" for r in silver)
            metrics = {"current_rows": stats[0], "unique_ids": stats[1], "dq_valid": stats[2], "fnb_buffered": stats[3], "dq_flagged": stats[4], "missing_tracked": len(missing), "change_counts": {k: len(v) for k, v in changes.items()}, "unchanged": upstream["counts"]["UNCHANGED"], "full_rebuild_incremental_equal": True,
                       "spatial_counts": dict(spatial), "on_boundary": sum(r["on_ward_boundary"] for r in silver),
                       "strict_inside_fnb": sum(r["inside_ward"] is True and r["is_fnb"] for r in silver),
                       "gold_candidates_preliminary": sum(r["gold_candidate"] for r in silver),
                       "dq_flags": dict(flags), "dq_severity": dict(severity_counts),
                       "dq_combinations": dict(combinations)}
            write_json(stage / "dq_metrics.json", metrics)
            artifact_names = ("current_state.parquet", "missing_or_out_of_scope.parquet")
            artifacts = {file: {"sha256": checksum(stage / file), "rows": pq.read_metadata(stage / file).num_rows,
                                "fields": pq.read_schema(stage / file).names} for file in artifact_names}
            manifest = {"release_id": release, "old_release": upstream["old_release"],
                        "bronze_manifest_sha256": pointer["manifest_sha256"],
                        "source_manifest_sha256": upstream["source_manifest_sha256"],
                        "scope": upstream["scope"], "ward_polygon_sha256": checksum(WARD_PATH),
                        "ward_metadata_sha256": checksum(WARD_METADATA), "artifacts": artifacts,
                        "dq_metrics_sha256": checksum(stage / "dq_metrics.json"), "metrics": metrics}
            write_json(stage / "manifest.json", manifest)
            verify_silver(stage)
            fault("after_stage")
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(stage, destination)
            fault("after_rename")
        manifest = verify_silver(destination)
        temp = silver_root / (".current-" + uuid.uuid4().hex + ".tmp")
        try:
            write_json(temp, {"release_id": release, "manifest_sha256": checksum(destination / "manifest.json")})
            os.replace(temp, silver_root / "current.json")
        finally:
            temp.unlink(missing_ok=True)
        fault("after_pointer")
        read_silver_active(silver_root)
        return destination, manifest
    finally:
        lock.unlink(missing_ok=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source", type=Path, required=True)
    p.add_argument("--bronze-publication", type=Path, required=True)
    p.add_argument("--silver-root", type=Path, required=True)
    a = p.parse_args()
    destination, manifest = publish(a.source, a.bronze_publication, a.silver_root)
    print(json.dumps({"status": "VERIFIED_ACTIVE", "path": str(destination), "metrics": manifest["metrics"]}))


if __name__ == "__main__":
    main()
