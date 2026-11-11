"""Historical catalog API; transformations and publication live in fnb_platform."""
from dataclasses import dataclass, field
from typing import Any

from fnb_platform import catalog as shared_catalog
from fnb_platform.catalog import (
    ROOT, DEFAULT_CATALOG, DEFAULT_WARD, SCHEMA_VERSION,
    administrative_polygon, manifest_path,
)
from crawl_experiment.core.place_catalog import CanonicalPlace

# These are historical inputs, deliberately distinct from fnb_platform defaults.
DEFAULT_RAW = ROOT / "experiments/poi_source_benchmark/output/poi_source_benchmark/overture_raw.parquet"
DEFAULT_SOURCE = ROOT / "experiments/overture_food_poi/output/overture_hcm_places.parquet"
DEFAULT_SOURCE_METADATA = DEFAULT_SOURCE.with_name("overture_food_summary.json")


@dataclass
class PlaceCatalog:
    places: list[CanonicalPlace]
    raw_payloads: dict[str, dict[str, Any]]
    counts: dict[str, int] = field(default_factory=dict)


def _legacy_result(catalog):
    return PlaceCatalog([CanonicalPlace.from_dict(p.to_dict()) for p in catalog.places],
                        catalog.raw_payloads, catalog.counts)


def build_overture_catalog(records, admin_area, *, observed_at, existing=None) -> PlaceCatalog:
    return _legacy_result(shared_catalog.build_overture_catalog(
        records, admin_area, observed_at=observed_at, existing=existing))


def persist_catalog(catalog, path, manifest):
    return shared_catalog.persist_catalog(catalog, path, manifest)


def load_catalog(path=DEFAULT_CATALOG):
    return _legacy_result(shared_catalog.load_catalog(path))


def prepare_catalog(source, ward, output, lineage, *, observed_at, reader=None):
    catalog, metadata = shared_catalog.prepare_catalog(
        source, ward, output, lineage, observed_at=observed_at, reader=reader)
    return _legacy_result(catalog), metadata
