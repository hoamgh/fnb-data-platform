"""M2.3 offline, fail-closed publication of an immutable M2.2 comparison.

Run: python -B experiments/overture_incremental/publish.py --source output/overture-m22/khanh-hoi-2026-09-23.1_to_2026-10-21.0-v1
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import uuid
from collections import Counter
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from fnb_platform.overture_incremental import compare_releases
from run import BUSINESS_FIELDS, FIELDS, canonical, checksum, field_changes

CHANGE_SETS = {"new": "NEW", "business_updated": "BUSINESS_UPDATED",
               "metadata_updated": "METADATA_UPDATED", "missing_or_out_of_scope": "MISSING_FROM_RELEASE"}
RELEASE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.\d+$")


def release_key(value):
    if not isinstance(value, str) or not RELEASE_RE.fullmatch(value):
        raise PublicationError("Release identity invalid")
    date, revision = value.split(".")
    return date, int(revision)


class PublicationError(ValueError):
    pass


def _json(path, obj):
    path.write_text(json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _validated_source(source, *, max_missing=0.20, max_category_shift=0.50):
    source = Path(source).resolve()
    manifest = _read(source / "manifest.json")
    for name, digest in manifest["output_sha256"].items():
        if Path(name).name != name or checksum(source / name) != digest:
            raise PublicationError("M2.2 source artifact checksum invalid")
    before, after = _read(source / "old.json"), _read(source / "new.json")
    previous, current = before["release"], after["release"]
    if release_key(previous) >= release_key(current):
        raise PublicationError("Release sequence/identity invalid")
    scope_keys = ("extraction_bbox", "spatial_filter", "taxonomy_policy", "source_schema")
    if any(before.get(k) != after.get(k) or before.get(k) != manifest["filter_scope"].get(k)
           for k in scope_keys):
        raise PublicationError("Geographic/taxonomy scope changed")
    comparison = compare_releases(source / "old.parquet", source / "old.json",
                                  source / "new.parquet", source / "new.json")
    old_table, new_table = pq.read_table(source / "old.parquet"), pq.read_table(source / "new.parquet")
    if old_table.schema != new_table.schema or set(old_table.schema.names) != set(FIELDS):
        raise PublicationError("Normalized source schema incompatible")
    old = {r["id"]: r for r in old_table.to_pylist()}
    new = {r["id"]: r for r in new_table.to_pylist()}
    changes = pq.read_table(source / "poi_changes.parquet")
    required = {"id", "status", "changed_fields", "substantive_change", "explanation"}
    if not required.issubset(changes.schema.names):
        raise PublicationError("Change schema invalid")
    records = changes.to_pylist()
    if len(records) != len(old.keys() | new.keys()) or len({r["id"] for r in records}) != len(records):
        raise PublicationError("Change IDs/count invalid")
    kinds = Counter()
    for r in records:
        key = r["id"]
        before_row, after_row = old.get(key), new.get(key)
        expected_fields = field_changes(before_row, after_row) if before_row and after_row else []
        status = ("NEW" if before_row is None else "MISSING_FROM_RELEASE" if after_row is None
                  else "UPDATED" if expected_fields else "UNCHANGED")
        if (r["status"] != status or r["changed_fields"] != expected_fields or
                r["substantive_change"] != bool(BUSINESS_FIELDS.intersection(expected_fields))):
            raise PublicationError("Changed-field classification mismatch")
        kinds[status] += 1
    if any(kinds[k] != comparison["counts"][k] for k in comparison["counts"]):
        raise PublicationError("Change count reconciliation invalid")
    if kinds["MISSING_FROM_RELEASE"] / len(old) > max_missing:
        raise PublicationError("Mass disappearance exceeds configured limit")
    def categories(rows):
        return Counter((r.get("taxonomy") or {}).get("primary") or "UNKNOWN" for r in rows.values())
    old_cat, new_cat = categories(old), categories(new)
    drift = max(abs(old_cat[k] / len(old) - new_cat[k] / len(new)) for k in old_cat.keys() | new_cat.keys())
    if drift > max_category_shift:
        raise PublicationError("Category distribution shift exceeds configured limit")
    for key in ("old", "new"):
        if manifest["rows"][key]["within_buffer"] != (len(old) if key == "old" else len(new)):
            raise PublicationError("Source row reconciliation invalid")
    if manifest["rows"]["comparison"] != comparison["counts"]:
        raise PublicationError("Source manifest comparison invalid")
    return manifest, records, comparison, drift


def verify_release(directory):
    directory = Path(directory)
    manifest = _read(directory / "manifest.json")
    if set(manifest["artifacts"]) != {f"{name}.parquet" for name in CHANGE_SETS}:
        raise PublicationError("Published artifact contract invalid")
    totals = Counter()
    for file, info in manifest["artifacts"].items():
        if checksum(directory / file) != info["sha256"]:
            raise PublicationError("Published checksum mismatch")
        data = pq.read_table(directory / file)
        if data.num_rows != info["rows"] or data.schema.names != manifest["schema_fields"]:
            raise PublicationError("Published row/schema mismatch")
        ids = data.column("id").to_pylist()
        if any(not isinstance(i, str) or not i for i in ids) or len(ids) != len(set(ids)):
            raise PublicationError("Published identities invalid")
        totals[file.removesuffix(".parquet")] = len(ids)
    if sum(totals.values()) != (manifest["counts"]["NEW"] + manifest["counts"]["UPDATED"] +
                                manifest["counts"]["MISSING_FROM_RELEASE"]):
        raise PublicationError("Published change set reconciliation invalid")
    return manifest


def read_active(root):
    root = Path(root)
    pointer = root / "current.json"
    if not pointer.exists():
        return None
    value = _read(pointer)
    name = value.get("release_id")
    if not isinstance(name, str) or not RELEASE_RE.fullmatch(name):
        raise PublicationError("Unsafe active-release identity")
    directory = root / "releases" / name
    if checksum(directory / "manifest.json") != value.get("manifest_sha256"):
        raise PublicationError("Active pointer checksum mismatch")
    return directory, verify_release(directory)


def publish(source, root, *, max_missing=0.20, max_category_shift=0.50,
            fault=lambda phase: None):
    """A single-writer transaction; exceptions leave previous pointer untouched."""
    root, source = Path(root).resolve(), Path(source).resolve()
    if root == source or root in source.parents or source in root.parents:
        raise PublicationError("Publication root must be isolated from source")
    root.mkdir(parents=True, exist_ok=True)
    lock = root / ".publication.lock"
    try:
        handle = lock.open("x", encoding="utf-8")
    except FileExistsError:
        raise PublicationError("Publication locked; manual stale-lock review required") from None
    try:
        with handle:
            handle.write(str(os.getpid()))
        manifest, records, comparison, drift = _validated_source(
            source, max_missing=max_missing, max_category_shift=max_category_shift)
        release = comparison["new_release"]
        active = read_active(root)
        if active and release_key(active[1]["new_release"]) > release_key(release):
            raise PublicationError("Release regression blocked")
        destination = root / "releases" / release
        source_manifest_hash = checksum(source / "manifest.json")
        if destination.exists():
            saved = verify_release(destination)
            if saved["source_manifest_sha256"] != source_manifest_hash:
                raise PublicationError("Release identity collision with different inputs")
        else:
            stage = root / "staging" / release
            if stage.exists():
                # Exclusive publication lock is held. Preserve abandoned evidence,
                # then reconstruct from the independently validated source.
                # A lock left by an uncertain/live owner blocks entry above.
                quarantine = root / "quarantine"
                quarantine.mkdir(parents=True, exist_ok=True)
                os.replace(stage, quarantine / f"{release}-{uuid.uuid4().hex}")
            stage.mkdir(parents=True)
            groups = {name: [] for name in CHANGE_SETS}
            for row in records:
                if row["status"] == "UNCHANGED":
                    continue
                key = ("business_updated" if row["status"] == "UPDATED" and row["substantive_change"] else
                       "metadata_updated" if row["status"] == "UPDATED" else
                       "new" if row["status"] == "NEW" else "missing_or_out_of_scope")
                groups[key].append(row)
            schema = pq.read_schema(source / "poi_changes.parquet")
            fault("before_write")
            files = {}
            for name, rows in groups.items():
                path = stage / f"{name}.parquet"
                pq.write_table(pa.Table.from_pylist(rows, schema=schema), path, compression="zstd")
                files[path.name] = {"sha256": checksum(path), "rows": len(rows)}
            output_manifest = {"old_release": comparison["old_release"], "new_release": release,
                               "source_manifest_sha256": source_manifest_hash,
                               "source_input_sha256": manifest["input_sha256"],
                               "scope": manifest["filter_scope"], "counts": comparison["counts"],
                               "category_max_share_shift": drift,
                               "quality_gate_config": {"max_missing_fraction": max_missing,
                                                       "max_category_share_shift": max_category_shift},
                               "schema_fields": schema.names, "artifacts": files}
            _json(stage / "manifest.json", output_manifest)
            verify_release(stage)
            fault("after_stage")
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(stage, destination)
            fault("after_release_rename")
        verified = verify_release(destination)
        if active and active[1]["new_release"] == release and active[0] != destination:
            raise PublicationError("Active release identity conflict")
        pointer = root / (".current-" + uuid.uuid4().hex + ".tmp")
        _json(pointer, {"release_id": release, "manifest_sha256": checksum(destination / "manifest.json")})
        try:
            os.replace(pointer, root / "current.json")
        finally:
            pointer.unlink(missing_ok=True)
        fault("after_pointer")
        read_active(root)
        return destination, verified
    finally:
        lock.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=Path("output/overture-m23"))
    parser.add_argument("--max-missing-fraction", type=float, default=0.20)
    parser.add_argument("--max-category-share-shift", type=float, default=0.50)
    args = parser.parse_args()
    if not 0 <= args.max_missing_fraction <= 1 or not 0 <= args.max_category_share_shift <= 1:
        parser.error("Anomaly thresholds must be within [0,1]")
    destination, manifest = publish(args.source, args.root, max_missing=args.max_missing_fraction,
                                     max_category_shift=args.max_category_share_shift)
    print(json.dumps({"status": "VERIFIED_ACTIVE", "destination": str(destination),
                      "counts": manifest["counts"], "change_sets": {k: v["rows"] for k, v in manifest["artifacts"].items()}}))


if __name__ == "__main__":
    main()
