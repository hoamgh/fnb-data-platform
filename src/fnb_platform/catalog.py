"""Overture ward catalog with atomic, immutable generation publication.

The logical Parquet path resolves through <path>.catalog/current.json to
generations/<manifest-sha256>/catalog.parquet and catalog.manifest.json.
Only pointer replacement activates a generation. Historical Parquet/manifest
pairs remain read-only fallback until the first activation; never dual-write.
Use load_catalog, not a direct Parquet read of the logical path.

Identical rows and metadata (including observation timestamps) reuse the same
generation. Failed/unused private staging and orphan generations are retained;
a hard process exit can leave a lock requiring manual owner review.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import uuid
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from shapely import from_wkb, get_type_id, get_x, get_y, wkb
from shapely.geometry import Point, shape
from shapely.errors import GEOSException

from .models import CanonicalPlace
from .categories import (
    get_category_hierarchy, get_primary_category, get_product_category_group,
    is_food_place, normalize_categories,
)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RAW = ROOT / "data/bronze/overture/places.parquet"
DEFAULT_WARD = ROOT / "config/admin_areas/phuong_khanh_hoi.geojson"
DEFAULT_CATALOG = ROOT / "data/place_catalog/khanh_hoi/canonical_places.parquet"
DEFAULT_SOURCE = ROOT / "data/bronze/overture/places.parquet"
DEFAULT_SOURCE_METADATA = DEFAULT_SOURCE.with_name("source_metadata.json")
SCHEMA_VERSION = 1


@dataclass
class PlaceCatalog:
    places: list[CanonicalPlace]
    raw_payloads: dict[str, dict[str, Any]]
    counts: dict[str, int] = field(default_factory=dict)


def administrative_polygon(admin_area):
    """Validate and parse a current WGS84 boundary once per preparation."""
    properties = admin_area.get("properties", {})
    if properties.get("is_current") is not True or properties.get("valid_to"):
        raise ValueError("admin_area must be a current ward feature")
    if not properties.get("admin_area_id") or not properties.get("admin_area_name"):
        raise ValueError("admin_area identity and name are required")
    crs = admin_area.get("crs")
    if crs and crs.get("properties", {}).get("name") not in (
        "EPSG:4326", "urn:ogc:def:crs:OGC:1.3:CRS84", "urn:ogc:def:crs:EPSG::4326",
    ):
        raise ValueError("admin_area must use WGS84 coordinates")
    polygon = shape(admin_area["geometry"])
    if polygon.geom_type not in ("Polygon", "MultiPolygon") or not polygon.is_valid or polygon.is_empty:
        raise ValueError("admin_area must contain a valid non-empty polygon")
    if any(not math.isfinite(v) for v in polygon.bounds) or not (
        -180 <= polygon.bounds[0] <= polygon.bounds[2] <= 180
        and -90 <= polygon.bounds[1] <= polygon.bounds[3] <= 90
    ):
        raise ValueError("admin_area coordinates must be WGS84 longitude/latitude")
    return polygon


def build_overture_catalog(records, admin_area, *, observed_at, existing=None) -> PlaceCatalog:
    """Compatibility API; delegates to the same pipeline used for persistence."""
    return _build_catalog(records, admin_area, administrative_polygon(admin_area),
                          observed_at=observed_at, existing=existing)


def _build_catalog(records, admin_area, polygon, *, observed_at, existing=None):
    properties = admin_area["properties"]
    # Arrow input: decode once, apply bbox before nested Python materialization.
    if isinstance(records, pa.Table):
        points = from_wkb(records["geometry"].to_pylist(), on_invalid="ignore")
        latitudes, longitudes = get_y(points), get_x(points)
        valid = (get_type_id(points) == 0) & np.isfinite(latitudes) & np.isfinite(longitudes)
        west, south, east, north = polygon.bounds
        indices = np.flatnonzero(valid & (longitudes >= west) & (longitudes <= east)
                                 & (latitudes >= south) & (latitudes <= north))
        bbox_records = records.take(pa.array(indices, type=pa.int64())).to_pylist()
        candidates = zip(bbox_records, points[indices])
    else:
        def decoded():
            for record in records:
                geometry = record.get("geometry")
                try:
                    point = wkb.loads(bytes(geometry)) if isinstance(geometry, (bytes, bytearray, memoryview)) else shape(geometry)
                except (TypeError, ValueError, AttributeError, GEOSException):
                    continue
                if not isinstance(point, Point) or point.is_empty:
                    continue
                lng, lat = point.x, point.y
                if not (math.isfinite(lat) and math.isfinite(lng) and -90 <= lat <= 90 and -180 <= lng <= 180):
                    continue
                west, south, east, north = polygon.bounds
                if west <= lng <= east and south <= lat <= north:
                    yield record, point
        candidates = decoded()

    counts = dict(raw_bbox=0, food_filtered=0, inside_polygon=0, deduplicated=0, canonical_persisted=0)
    winners = {}
    for record, point in candidates:
        counts["raw_bbox"] += 1
        if not is_food_place(record):
            continue
        counts["food_filtered"] += 1
        if not polygon.covers(point):
            continue
        counts["inside_polygon"] += 1
        source_id = record.get("id")
        if not isinstance(source_id, str) or not source_id.strip():
            continue
        # Last accepted occurrence wins; first occurrence determines row order.
        # Identical to the previous dictionary overwrite, before normalization.
        winners[f"overture:{source_id}"] = (record, point.x, point.y)

    accepted, raw_payloads = {}, {}
    for key, (record, lng, lat) in winners.items():
        source_id = record["id"]
        previous = (existing or {}).get(key)
        names = record.get("names") or {}
        addresses = record.get("addresses") or []
        address = next((a.get("freeform") for a in addresses if a.get("freeform")), None)
        place = CanonicalPlace(
            place_key=key, source="overture", source_place_id=source_id,
            name=names.get("primary"), lat=lat, lng=lng, address=address,
            primary_category=get_primary_category(record), categories=normalize_categories(record),
            category_hierarchy=get_category_hierarchy(record),
            product_category_group=get_product_category_group(record), category_source="overture",
            confidence=record.get("confidence"), admin_area_id=properties["admin_area_id"],
            admin_area_name=properties["admin_area_name"],
            first_seen_at=previous.first_seen_at if previous else observed_at, last_seen_at=observed_at,
        )
        if previous:
            place = place.with_google_enrichment(
                place_id=previous.google_place_id, match_status=previous.google_match_status,
                match_score=previous.google_match_score, categories=previous.google_categories,
            )
        accepted[key] = place
        raw_payloads[key] = deepcopy(dict(record))
    counts["deduplicated"] = len(winners)
    counts["canonical_persisted"] = len(accepted)
    return PlaceCatalog(list(accepted.values()), raw_payloads, counts)


def _schema():
    lists = {"categories", "category_hierarchy", "google_categories"}
    numbers = {"lat", "lng", "confidence", "google_match_score"}
    return pa.schema([(f.name, pa.list_(pa.string()) if f.name in lists else
                       pa.float64() if f.name in numbers else pa.string())
                      for f in fields(CanonicalPlace)])


def manifest_path(path):
    return Path(path).with_suffix(".manifest.json")


def publication_root(path):
    """The old Parquet path remains a logical catalog handle, never a dual write."""
    return Path(path).with_name(Path(path).name + ".catalog")


def catalog_exists(path):
    return ((publication_root(path) / "current.json").exists() or Path(path).exists()
            or manifest_path(path).exists())


def _json_bytes(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


@contextmanager
def _publication_lock(path):
    root = publication_root(path)
    root.mkdir(parents=True, exist_ok=True)
    lock = root / ".publication.lock"
    try:
        stream = lock.open("x", encoding="utf-8")
    except FileExistsError:
        raise ValueError("Catalog publication locked; manual stale-lock review required") from None
    try:
        with stream:
            stream.write(str(os.getpid()))
        yield root
    finally:
        lock.unlink()


def _pin_catalog(path):
    """Resolve current exactly once; historical pairs are read-only fallback."""
    path = Path(path)
    root = publication_root(path)
    pointer = root / "current.json"
    if pointer.exists():
        current = json.loads(pointer.read_bytes())
        generation = current.get("generation")
        if (current.get("publication_version") != 1 or not isinstance(generation, str)
                or len(generation) != 64 or any(c not in "0123456789abcdef" for c in generation)
                or current.get("manifest_sha256") != generation):
            raise ValueError("Invalid catalog generation pointer")
        path = root / "generations" / generation / "catalog.parquet"
        metadata = manifest_path(path).read_bytes()
        if hashlib.sha256(metadata).hexdigest() != generation:
            raise ValueError("Catalog manifest checksum mismatch")
    else:
        if not path.exists() or not manifest_path(path).exists():
            raise FileNotFoundError(f"Canonical catalog missing: {path}; run python -m fnb_platform.prepare_catalog first")
        metadata = manifest_path(path).read_bytes()
    return path, json.loads(metadata)


def persist_catalog(catalog, path, manifest, *, fault=lambda phase: None):
    """Publish one immutable generation; current-pointer replacement commits it."""
    with _publication_lock(path) as root:
        return _publish_catalog(catalog, path, manifest, root, fault=fault)


def _publish_catalog(catalog, path, manifest, root, *, fault=lambda phase: None):
    # Never silently repair an invalid active generation or historical pair.
    if catalog_exists(path):
        load_catalog(path)
    metadata = dict(manifest, schema_version=SCHEMA_VERSION, counts=catalog.counts,
                    dedup_policy="last accepted occurrence wins; first identity occurrence orders rows")
    stage = root / "staging" / uuid.uuid4().hex
    stage.mkdir(parents=True)
    artifact = stage / "catalog.parquet"
    pq.write_table(pa.Table.from_pylist([p.to_dict() for p in catalog.places], schema=_schema()),
                   artifact, compression="zstd")
    metadata["artifact_sha256"] = hashlib.sha256(artifact.read_bytes()).hexdigest()
    manifest_bytes = _json_bytes(metadata)
    manifest_path(artifact).write_bytes(manifest_bytes)
    _read_catalog(artifact, metadata)
    generation = hashlib.sha256(manifest_bytes).hexdigest()
    destination = root / "generations" / generation
    if destination.exists():
        saved = manifest_path(destination / "catalog.parquet").read_bytes()
        if saved != manifest_bytes:
            raise ValueError("Existing catalog generation manifest checksum mismatch")
        _read_catalog(destination / "catalog.parquet", json.loads(saved))
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        fault("before_release_rename")
        os.replace(stage, destination)
    fault("after_release_rename")
    # Verify at the final path too (e.g. Windows path/access failures after rename).
    published_manifest = manifest_path(destination / "catalog.parquet").read_bytes()
    if published_manifest != manifest_bytes:
        raise ValueError("Published catalog manifest checksum mismatch")
    _read_catalog(destination / "catalog.parquet", json.loads(published_manifest))
    pointer_bytes = _json_bytes(dict(publication_version=1, generation=generation,
                                   manifest_sha256=generation))
    pointer = root / "current.json"
    if not pointer.exists() or pointer.read_bytes() != pointer_bytes:
        temporary = root / (".current-" + uuid.uuid4().hex + ".tmp")
        temporary.write_bytes(pointer_bytes)
        fault("before_pointer_replace")
        os.replace(temporary, pointer)
    fault("after_pointer_replace")
    return metadata


def load_catalog(path=DEFAULT_CATALOG):
    """Read typed persisted objects only: no taxonomy, polygon, raw or network."""
    artifact, manifest = _pin_catalog(path)
    return _read_catalog(artifact, manifest)


def _read_catalog(path, manifest):
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Unsupported canonical catalog schema version")
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != manifest.get("artifact_sha256"):
        raise ValueError("Canonical catalog/manifest checksum mismatch")
    table = pq.ParquetFile(pa.BufferReader(content)).read()
    if table.schema != _schema():
        raise ValueError("Canonical catalog fields/types are incompatible")
    places = [CanonicalPlace.from_dict(row) for row in table.to_pylist()]
    if len(places) != manifest["counts"]["canonical_persisted"] or len({p.place_key for p in places}) != len(places):
        raise ValueError("Canonical catalog row count or identities are invalid")
    return PlaceCatalog(places, {}, manifest["counts"])


def prepare_catalog(source, ward, output, lineage, *, observed_at, reader=None):
    """Cached mode reads a snapshot; explicit reader receives the polygon bbox."""
    ward = Path(ward)
    admin_area = json.loads(ward.read_text(encoding="utf-8"))
    polygon = administrative_polygon(admin_area)
    bbox = tuple(polygon.bounds)
    release = lineage.get("source_release") or lineage.get("release")
    if not release:
        raise ValueError("Actual Overture release is required in source metadata")
    if reader is None:
        table = pq.ParquetFile(source).read()
    else:
        table = reader(bbox=bbox, release=release)
    properties = admin_area["properties"]
    metadata = dict(
        overture_release=release, overturemaps_client_version=lineage.get("client_version"),
        retrieved_at=lineage.get("retrieved_at"), generated_at=observed_at,
        source_path=str(source) if reader is None else None,
        source_url=lineage.get("source_catalog") or lineage.get("source_url"),
        attribution=lineage.get("license_attribution") or lineage.get("attribution"),
        source_mode="bbox_query" if reader else "cached_snapshot_bbox_filter",
        admin_area_id=properties["admin_area_id"], admin_area_name=properties["admin_area_name"],
        boundary_source=properties.get("source"), boundary_osm_id=properties.get("osm_id"),
        boundary_valid_from=properties.get("valid_from"),
        boundary_sha256=hashlib.sha256(json.dumps(admin_area["geometry"], sort_keys=True).encode()).hexdigest(),
        bbox=list(bbox),
    )
    # Serialize read/merge/publish so a concurrent enrichment cannot be lost.
    with _publication_lock(output) as root:
        previous = {p.place_key: p for p in load_catalog(output).places} if catalog_exists(output) else None
        catalog = _build_catalog(table, admin_area, polygon, observed_at=observed_at, existing=previous)
        return catalog, _publish_catalog(catalog, output, metadata, root)
