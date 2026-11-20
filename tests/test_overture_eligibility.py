"""Offline M1 input, eligibility and publication failure-path contracts."""
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from shapely.geometry import Point

from fnb_platform.overture import eligibility as e


COUNTS = {"silver_rows": 4, "inside_ward": 3, "original_candidates": 1,
          "hierarchy_additions": 1, "candidates": 2, "missing_from_release": 1}


def test_eligibility_legacy_module_policy_and_monkeypatch_compatibility(monkeypatch):
    import importlib
    from experiments.overture_gold_contract import m242, spatial_prededup
    from scripts import google_m2_batch
    from fnb_platform.overture import silver
    legacy = importlib.import_module('experiments.overture_gold_contract.eligibility')
    assert legacy is e and spatial_prededup.e is e and google_m2_batch.e is e
    assert not hasattr(e, 'm242') and e.consumer is silver
    assert m242._policy is e.policy
    root = Path(__file__).resolve().parents[1]
    assert e.ROOT == root
    assert Path(e.__file__).resolve() == root/'src/fnb_platform/overture/eligibility.py'
    assert e.BOUNDARY == Path('config/admin_areas/phuong_khanh_hoi.geojson')
    monkeypatch.setattr(legacy, 'POLICY', 'fixture-monkeypatch-policy')
    assert e.build_tables.__globals__['POLICY'] == legacy.POLICY


@pytest.mark.parametrize('filename', ['src/fnb_platform/overture/eligibility.py',
                                    'src/fnb_platform/overture/eligibility_policy.py',
                                    'experiments/overture_gold_contract/eligibility.py'])
def test_campaign_fingerprint_covers_eligibility_and_wrapper(monkeypatch, filename):
    import hashlib
    from scripts import google_m2_batch as batch
    before = batch.campaign_code_fingerprints()
    assert {'src/fnb_platform/overture/eligibility.py',
            'src/fnb_platform/overture/eligibility_policy.py',
            'experiments/overture_gold_contract/eligibility.py'} <= {name.replace('\\','/') for name in before}
    target = e.ROOT/filename
    checksum = e.sha
    monkeypatch.setattr(e, 'sha', lambda path:
        hashlib.sha256(Path(path).read_bytes()+b'\n# simulated change').hexdigest()
        if Path(path) == target else checksum(path))
    after = batch.campaign_code_fingerprints()
    assert [name.replace('\\','/') for name in before if before[name] != after[name]] == [filename]


@pytest.mark.parametrize('legacy', [True, False])
def test_eligibility_old_and_new_cli_help(legacy):
    import os
    import subprocess
    import sys
    env = dict(os.environ)
    if legacy:
        env.pop('PYTHONPATH', None)
        command = [sys.executable,'-B',str(e.ROOT/'experiments/overture_gold_contract/eligibility.py'),'--help']
    else:
        env['PYTHONPATH'] = str(e.ROOT/'src')
        command = [sys.executable,'-B','-m','fnb_platform.overture.eligibility','--help']
    result = subprocess.run(command,cwd=e.ROOT,env=env,capture_output=True,text=True,timeout=30)
    assert result.returncode == 0, result.stderr
    assert '--input-root' in result.stdout and '--output-root' in result.stdout


def test_canonical_eligibility_import_has_no_historical_code_dependency(tmp_path):
    import os
    import subprocess
    import sys
    env = dict(os.environ, PYTHONPATH=str(e.ROOT/'src'))
    code = ('import sys; from fnb_platform.overture import eligibility as e; '
            'assert not hasattr(e,"m242"); '
            'assert not any(n=="experiments" or n.startswith("experiments.") for n in sys.modules); '
            'assert e.policy.__name__=="fnb_platform.overture.eligibility_policy"')
    result = subprocess.run([sys.executable,'-B','-c',code],cwd=tmp_path,env=env,
                            capture_output=True,text=True,timeout=30)
    assert result.returncode == 0, result.stderr


def test_historical_audit_preserves_policy_geometry_hooks_and_defaults(inputs, monkeypatch):
    from experiments.overture_gold_contract import m242
    paths = dict(historical_path=inputs/e.HIST,
                 silver_path=inputs/e.SILVER_ROOT/'releases'/e.RELEASE/'current_state.parquet',
                 source_path=inputs/e.SOURCE_DIR/'new.parquet',boundary_path=inputs/e.BOUNDARY)
    expected = e.analyze(**paths)
    assert m242.HIST == e.ROOT/e.HIST
    assert m242.SILVER == e.ROOT/e.SILVER_ROOT/'releases'/e.RELEASE/'current_state.parquet'
    assert m242.SOURCE == e.ROOT/e.SOURCE_DIR/'new.parquet'
    assert m242.BOUNDARY == e.ROOT/e.BOUNDARY
    assert m242.analyze.__kwdefaults__ == dict(historical_path=m242.HIST,
        silver_path=m242.SILVER,source_path=m242.SOURCE,boundary_path=m242.BOUNDARY)
    calls = {}
    for name in ('taxonomy_decision','eligibility','historical_inside','from_wkb','shape'):
        original = getattr(m242,name)
        def observed(*args,_name=name,_original=original,**kwargs):
            calls[_name] = calls.get(_name,0)+1
            return _original(*args,**kwargs)
        monkeypatch.setattr(m242,name,observed)
    assert m242.analyze(**paths) == expected
    assert set(calls) == {'taxonomy_decision','eligibility','historical_inside','from_wkb','shape'}
    before = dict(calls)
    assert e.analyze(**paths) == expected
    assert calls == before


def test_policy_module_is_the_single_live_eligibility_owner(inputs, monkeypatch):
    from experiments.overture_gold_contract import m242
    paths = dict(historical_path=inputs/e.HIST,
                 silver_path=inputs/e.SILVER_ROOT/'releases'/e.RELEASE/'current_state.parquet',
                 source_path=inputs/e.SOURCE_DIR/'new.parquet',boundary_path=inputs/e.BOUNDARY)
    monkeypatch.setattr(e.policy,'eligibility',lambda *args,**kwargs:(False,'FIXTURE_POLICY_HOLD'))
    for analyze in (e.analyze,m242.analyze):
        projected = analyze(**paths)[-1]
        assert projected and all(not row['gold_eligible'] and row['eligibility_reason']=='FIXTURE_POLICY_HOLD' for row in projected)


def seal(root):
    silver = root / e.SILVER_ROOT / "releases" / e.RELEASE
    manifest = e.read_json(silver / "manifest.json")
    for name in manifest["artifacts"]:
        table = pq.read_table(silver / name)
        manifest["artifacts"][name] = {"sha256": e.sha(silver / name),
                                       "rows": table.num_rows, "fields": table.schema.names}
    e.write_json(silver / "manifest.json", manifest)
    e.write_json(root / e.SILVER_ROOT / "current.json",
                 {"release_id": e.RELEASE, "manifest_sha256": e.sha(silver / "manifest.json")})
    paths = [root / e.HIST, silver / "current_state.parquet", root / e.SOURCE_DIR / "new.parquet", root / e.BOUNDARY]
    e.write_json(root / e.REFERENCE, {"release": e.RELEASE,
                  "inputs_sha256": {p.relative_to(root).as_posix(): e.sha(p) for p in paths}})


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
                 "output_sha256": {"new.parquet": e.sha(source / "new.parquet")}})
    normalized = e.consumer.make_silver({r["id"]: r for r in rows}, e.RELEASE, "bronze-fixture-hash", scope)
    pq.write_table(pa.Table.from_pylist(normalized), silver / "current_state.parquet")
    missing = pa.Table.from_pylist([{"source_id": "missing", "source_release": e.RELEASE,
                                    "source_presence": "MISSING_FROM_RELEASE", "verified_business_status": "UNKNOWN",
                                    "explanation": "ABSENT_IN_NEW_BUFFER_UNRESOLVED"}])
    pq.write_table(missing, silver / "missing_or_out_of_scope.parquet")
    e.write_json(silver / "dq_metrics.json", {"fixture": True})
    e.write_json(silver / "manifest.json", {"release_id": e.RELEASE, "scope": scope,
                 "source_manifest_sha256": e.sha(source / "manifest.json"), "bronze_manifest_sha256": "bronze-fixture-hash",
                 "ward_polygon_sha256": e.sha(root / e.BOUNDARY),
                 "ward_metadata_sha256": e.sha(root / "config/admin_areas/phuong_khanh_hoi_metadata.json"),
                 "dq_metrics_sha256": e.sha(silver / "dq_metrics.json"), "metrics": {"fixture": True},
                 "artifacts": {"current_state.parquet": {}, "missing_or_out_of_scope.parquet": {}}})
    seal(root)
    return root


def publish(inputs, output, **kwargs):
    return e.publish(inputs, output, expected_counts=COUNTS, **kwargs)


def test_spatial_publication_pins_canonical_and_legacy_eligibility_without_collision(inputs, tmp_path, monkeypatch):
    from experiments.overture_gold_contract import spatial_prededup
    monkeypatch.setattr(e, 'BASELINE', COUNTS)
    # Preserve the output-root guard inside the isolated fixture project.
    monkeypatch.setattr(spatial_prededup, 'ROOT', tmp_path)
    summary = spatial_prededup.run(inputs, tmp_path/'output/spatial')
    hashes = summary['code_sha256']
    wrapper = 'experiments/overture_gold_contract/eligibility.py'
    assert hashes['eligibility.py'] == e.sha(Path(e.__file__))
    assert hashes[wrapper] == e.sha(e.ROOT/wrapper)
    assert hashes['eligibility_policy.py'] == e.sha(Path(e.policy.__file__))
    assert hashes['eligibility.py'] != hashes[wrapper]
    assert summary['counts'] == COUNTS


def test_provisional_partition_and_byte_identical_rerun(inputs, tmp_path):
    before = {str(p): e.sha(p) for p in inputs.rglob("*") if p.is_file()}
    output = tmp_path / "published"
    path, manifest = publish(inputs, output)
    assert {'src/fnb_platform/overture/__init__.py',
            'src/fnb_platform/overture/comparison_run.py',
            'src/fnb_platform/overture/comparison_publication.py',
            'src/fnb_platform/overture/silver.py'} <= set(manifest['signature']['code_sha256'])
    assert 'experiments/overture_silver/consumer.py' in manifest['signature']['code_sha256']
    assert manifest['signature']['code_sha256']['src/fnb_platform/overture/eligibility.py'] == e.sha(Path(e.__file__))
    assert manifest['signature']['code_sha256']['src/fnb_platform/overture/eligibility_policy.py'] == e.sha(Path(e.policy.__file__))
    assert manifest['signature']['code_sha256']['src/fnb_platform/overture/snapshot.py'] == e.sha(e.ROOT/'src/fnb_platform/overture/snapshot.py')
    assert 'experiments/overture_gold_contract/eligibility.py' in manifest['signature']['code_sha256']
    assert manifest["metrics"]["counts"] == COUNTS
    rows = pq.read_table(path / "candidates.parquet").to_pylist()
    assert {r["eligibility_basis"] for r in rows} == {"ORIGINAL_FILTER", "HIERARCHY_EXTENSION"}
    assert all(r["identity_status"] == "UNVERIFIED" and r["verified_business_status"] == "UNKNOWN" for r in rows)
    assert all(r["address"] is None for r in rows)
    assert all(r["candidate_status"] == "PROVISIONAL" for r in rows)
    assert pq.read_table(path / "eligibility.parquet").num_rows == 4
    old = {str(p): (e.sha(p), p.stat().st_mtime_ns) for p in output.rglob("*") if p.is_file()}
    assert publish(inputs, output)[0] == path
    assert old == {str(p): (e.sha(p), p.stat().st_mtime_ns) for p in output.rglob("*") if p.is_file()}
    assert before == {str(p): e.sha(p) for p in inputs.rglob("*") if p.is_file()}


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
    assert e.policy.eligibility(True, "INCLUDE_HIERARCHY_FNB", "NONE", status)[0] is expected


@pytest.mark.parametrize("taxonomy,reason", [
    ({"primary": "cafe", "hierarchy": ["food_and_drink", "cafe"]}, "INCLUDE_HIERARCHY_FNB"),
    ({"primary": "cafe", "hierarchy": ["food_and_drink", "bar"]}, "REVIEW_INVALID_TAXONOMY_PATH"),
    ({"primary": "shop", "hierarchy": ["shop"], "alternates": ["food_and_drink"]}, "REVIEW_ALTERNATE_FNB"),
    ({}, "REVIEW_MISSING_TAXONOMY"),
    ({"primary": "shop", "hierarchy": ["shop"]}, "EXCLUDE_NON_FNB_HIERARCHY")])
def test_existing_taxonomy_rules_are_reused(taxonomy, reason):
    assert e.policy.taxonomy_decision({"taxonomy": taxonomy}) == reason


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
                 "inputs_sha256": {p.relative_to(inputs).as_posix(): e.sha(p) for p in paths}})
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
    before = {p: (e.sha(p), p.stat().st_mtime_ns) for p in directory.iterdir() if p.is_file()}
    c.verify_silver(directory)
    assert before == {p: (e.sha(p), p.stat().st_mtime_ns) for p in before}


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
