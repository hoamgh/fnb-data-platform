"""Offline M1 input, eligibility and publication failure-path contracts."""
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from shapely.geometry import Point

from experiments.overture_gold_contract import eligibility as e


COUNTS = {"silver_rows": 4, "inside_ward": 3, "original_candidates": 1,
          "hierarchy_additions": 1, "candidates": 2, "missing_from_release": 1}


def seal(root):
    silver = root / e.SILVER_ROOT / "releases" / e.RELEASE
    manifest = e.read_json(silver / "manifest.json")
    for name in manifest["artifacts"]:
        table = pq.read_table(silver / name)
        manifest["artifacts"][name] = {"sha256": e.m242.sha(silver / name),
                                       "rows": table.num_rows, "fields": table.schema.names}
    e.write_json(silver / "manifest.json", manifest)
    e.write_json(root / e.SILVER_ROOT / "current.json",
                 {"release_id": e.RELEASE, "manifest_sha256": e.m242.sha(silver / "manifest.json")})
    paths = [root / e.HIST, silver / "current_state.parquet", root / e.SOURCE_DIR / "new.parquet", root / e.BOUNDARY]
    e.write_json(root / e.REFERENCE, {"release": e.RELEASE,
                  "inputs_sha256": {p.relative_to(root).as_posix(): e.m242.sha(p) for p in paths}})


@pytest.fixture
def inputs(tmp_path):
    root = tmp_path / "inputs"
    source = root / e.SOURCE_DIR
    silver = root / e.SILVER_ROOT / "releases" / e.RELEASE
    for p in [source, silver, (root / e.HIST).parent, (root / e.REFERENCE).parent, (root / e.BOUNDARY).parent]:
        p.mkdir(parents=True, exist_ok=True)
    for name in [e.BOUNDARY, Path("config/admin_areas/phuong_khanh_hoi_metadata.json")]:
        (root / name).write_bytes((e.ROOT / name).read_bytes())
    inside = e.consumer.load_ward().representative_point()
    rows = []
    for key, primary, point in [("original", "cafe", inside), ("expanded", "sushi_restaurant", inside),
                                 ("outside", "cafe", Point(0, 0)), ("uncertain", None, inside)]:
        rows.append({"id": key, "names": {"primary": "Fixture " + key}, "basic_category": None,
                     "taxonomy": {"primary": primary, "hierarchy": ["food_and_drink", primary] if primary else [], "alternates": []},
                     "operating_status": None, "addresses": [], "geometry": point.wkb, "confidence": .8})
    schema = pa.schema([("id", pa.string()), ("names", pa.struct([("primary", pa.string())])),
                        ("basic_category", pa.string()), ("taxonomy", pa.struct([
                            ("primary", pa.string()), ("hierarchy", pa.list_(pa.string())), ("alternates", pa.list_(pa.string()))])),
                        ("operating_status", pa.string()), ("addresses", pa.list_(pa.struct([("freeform", pa.string())]))),
                        ("geometry", pa.binary()), ("confidence", pa.float64())])
    raw = pa.Table.from_pylist(rows, schema=schema)
    pq.write_table(raw, source / "new.parquet")
    pq.write_table(raw.slice(0, 2), root / e.HIST)
    scope = {"spatial_filter": "fixture polygon.covers"}
    e.write_json(source / "manifest.json", {"new_release": e.RELEASE, "filter_scope": scope,
                 "output_sha256": {"new.parquet": e.m242.sha(source / "new.parquet")}})
    normalized = e.consumer.make_silver({r["id"]: r for r in rows}, e.RELEASE, "bronze-fixture-hash", scope)
    pq.write_table(pa.Table.from_pylist(normalized), silver / "current_state.parquet")
    missing = pa.Table.from_pylist([{"source_id": "missing", "source_release": e.RELEASE,
                                    "source_presence": "MISSING_FROM_RELEASE", "verified_business_status": "UNKNOWN",
                                    "explanation": "ABSENT_IN_NEW_BUFFER_UNRESOLVED"}])
    pq.write_table(missing, silver / "missing_or_out_of_scope.parquet")
    e.write_json(silver / "dq_metrics.json", {"fixture": True})
    e.write_json(silver / "manifest.json", {"release_id": e.RELEASE, "scope": scope,
                 "source_manifest_sha256": e.m242.sha(source / "manifest.json"), "bronze_manifest_sha256": "bronze-fixture-hash",
                 "ward_polygon_sha256": e.m242.sha(root / e.BOUNDARY),
                 "ward_metadata_sha256": e.m242.sha(root / "config/admin_areas/phuong_khanh_hoi_metadata.json"),
                 "dq_metrics_sha256": e.m242.sha(silver / "dq_metrics.json"), "metrics": {"fixture": True},
                 "artifacts": {"current_state.parquet": {}, "missing_or_out_of_scope.parquet": {}}})
    seal(root)
    return root


def publish(inputs, output, **kwargs):
    return e.publish(inputs, output, expected_counts=COUNTS, **kwargs)


def test_provisional_partition_and_byte_identical_rerun(inputs, tmp_path):
    before = {str(p): e.m242.sha(p) for p in inputs.rglob("*") if p.is_file()}
    output = tmp_path / "published"
    path, manifest = publish(inputs, output)
    assert manifest["metrics"]["counts"] == COUNTS
    rows = pq.read_table(path / "candidates.parquet").to_pylist()
    assert {r["eligibility_basis"] for r in rows} == {"ORIGINAL_FILTER", "HIERARCHY_EXTENSION"}
    assert all(r["identity_status"] == "UNVERIFIED" and r["verified_business_status"] == "UNKNOWN" for r in rows)
    assert all(r["address"] is None for r in rows)
    assert all(r["candidate_status"] == "PROVISIONAL" for r in rows)
    assert pq.read_table(path / "eligibility.parquet").num_rows == 4
    old = {str(p): (e.m242.sha(p), p.stat().st_mtime_ns) for p in output.rglob("*") if p.is_file()}
    assert publish(inputs, output)[0] == path
    assert old == {str(p): (e.m242.sha(p), p.stat().st_mtime_ns) for p in output.rglob("*") if p.is_file()}
    assert before == {str(p): e.m242.sha(p) for p in inputs.rglob("*") if p.is_file()}


@pytest.mark.parametrize("relative", [e.SOURCE_DIR / "new.parquet", e.HIST, e.BOUNDARY])
def test_input_checksum_failure_never_publishes(inputs, tmp_path, relative):
    with (inputs / relative).open("ab") as f:
        f.write(b"corrupted")
    with pytest.raises(ValueError, match="checksum"):
        publish(inputs, tmp_path / "out")
    assert not (tmp_path / "out/current.json").exists()


@pytest.mark.parametrize("case", ["duplicate", "missing_id", "wrong_geography", "wrong_normalization"])
def test_invalid_silver_even_with_updated_manifest_is_rejected(inputs, tmp_path, case):
    path = inputs / e.SILVER_ROOT / "releases" / e.RELEASE / "current_state.parquet"
    table = pq.read_table(path)
    rows = table.to_pylist()
    if case == "duplicate": rows.append(rows[0])
    if case == "missing_id": rows[0]["source_id"] = None
    if case == "wrong_geography": rows[0]["inside_ward"] = False
    if case == "wrong_normalization": rows[0]["name_normalized"] = "invented alias"
    pq.write_table(pa.Table.from_pylist(rows, schema=table.schema), path)
    seal(inputs)
    with pytest.raises(ValueError, match="IDs|normalization"):
        publish(inputs, tmp_path / "out")


def test_changed_baseline_requires_review(inputs, tmp_path):
    with pytest.raises(ValueError, match="Baseline counts"):
        e.publish(inputs, tmp_path / "out")


@pytest.mark.parametrize("stage", ["after_stage_validation", "after_release_rename", "before_pointer_replace"])
def test_failure_preserves_active_and_retry_recovers(inputs, tmp_path, monkeypatch, stage):
    output = tmp_path / "out"
    old_path, _ = publish(inputs, output)
    pointer = (output / "current.json").read_bytes()
    monkeypatch.setattr(e, "POLICY", "fixture-next-policy")
    def fail(at):
        if at == stage: raise RuntimeError("injected interruption")
    with pytest.raises(RuntimeError, match="interruption"):
        publish(inputs, output, fault=fail)
    assert (output / "current.json").read_bytes() == pointer
    assert e.read_active(output)[0] == old_path
    assert not (output / ".publish.lock").exists()
    new_path, _ = publish(inputs, output)
    assert new_path != old_path and old_path.exists()
    assert e.read_active(output)[0] == new_path


def test_existing_corrupt_publication_is_not_overwritten(inputs, tmp_path):
    output = tmp_path / "out"
    path, _ = publish(inputs, output)
    with (path / "candidates.parquet").open("ab") as f: f.write(b"corrupted")
    pointer = (output / "current.json").read_bytes()
    with pytest.raises(ValueError, match="Published checksum"):
        publish(inputs, output)
    assert (output / "current.json").read_bytes() == pointer


def test_input_change_during_publication_is_rejected(inputs, tmp_path):
    def mutate(stage):
        if stage == "after_stage_validation":
            with (inputs / e.HIST).open("ab") as f: f.write(b"changed")
    output = tmp_path / "out"
    with pytest.raises(ValueError, match="Input changed"):
        publish(inputs, output, fault=mutate)
    assert not (output / "current.json").exists()


def test_atomic_pointer_replace_failure_keeps_old_generation(inputs, tmp_path, monkeypatch):
    output = tmp_path / "out"
    old_path, _ = publish(inputs, output)
    before = (output / "current.json").read_bytes()
    monkeypatch.setattr(e, "POLICY", "fixture-policy-update")
    real_replace = e.os.replace
    def fail_pointer(source, target):
        if Path(target).name == "current.json": raise OSError("injected rename failure")
        return real_replace(source, target)
    monkeypatch.setattr(e.os, "replace", fail_pointer)
    with pytest.raises(OSError, match="rename failure"):
        publish(inputs, output)
    assert (output / "current.json").read_bytes() == before
    assert e.read_active(output)[0] == old_path
    monkeypatch.setattr(e.os, "replace", real_replace)
    assert publish(inputs, output)[0] != old_path


def test_exclusive_writer_and_read_only_root_guard(inputs, tmp_path):
    with pytest.raises(ValueError, match="separate"):
        publish(inputs, inputs / "output/new")
    output = tmp_path / "out"
    output.mkdir()
    (output / ".publish.lock").write_text("existing owner")
    with pytest.raises(FileExistsError): publish(inputs, output)
    assert (output / ".publish.lock").read_text() == "existing owner"


@pytest.mark.parametrize("status,expected", [(None, True), ("open", True), ("temporarily_closed", False), ("permanently_closed", False)])
def test_existing_operating_status_policy_is_preserved(status, expected):
    assert e.m242.eligibility(True, "INCLUDE_HIERARCHY_FNB", "NONE", status)[0] is expected


@pytest.mark.parametrize("taxonomy,reason", [
    ({"primary": "cafe", "hierarchy": ["food_and_drink", "cafe"]}, "INCLUDE_HIERARCHY_FNB"),
    ({"primary": "cafe", "hierarchy": ["food_and_drink", "bar"]}, "REVIEW_INVALID_TAXONOMY_PATH"),
    ({"primary": "shop", "hierarchy": ["shop"], "alternates": ["food_and_drink"]}, "REVIEW_ALTERNATE_FNB"),
    ({}, "REVIEW_MISSING_TAXONOMY"),
    ({"primary": "shop", "hierarchy": ["shop"]}, "EXCLUDE_NON_FNB_HIERARCHY")])
def test_existing_taxonomy_rules_are_reused(taxonomy, reason):
    assert e.m242.taxonomy_decision({"taxonomy": taxonomy}) == reason


def publish_silver_fixture(inputs, monkeypatch, missing, complete):
    """Run the real Silver publisher on a pinned, offline comparison fixture."""
    c = e.consumer
    source = inputs / e.SOURCE_DIR
    raw = pq.read_table(source / "new.parquet")
    rows = raw.to_pylist()
    for row in rows:
        row["confidence"] = None
        if complete:
            row["addresses"] = [{"freeform": "Fixture address"}]
            if row["taxonomy"]["primary"] is None:
                row["taxonomy"] = {"primary": "shop", "hierarchy": ["shop"], "alternates": []}
    raw = pa.Table.from_pylist(rows, schema=raw.schema)
    pq.write_table(raw, source / "new.parquet")
    old_rows = rows + ([dict(rows[0], id="missing")] if missing else [])
    pq.write_table(pa.Table.from_pylist(old_rows, schema=raw.schema), source / "old.parquet")
    scope = {"spatial_filter": "fixture polygon.covers"}
    counts = {"NEW": 0, "UPDATED": 0, "UNCHANGED": len(rows), "MISSING_FROM_RELEASE": int(missing)}
    (source / c.BOUNDARY_ARTIFACT).write_bytes((inputs / e.BOUNDARY).read_bytes())
    boundary_input = {"polygon": c.checksum(source / c.BOUNDARY_ARTIFACT)}
    source_manifest = {
        "old_release": "2026-09-23.1", "new_release": e.RELEASE, "filter_scope": scope,
        "input_sha256": boundary_input, "boundary_artifact": c.BOUNDARY_ARTIFACT,
        "output_sha256": {name: c.checksum(source / name) for name in ("old.parquet", "new.parquet", c.BOUNDARY_ARTIFACT)},
        "rows": {"old": {"within_buffer": len(old_rows)}, "new": {"within_buffer": len(rows)}},
    }
    c.write_json(source / "manifest.json", source_manifest)
    publication = inputs / "fixture-bronze"
    release = publication / "releases" / e.RELEASE
    release.mkdir(parents=True)
    change_schema = pa.schema([
        ("id", pa.string()), ("status", pa.string()), ("changed_fields", pa.list_(pa.string())),
        ("substantive_change", pa.bool_()), ("explanation", pa.string()),
    ])
    artifacts = {}
    for group in c.bronze.CHANGE_SETS:
        changes = ([dict(id="missing", status="MISSING_FROM_RELEASE", changed_fields=[],
                         substantive_change=False, explanation="ABSENT_IN_NEW_BUFFER_UNRESOLVED")]
                   if missing and group == "missing_or_out_of_scope" else [])
        path = release / (group + ".parquet")
        pq.write_table(pa.Table.from_pylist(changes, schema=change_schema), path)
        artifacts[path.name] = {"sha256": c.checksum(path), "rows": len(changes)}
    c.write_json(release / "manifest.json", {
        "old_release": source_manifest["old_release"], "new_release": e.RELEASE,
        "source_manifest_sha256": c.checksum(source / "manifest.json"),
        "source_input_sha256": boundary_input, "scope": scope, "counts": counts,
        "schema_fields": change_schema.names, "artifacts": artifacts,
    })
    c.write_json(publication / "current.json", {
        "release_id": e.RELEASE, "manifest_sha256": c.checksum(release / "manifest.json"),
    })
    monkeypatch.setattr(e, "SILVER_ROOT", Path("fixture-silver"))
    directory, manifest = c.publish(source, publication, inputs / e.SILVER_ROOT)
    # Pin the new fixture artifacts for the existing eligibility consumer.
    paths = [inputs / e.HIST, directory / "current_state.parquet", source / "new.parquet", inputs / e.BOUNDARY]
    e.write_json(inputs / e.REFERENCE, {"release": e.RELEASE,
                 "inputs_sha256": {p.relative_to(inputs).as_posix(): e.m242.sha(p) for p in paths}})
    return directory, manifest, source, publication


@pytest.mark.parametrize("missing", [False, True])
@pytest.mark.parametrize("complete", [False, True])
def test_silver_explicit_schemas_roundtrip_to_eligibility(inputs, tmp_path, monkeypatch, missing, complete):
    c = e.consumer
    directory, manifest, source, publication = publish_silver_fixture(inputs, monkeypatch, missing, complete)
    current = pq.read_table(directory / "current_state.parquet")
    absent = pq.read_table(directory / "missing_or_out_of_scope.parquet")
    assert current.schema == c.CURRENT_STATE_SCHEMA
    assert absent.schema == c.MISSING_STATE_SCHEMA
    assert all(field.nullable for field in current.schema)
    assert all(field.nullable for field in absent.schema)
    assert current.schema.field("source_id").type == pa.string()
    assert current.schema.field("confidence").type == pa.float64()
    assert current.schema.field("inside_ward").type == pa.bool_()
    assert current.schema.field("dq_reasons").type == pa.list_(pa.string())
    assert current.column("confidence").to_pylist() == [None] * 4
    assert current.column("source_operating_status").to_pylist() == [None] * 4
    assert current.column("source_id").to_pylist() == ["expanded", "original", "outside", "uncertain"]
    if complete:
        assert current.column("dq_reasons").to_pylist() == [[], [], [], []]
    else:
        assert current.column("address").to_pylist() == [None] * 4
    assert absent.column("source_id").to_pylist() == (["missing"] if missing else [])
    if missing:
        assert absent.to_pylist()[0] == {
            "source_id": "missing", "source_release": e.RELEASE,
            "source_presence": "MISSING_FROM_RELEASE", "verified_business_status": "UNKNOWN",
            "explanation": "ABSENT_IN_NEW_BUFFER_UNRESOLVED",
        }
    counts = dict(COUNTS, missing_from_release=int(missing))
    target, _ = e.publish(inputs, tmp_path / "eligibility", expected_counts=counts)
    assert pq.read_table(target / "missing_from_release.parquet").equals(absent)
    assert pq.read_table(target / "candidates.parquet").column("source_id").to_pylist() == ["expanded", "original"]
    hashes = {name: c.checksum(directory / name) for name in manifest["artifacts"]}
    assert c.publish(source, publication, inputs / e.SILVER_ROOT)[0] == directory
    assert hashes == {name: c.checksum(directory / name) for name in hashes}


def test_historical_inferred_nulls_verify_without_rewriting(inputs):
    c = e.consumer
    directory = inputs / e.SILVER_ROOT / "releases" / e.RELEASE
    path = directory / "current_state.parquet"
    rows = pq.read_table(path).to_pylist()
    for row in rows:
        for name in c.LEGACY_NULL_FIELDS:
            row[name] = None
        row["dq_reasons"] = []
    legacy = pa.Table.from_pylist(rows)
    assert legacy.schema.field("address").type == pa.null()
    assert legacy.schema.field("dq_reasons").type == pa.list_(pa.null())
    pq.write_table(legacy, path)
    seal(inputs)
    before = {p: (e.m242.sha(p), p.stat().st_mtime_ns) for p in directory.iterdir() if p.is_file()}
    c.verify_silver(directory)
    assert before == {p: (e.m242.sha(p), p.stat().st_mtime_ns) for p in before}


@pytest.mark.parametrize("case", ["wrong_id_type", "wrong_optional_type", "wrong_list_type", "missing_column", "legacy_columnless"])
def test_silver_rejects_incompatible_schema_even_with_resealed_manifest(inputs, case):
    c = e.consumer
    directory = inputs / e.SILVER_ROOT / "releases" / e.RELEASE
    path = directory / "current_state.parquet"
    table = pq.read_table(path)
    if case == "missing_column":
        table = table.drop(["name_normalized"])
    elif case == "legacy_columnless":
        path = directory / "missing_or_out_of_scope.parquet"
        table = pa.Table.from_pylist([])
    else:
        name, values = {"wrong_id_type": ("source_id", [1, 2, 3, 4]),
                        "wrong_optional_type": ("address", [1, 2, 3, 4]),
                        "wrong_list_type": ("dq_reasons", [[1]] * 4)}[case]
        table = table.set_column(table.schema.get_field_index(name), name, pa.array(values))
    pq.write_table(table, path)
    seal(inputs)
    with pytest.raises(c.SilverError, match="required columns|field type"):
        c.verify_silver(directory)
