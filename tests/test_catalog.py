import json
from copy import deepcopy

import pytest
from shapely.geometry import Point

from fnb_platform.models import CanonicalPlace
from fnb_platform.categories import (
    get_primary_category, get_category_hierarchy, get_product_category_group,
    is_food_place, normalize_categories,
)
from fnb_platform import catalog as storage
from fnb_platform.catalog import build_overture_catalog


def record(category="vietnamese_restaurant", source_id="one", xy=(1, 1)):
    return {
        "id": source_id, "names": {"primary": "Example"},
        "geometry": Point(*xy).wkb, "confidence": 0.8,
        "addresses": [{"freeform": "Outside ward address text"}],
        "taxonomy": {"primary": category, "hierarchy": [
            "food_and_drink", "restaurant", category,
        ]},
    }


def ward():
    return {"type": "Feature", "properties": {
        "admin_area_id": "ward-1", "admin_area_name": "Current ward", "is_current": True,
        "valid_to": None,
    }, "geometry": {"type": "Polygon", "coordinates": [
        [[0, 0], [2, 0], [2, 2], [0, 2], [0, 0]],
        [[1.4, 1.4], [1.6, 1.4], [1.6, 1.6], [1.4, 1.6], [1.4, 1.4]],
    ]}}


def test_taxonomy_preservation_and_food_rule():
    raw = record()
    original = deepcopy(raw)
    assert is_food_place(raw)
    assert get_primary_category(raw) == "vietnamese_restaurant"
    assert get_category_hierarchy(raw) == raw["taxonomy"]["hierarchy"]
    assert normalize_categories(raw) == ["vietnamese_restaurant", "restaurant"]
    assert raw == original
    raw["taxonomy"]["hierarchy"] = ["shopping", "restaurant"]
    assert not is_food_place(raw)  # primary alone never includes a POI
    assert get_product_category_group(raw) is None
    assert not is_food_place({"taxonomy": None})
    assert get_primary_category({}) is None
    assert get_category_hierarchy({}) == []


@pytest.mark.parametrize("category,group", [
    ("vietnamese_restaurant", "restaurant"), ("seafood_restaurant", "restaurant"),
    ("coffee_shop", "cafe_coffee"), ("cafe", "cafe_coffee"),
    ("bakery", "bakery_dessert"), ("bubble_tea_shop", "beverage"),
    ("fast_food_restaurant", "fast_food"), ("bar", "bar"),
    ("future_unknown_food", "other_food"),
])
def test_explicit_groups(category, group):
    assert get_product_category_group(record(category)) == group


def test_no_invented_ancestors_or_duplicates():
    raw = record()
    raw["taxonomy"]["hierarchy"] = ["food_and_drink", "vietnamese_restaurant"]
    assert normalize_categories(raw) == ["vietnamese_restaurant"]
    raw["taxonomy"]["primary"] = None
    assert normalize_categories(raw) == ["vietnamese_restaurant"]
    assert get_product_category_group(raw) == "other_food"


def test_polygon_filter_not_address_and_raw_separation():
    inside = record()
    nonfood = record(source_id="nonfood")
    nonfood["taxonomy"]["hierarchy"] = ["shopping"]
    result = build_overture_catalog([
        inside, record(source_id="outside", xy=(3, 3)),
        record(source_id="hole", xy=(1.5, 1.5)), nonfood,
        record(source_id="boundary", xy=(0, 1)), inside,
    ], ward(), observed_at="2026-10-01T00:00:00Z")
    assert [p.source_place_id for p in result.places] == ["one", "boundary"]
    place = result.places[0]
    assert place.admin_area_id == "ward-1"
    assert place.category_source == "overture"
    assert place.address == "Outside ward address text"
    assert "taxonomy" not in place.to_dict()
    assert result.raw_payloads[place.place_key] == inside
    result.raw_payloads[place.place_key]["taxonomy"]["primary"] = "changed"
    assert inside["taxonomy"]["primary"] == "vietnamese_restaurant"


def test_serialization_reobservation_and_google_enrichment():
    first = build_overture_catalog([record()], ward(), observed_at="2026-09-30T00:00:00Z").places[0]
    enriched = first.with_google_enrichment(
        place_id="google-id", match_status="matched", match_score=0.9,
        categories=["Google-only category"],
    )
    restored = CanonicalPlace.from_dict(json.loads(json.dumps(enriched.to_dict())))
    assert restored == enriched
    second = build_overture_catalog([record()], ward(), observed_at="2026-10-01T00:00:00Z",
                                    existing={restored.place_key: restored}).places[0]
    assert second.first_seen_at == first.first_seen_at
    assert second.last_seen_at != first.last_seen_at
    assert second.google_categories == ["Google-only category"]
    assert second.primary_category == first.primary_category
    assert second.category_hierarchy == first.category_hierarchy


@pytest.mark.parametrize("geometry", [None, b"bad wkb", {"type": "Point", "coordinates": [999, 1]}])
def test_invalid_coordinate_exclusion(geometry):
    raw = record()
    raw["geometry"] = geometry
    assert build_overture_catalog([raw], ward(), observed_at="now").places == []


def test_historic_ward_rejected():
    area = ward()
    area["properties"]["is_current"] = False
    with pytest.raises(ValueError, match="current ward"):
        build_overture_catalog([], area, observed_at="now")


def test_multipolygon_and_crs():
    area = ward()
    area["geometry"] = {"type": "MultiPolygon", "coordinates": [area["geometry"]["coordinates"]]}
    assert len(build_overture_catalog([record()], area, observed_at="now").places) == 1
    area["crs"] = {"properties": {"name": "EPSG:3857"}}
    with pytest.raises(ValueError, match="WGS84"):
        build_overture_catalog([], area, observed_at="now")


def test_dedup_last_accepted_wins_before_normalization(monkeypatch):
    first, last = record(), record(category="coffee_shop")
    last["names"]["primary"] = "Last row"
    calls = []
    original = storage.normalize_categories
    monkeypatch.setattr(storage, "normalize_categories", lambda r: (calls.append(r["id"]), original(r))[1])
    catalog = build_overture_catalog([first, record(source_id="two"), last,
                                      record(source_id="one", xy=(3, 3))], ward(), observed_at="now")
    assert [p.source_place_id for p in catalog.places] == ["one", "two"]
    assert catalog.places[0].name == "Last row"
    assert calls == ["one", "two"]
    assert catalog.counts == dict(raw_bbox=3, food_filtered=3, inside_polygon=3,
                                  deduplicated=2, canonical_persisted=2)


def test_persisted_catalog_roundtrip_manifest_bbox_and_single_polygon_parse(monkeypatch, tmp_path):
    import pyarrow as pa
    import pyarrow.parquet as pq
    source, boundary, output = tmp_path / "raw.parquet", tmp_path / "ward.json", tmp_path / "canonical.parquet"
    nonfood = record(source_id="shopping")
    nonfood["taxonomy"]["hierarchy"] = ["shopping"]
    records = [record(), record(source_id="hole", xy=(1.5, 1.5)), record(source_id="outside", xy=(3, 3)),
               record(), nonfood]
    pq.write_table(pa.Table.from_pylist(records), source)
    boundary.write_text(json.dumps(ward()))
    lineage = dict(source_release="test-release", client_version="test-client", retrieved_at="retrieved")
    calls = []
    original = storage.administrative_polygon
    monkeypatch.setattr(storage, "administrative_polygon", lambda area: (calls.append(True), original(area))[1])
    catalog, manifest = storage.prepare_catalog(source, boundary, output, lineage, observed_at="now")
    assert len(calls) == 1
    assert manifest["bbox"] == [0, 0, 2, 2]
    assert manifest["counts"] == dict(raw_bbox=4, food_filtered=3, inside_polygon=2,
                                       deduplicated=1, canonical_persisted=1)
    assert manifest["overture_release"] == "test-release"
    assert manifest["overturemaps_client_version"] == "test-client"
    monkeypatch.setattr(storage, "_build_catalog", lambda *a, **kw: pytest.fail("load must not rebuild"))
    monkeypatch.setattr(storage, "administrative_polygon", lambda *a: pytest.fail("load must not parse polygon"))
    assert storage.load_catalog(output).places == catalog.places


def test_manifest_deterministic_empty_catalog_and_enrichment_update(tmp_path):
    import pyarrow as pa
    import pyarrow.parquet as pq
    source, boundary, output = tmp_path / "raw.parquet", tmp_path / "ward.json", tmp_path / "canonical.parquet"
    pq.write_table(pa.Table.from_pylist([record()]), source)
    boundary.write_text(json.dumps(ward()))
    lineage = dict(source_release="known", client_version="client", retrieved_at="retrieved")
    first, manifest = storage.prepare_catalog(source, boundary, output, lineage, observed_at="first")
    again, same = storage.prepare_catalog(source, boundary, output, lineage, observed_at="first")
    assert manifest == same and again.places == first.places
    enriched = first.places[0].with_google_enrichment(place_id="google", match_status="RESOLVED",
                                                    match_score=1.0, categories=["enrichment"])
    storage.persist_catalog(storage.PlaceCatalog([enriched], {}, first.counts), output, manifest)
    updated, _ = storage.prepare_catalog(source, boundary, output, lineage, observed_at="second")
    assert updated.places[0].first_seen_at == "first"
    assert updated.places[0].last_seen_at == "second"
    assert updated.places[0].google_place_id == "google"
    empty = storage.build_overture_catalog([], ward(), observed_at="now")
    storage.persist_catalog(empty, output, manifest)
    assert storage.load_catalog(output).places == []


def test_query_reader_receives_polygon_bbox_and_decodes_once(monkeypatch, tmp_path):
    import pyarrow as pa
    boundary, output = tmp_path / "ward.json", tmp_path / "catalog.parquet"
    boundary.write_text(json.dumps(ward()))
    calls, decoded = [], []
    original = storage.from_wkb
    monkeypatch.setattr(storage, "from_wkb", lambda values, **kwargs: (decoded.append(len(values)), original(values, **kwargs))[1])
    def reader(**kwargs):
        calls.append(kwargs)
        return pa.Table.from_pylist([record()])
    catalog, manifest = storage.prepare_catalog(None, boundary, output, {"release": "fixed"},
                                                observed_at="now", reader=reader)
    assert calls == [{"bbox": (0.0, 0.0, 2.0, 2.0), "release": "fixed"}]
    assert decoded == [1] and len(catalog.places) == 1
    assert manifest["source_mode"] == "bbox_query"


def test_catalog_load_fails_closed_on_corrupt_or_missing_manifest(tmp_path):
    output = tmp_path / "catalog.parquet"
    catalog = build_overture_catalog([record()], ward(), observed_at="now")
    storage.persist_catalog(catalog, output, {})
    artifact, _ = storage._pin_catalog(output)
    artifact.write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="checksum"):
        storage.load_catalog(output)
    storage.manifest_path(artifact).unlink()
    with pytest.raises(FileNotFoundError):
        storage.load_catalog(output)


def catalog_named(name):
    row = record()
    row['names']['primary'] = name
    return build_overture_catalog([row], ward(), observed_at='fixed')


def tree_bytes(path):
    return {p.relative_to(path): p.read_bytes() for p in path.rglob('*') if p.is_file()}


@pytest.mark.parametrize('phase,active_name', [
    ('before_release_rename', 'old'), ('after_release_rename', 'old'),
    ('before_pointer_replace', 'old'), ('after_pointer_replace', 'new'),
])
def test_atomic_catalog_faults_and_recovery(tmp_path, phase, active_name):
    output = tmp_path/'catalog.parquet'
    storage.persist_catalog(catalog_named('old'), output, {})
    old_artifact, _ = storage._pin_catalog(output)
    original = tree_bytes(old_artifact.parent)
    def crash(stage):
        if stage == phase:
            raise RuntimeError('injected crash')
    with pytest.raises(RuntimeError, match='injected'):
        storage.persist_catalog(catalog_named('new'), output, {}, fault=crash)
    assert storage.load_catalog(output).places[0].name == active_name
    assert tree_bytes(old_artifact.parent) == original
    storage.persist_catalog(catalog_named('new'), output, {})
    assert storage.load_catalog(output).places[0].name == 'new'
    assert tree_bytes(old_artifact.parent) == original
    assert len(list((storage.publication_root(output)/'generations').iterdir())) == 2


def test_same_input_reuses_generation_and_pointer(tmp_path):
    output = tmp_path/'catalog.parquet'
    catalog = catalog_named('same')
    first = storage.persist_catalog(catalog, output, {})
    root = storage.publication_root(output)
    pointer = root/'current.json'
    before, modified = pointer.read_bytes(), pointer.stat().st_mtime_ns
    artifact, _ = storage._pin_catalog(output)
    original = tree_bytes(artifact.parent)
    assert storage.persist_catalog(catalog, output, first) == first
    assert pointer.read_bytes() == before and pointer.stat().st_mtime_ns == modified
    assert tree_bytes(artifact.parent) == original
    assert len(list((root/'generations').iterdir())) == 1
    assert not output.exists()  # Never maintain an independently updated alias.


@pytest.mark.parametrize('corrupt', ['missing_manifest', 'manifest_checksum', 'artifact', 'pointer'])
def test_corrupt_active_generation_rejects_load_and_publish(tmp_path, corrupt):
    output = tmp_path/'catalog.parquet'
    storage.persist_catalog(catalog_named('old'), output, {})
    artifact, _ = storage._pin_catalog(output)
    if corrupt == 'missing_manifest':
        storage.manifest_path(artifact).unlink()
    elif corrupt == 'manifest_checksum':
        path = storage.manifest_path(artifact)
        path.write_bytes(path.read_bytes() + b' ')
    elif corrupt == 'artifact':
        artifact.write_bytes(b'bad parquet')
    else:
        (storage.publication_root(output)/'current.json').write_text('{bad json')
    before = tree_bytes(tmp_path)
    for action in (lambda: storage.load_catalog(output),
                   lambda: storage.persist_catalog(catalog_named('new'), output, {})):
        with pytest.raises((ValueError, FileNotFoundError)):
            action()
        assert tree_bytes(tmp_path) == before


def test_reader_pins_one_generation_during_activation(tmp_path, monkeypatch):
    output = tmp_path/'catalog.parquet'
    storage.persist_catalog(catalog_named('old'), output, {})
    original = storage._read_catalog
    switched = False
    def read_after_switch(path, manifest):
        nonlocal switched
        if not switched:
            switched = True
            storage.persist_catalog(catalog_named('new'), output, {})
        return original(path, manifest)
    monkeypatch.setattr(storage, '_read_catalog', read_after_switch)
    assert storage.load_catalog(output).places[0].name == 'old'
    assert storage.load_catalog(output).places[0].name == 'new'


def test_legacy_read_and_upgrade_preserve_historical_pair(tmp_path):
    import hashlib
    import pyarrow as pa
    import pyarrow.parquet as pq
    from crawl_experiment.storage import place_catalog as compatibility
    output = tmp_path/'catalog.parquet'
    old = catalog_named('historical')
    pq.write_table(pa.Table.from_pylist([p.to_dict() for p in old.places], schema=storage._schema()), output)
    storage.manifest_path(output).write_text(json.dumps(dict(schema_version=1, counts=old.counts,
        artifact_sha256=hashlib.sha256(output.read_bytes()).hexdigest())))
    before = (output.read_bytes(), storage.manifest_path(output).read_bytes())
    assert storage.load_catalog(output).places == old.places
    assert compatibility.load_catalog(output).places[0].name == 'historical'
    compatibility.persist_catalog(catalog_named('active'), output, {})
    assert compatibility.load_catalog(output).places[0].name == 'active'
    assert storage.load_catalog(output).places[0].name == 'active'
    assert (output.read_bytes(), storage.manifest_path(output).read_bytes()) == before


def test_partial_stage_and_orphan_never_implicitly_active(tmp_path):
    output = tmp_path/'catalog.parquet'
    def crash(phase):
        if phase == 'after_release_rename':
            raise RuntimeError('orphan')
    with pytest.raises(RuntimeError):
        storage.persist_catalog(catalog_named('orphan'), output, {}, fault=crash)
    with pytest.raises(FileNotFoundError):
        storage.load_catalog(output)
    assert not storage.catalog_exists(output)
    storage.persist_catalog(catalog_named('orphan'), output, {})
    assert storage.load_catalog(output).places[0].name == 'orphan'


def test_concurrent_writer_rejected_and_stale_lock_preserved(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    output = tmp_path/'catalog.parquet'
    storage.persist_catalog(catalog_named('old'), output, {})
    def competing_writer(phase):
        if phase == 'before_pointer_replace':
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(storage.persist_catalog, catalog_named('competitor'), output, {})
                with pytest.raises(ValueError, match='locked'):
                    future.result(timeout=5)
            assert storage.load_catalog(output).places[0].name == 'old'
    storage.persist_catalog(catalog_named('winner'), output, {}, fault=competing_writer)
    assert storage.load_catalog(output).places[0].name == 'winner'
    lock = storage.publication_root(output)/'.publication.lock'
    lock.write_text('uncertain owner')
    with pytest.raises(ValueError, match='manual stale-lock review'):
        storage.persist_catalog(catalog_named('blocked'), output, {})
    assert lock.read_text() == 'uncertain owner'


def test_corrupt_orphan_is_not_reused_or_repaired(tmp_path):
    output = tmp_path/'catalog.parquet'
    storage.persist_catalog(catalog_named('old'), output, {})
    old_artifact, _ = storage._pin_catalog(output)
    pointer = storage.publication_root(output)/'current.json'
    original = pointer.read_bytes()
    def crash(phase):
        if phase == 'after_release_rename':
            raise RuntimeError('orphan')
    with pytest.raises(RuntimeError):
        storage.persist_catalog(catalog_named('new'), output, {}, fault=crash)
    orphan = next(p for p in old_artifact.parent.parent.iterdir() if p != old_artifact.parent)
    (orphan/'catalog.parquet').write_bytes(b'corrupt')
    with pytest.raises(ValueError, match='checksum'):
        storage.persist_catalog(catalog_named('new'), output, {})
    assert (orphan/'catalog.parquet').read_bytes() == b'corrupt'
    assert pointer.read_bytes() == original
    assert storage.load_catalog(output).places[0].name == 'old'


def test_catalog_consumer_import_cli_and_sample_smoke(tmp_path):
    from crawl_experiment.runners import crawler_benchmark_runner as runner
    from fnb_platform import prepare_catalog as cli
    for entrypoint in (runner.main, cli.main):
        with pytest.raises(SystemExit) as exited:
            entrypoint(['--help'])
        assert exited.value.code == 0
    output = tmp_path/'catalog.parquet'
    storage.persist_catalog(catalog_named('selected'), output, {})
    sample = tmp_path/'sample.json'
    places, fingerprint = runner.load_sample(sample, 1, output)
    assert places[0].name == 'selected'
    storage.persist_catalog(catalog_named('later'), output, {})
    frozen, same = runner.load_sample(sample, 1, output)
    assert frozen == places and same == fingerprint  # Frozen samples stay frozen.


def test_legacy_model_schema_identity_serialization_and_taxonomy():
    import dataclasses
    import inspect
    import pickle
    from crawl_experiment.core.place_catalog import CanonicalPlace as LegacyPlace
    from crawl_experiment.core import place_categories as legacy_categories
    from fnb_platform import categories
    assert LegacyPlace is not CanonicalPlace
    assert LegacyPlace.__module__ == 'crawl_experiment.core.place_catalog'
    assert inspect.signature(LegacyPlace) == inspect.signature(CanonicalPlace)
    assert [(f.name, f.type) for f in dataclasses.fields(LegacyPlace)] == [
        (f.name, f.type) for f in dataclasses.fields(CanonicalPlace)]
    current = catalog_named('same').places[0]
    legacy = LegacyPlace.from_dict(current.to_dict())
    assert legacy != current  # Preserve class-specific dataclass equality.
    assert legacy.to_dict() == current.to_dict()
    assert pickle.loads(pickle.dumps(legacy)) == legacy
    enriched = legacy.with_google_enrichment(place_id='google', match_status='RESOLVED',
                                             match_score=.9, categories=['coffee'])
    assert type(enriched) is LegacyPlace and enriched.google_place_id == 'google'
    with pytest.raises(dataclasses.FrozenInstanceError):
        legacy.name = 'changed'
    assert legacy_categories.PRODUCT_CATEGORY_GROUPS == categories.PRODUCT_CATEGORY_GROUPS
    for function in ('get_primary_category', 'get_category_hierarchy', 'is_food_place',
                     'normalize_categories', 'get_product_category_group'):
        assert getattr(legacy_categories, function) is getattr(categories, function)


@pytest.mark.parametrize('arrow', [False, True])
def test_legacy_catalog_build_and_publish_match_canonical(tmp_path, arrow):
    import pyarrow as pa
    import pyarrow.parquet as pq
    from crawl_experiment.storage import place_catalog as legacy
    from crawl_experiment.core.place_catalog import CanonicalPlace as LegacyPlace
    rows = [record(), record(source_id='edge', xy=(0,1)),
            record(category='coffee_shop'), record(source_id='hole', xy=(1.5,1.5)),
            record(source_id='outside', xy=(3,3))]
    data = pa.Table.from_pylist(rows) if arrow else rows
    canonical = storage.build_overture_catalog(data, ward(), observed_at='same')
    compatible = legacy.build_overture_catalog(data, ward(), observed_at='same')
    assert type(compatible) is legacy.PlaceCatalog
    assert all(type(p) is LegacyPlace for p in compatible.places)
    assert [p.to_dict() for p in compatible.places] == [p.to_dict() for p in canonical.places]
    assert compatible.raw_payloads == canonical.raw_payloads and compatible.counts == canonical.counts
    assert [p.source_place_id for p in compatible.places] == ['one','edge']
    left, right = tmp_path/'canonical.parquet', tmp_path/'legacy.parquet'
    assert storage.persist_catalog(canonical, left, {}) == legacy.persist_catalog(compatible, right, {})
    a, _ = storage._pin_catalog(left)
    b, _ = storage._pin_catalog(right)
    assert pq.read_schema(a) == pq.read_schema(b) == storage._schema()
    assert a.read_bytes() == b.read_bytes()
    assert legacy.load_catalog(right) == compatible.__class__(compatible.places, {}, compatible.counts)
