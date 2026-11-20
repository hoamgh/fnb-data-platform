import csv
from copy import deepcopy
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from fnb_platform import category_quality as c, google_silver as s
from fnb_platform.overture_incremental import _digest


MAPPING = c.load_mapping()
REFS = {k: dict(path=k + ".parquet", sha256="fixture-checksum") for k in ("overture", "places", "er")}


def ov(primary="restaurant", oid="ov1", hierarchy=None, alternates=None, **extra):
    return dict(source_id=oid, name="Source fixture", address=None, taxonomy_primary=primary,
        taxonomy_hierarchy=hierarchy if hierarchy is not None else ["food_and_drink", primary],
        taxonomy_alternates=alternates or [], basic_category=primary, is_fnb_candidate=True,
        inside_ward=True, source_release="fixture", source_manifest_sha256="fixture", **extra)


def google(category="Nhà hàng", gid="0x1:0x2"):
    return dict(google_id_kind="data_id", google_identity_value=gid, name_display="Google fixture",
        address_display=None, category_display=category, category_normalized=s.normalized(category) if isinstance(category, str) else None,
        field_provenance_json="{}", conflicting_values_json="{}", metadata_status="AVAILABLE")


def er(oid="ov1", gid="0x1:0x2", classification="MATCH_CANDIDATE"):
    return dict(overture_id=oid, google_id_kind="data_id", google_identity_value=gid,
        classification=classification, er_status="ER_PENDING", rule_version=c.ER_VERSION,
        observation_ref="fixture.parquet#composite-key", reason="Fixture identity evidence")


def accepted(oid="ov1", gid="0x1:0x2", store="store-fixture"):
    return dict(overture_id=oid, google_id_kind="data_id", google_identity_value=gid,
        store_id=store, identity_status="MATCH", decision_ref="independent-fixture-decision",
        mapping_version="fixture-v1")


def build(o=None, g=None, links=None, mappings=()):
    return c.build(o if o is not None else [ov()], g if g is not None else [google()],
        links if links is not None else [er()], MAPPING, REFS, accepted_mappings=mappings)


@pytest.mark.parametrize("primary,category,expected", [
    ("restaurant", "Nhà hàng", "CONSISTENT"),
    ("restaurant", "clothing store", "CATEGORY_CONFLICT"),
    ("cafe", "coffee shop", "CONSISTENT"),
    ("bakery", "Quán cà phê", "SUBCATEGORY_DIFFERENCE"),
    ("restaurant", None, "SOURCE_ONLY"),
])
def test_cross_source_comparisons(primary, category, expected):
    tables, _ = build([ov(primary)], [google(category)])
    row = tables["category_audit.parquet"].to_pylist()[0]
    assert row["category_relation"] == expected
    assert row["store_id"] is None and row["canonical_category"] == "unknown"
    assert row["classification_scope"] == "PROVISIONAL"
    if expected == "CATEGORY_CONFLICT":
        assert row["comparison_status"] == "NEEDS_IDENTITY_RECHECK"
    assert "ER_PENDING" in row["reason"]


def test_unrecognized_not_other_food_and_no_record_dropped():
    tables, summary = build([ov("unrecognized_food")], [google("Label not in dictionary")])
    rows = tables["category_normalization.parquet"].to_pylist()
    assert all(r["normalized_categories"] == ["unknown"] for r in rows)
    assert rows[0]["candidate_classification"] == "FNB_CANDIDATE"  # explicit food ancestor only
    assert rows[0]["baseline_fnb_candidate"] is True
    assert summary["coverage"]["baseline_candidates"] == 1
    assert all("UNRECOGNIZED_CATEGORY" in r["dq_flags"] for r in rows)


def test_multiple_categories_and_ancestors_do_not_duplicate_subtypes():
    tables, _ = build([ov("bakery", alternates=["cafe", "bakery"])], [google(["bakery", "coffee shop"])])
    rows = tables["category_normalization.parquet"].to_pylist()
    assert all(r["normalized_categories"] == ["bakery_dessert", "cafe_coffee"] for r in rows)
    a = tables["category_audit.parquet"].to_pylist()[0]
    assert a["category_relation"] == "CONSISTENT" and a["canonical_category"] == "unknown"


def test_source_category_conflict_and_invalid_path():
    r = ov("restaurant", hierarchy=["shopping", "clothing_store"], alternates=["clothing_store"])
    row = c.normalize_source(r, "overture", MAPPING, REFS["overture"])
    assert row["candidate_classification"] == "UNKNOWN_CATEGORY"
    assert "CONFLICTING_SOURCE_CATEGORIES" in row["dq_flags"]
    assert "INVALID_TAXONOMY_PATH" in row["dq_flags"]
    assert row["baseline_fnb_candidate"] is True


def test_missing_invalid_and_unknown_are_distinct_warnings():
    for value, flag in ((None, "MISSING_CATEGORY"), (42, "INVALID_CATEGORY_VALUE"), ("never-seen", "UNRECOGNIZED_CATEGORY")):
        row = c.normalize_source(google(value), "google", MAPPING, REFS["places"])
        assert flag in row["dq_flags"] and row["normalized_categories"] == ["unknown"]
        if flag == "INVALID_CATEGORY_VALUE":
            assert "MISSING_CATEGORY" not in row["dq_flags"]
        detail = next(d for d in json.loads(row["dq_details_json"]) if d["flag_code"] == flag)
        assert all(detail[k] for k in ("reason", "evidence_json", "observation_ref", "rule_version", "recommended_action"))
        assert json.loads(detail["evidence_json"])["raw_categories"]["category_display"] == value


@pytest.mark.parametrize("category", ["Nhà sản xuất thực phẩm", "Phòng trà", "Cửa hàng bán buôn"])
def test_ambiguous_food_or_generic_business_labels_remain_unknown(category):
    row = c.normalize_source(google(category), "google", MAPPING, REFS["places"])
    assert row["candidate_classification"] == "UNKNOWN_CATEGORY"


def test_stored_normalization_disagreement_and_historical_flags_block_selection():
    g = google(); g["category_normalized"] = "clothing store"
    g["conflicting_values_json"] = json.dumps({"category_display": ["Nhà hàng", "Cửa hàng quần áo"]})
    tables, _ = build(g=[g], mappings=[accepted()])
    row = tables["category_normalization.parquet"].to_pylist()[1]
    assert "CATEGORY_DISPLAY_NORMALIZED_DISAGREEMENT" in row["dq_flags"]
    assert "HISTORICAL_CATEGORY_METADATA_CONFLICT" in row["dq_flags"]
    audit = tables["category_audit.parquet"].to_pylist()[0]
    assert audit["category_relation"] == "CONSISTENT"  # historical values aren't current labels
    assert audit["canonical_category"] == "unknown"


def test_invalid_google_identity_and_missing_source_reference_fail_closed():
    g = google(gid="not-a-google-id")
    with pytest.raises(ValueError, match="Invalid Google typed identity"):
        build(g=[g], links=[er(gid="not-a-google-id")])
    with pytest.raises(ValueError, match="missing source record"):
        build(g=[], links=[er()])


def test_excluded_taxonomy_name_hint_is_not_a_category_or_eligibility_change():
    r = ov("clothing_store", hierarchy=["shopping", "clothing_store"])
    r.update(name="Nhà hàng fixture", is_fnb_candidate=False)
    tables, summary = build([r], [], [])
    row = tables["category_normalization.parquet"].to_pylist()[0]
    assert "FNB_NAME_HINT_TAXONOMY_EXCLUDED" in row["dq_flags"]
    assert row["normalized_categories"] == ["non_fnb"]
    assert row["baseline_fnb_candidate"] is False
    assert summary["coverage"]["normalized_rows"] == 1


def test_same_place_different_business_is_not_confirmed_by_category():
    g = google(); g["name_display"] = "Different business"
    row = build(g=[g], links=[er(classification="NON_MATCH_CANDIDATE")])[0]["category_audit.parquet"].to_pylist()[0]
    assert row["category_relation"] == "CONSISTENT"
    assert row["comparison_status"] == "NEEDS_IDENTITY_RECHECK"
    assert row["er_status"] == "ER_PENDING" and row["store_id"] is None


def test_shared_google_and_multiple_candidates_do_not_merge():
    tables, _ = build([ov(), ov(oid="ov2")], [google(), google(gid="0x3:0x4")],
        [er(), er("ov2"), er(gid="0x3:0x4")])
    audits = tables["category_audit.parquet"].to_pylist()
    assert len(audits) == 3
    assert all(r["comparison_status"] == "NEEDS_IDENTITY_RECHECK" for r in audits)
    assert all(r["store_id"] is None for r in audits)
    assert {r["overture_id"] for r in audits} == {"ov1", "ov2"}
    assert "ov2" in audits[0]["evidence_json"]


def test_accepted_mapping_contract_only_with_independent_provenance():
    row = build(mappings=[accepted()])[0]["category_audit.parquet"].to_pylist()[0]
    assert row["store_id"] == "store-fixture"
    assert row["canonical_category"] == "restaurant"
    assert row["category_source"] == "overture+google"
    assert row["classification_scope"] == "VERIFIED_IDENTITY"
    assert row["category_validation_status"] == "SOURCE_AGREEMENT_ACCEPTED_IDENTITY"
    assert json.loads(row["category_provenance"])["accepted_mapping"]["decision_ref"]
    bad = accepted(); bad["identity_status"] = "MATCH_CANDIDATE"
    with pytest.raises(ValueError, match="explicit MATCH"):
        build(mappings=[bad])


def test_accepted_identity_does_not_resolve_category_conflict():
    row = build(g=[google("clothing store")], mappings=[accepted()])[0]["category_audit.parquet"].to_pylist()[0]
    assert row["comparison_status"] == "CATEGORY_CONFLICT"
    assert row["canonical_category"] == "unknown" and row["category_provenance"] is None


def test_store_category_conflict_across_accepted_links_blocks_selection():
    tables, _ = build([ov(), ov("cafe", oid="ov2")], [google(), google("coffee shop", gid="0x3:0x4")],
        [er(), er("ov2", "0x3:0x4")], [accepted(), accepted("ov2", "0x3:0x4")])
    assert all(r["canonical_category"] == "unknown" for r in tables["category_audit.parquet"].to_pylist())


def test_inconsistent_accepted_store_mapping_is_hard_failure():
    with pytest.raises(ValueError, match="Inconsistent canonical"):
        build([ov(), ov(oid="ov2")], [google()], [er(), er("ov2")],
            [accepted(), accepted("ov2", store="other-store")])


@pytest.mark.parametrize("mutation", ["category", "provenance", "conflict"])
def test_gold_contract_refuses_invalid_or_unsupported_selection(mutation):
    tables, _ = build(mappings=[accepted()])
    normal = tables["category_normalization.parquet"].to_pylist()
    audits = tables["category_audit.parquet"].to_pylist()
    if mutation == "category":
        audits[0]["canonical_category"] = "invented-category"
    elif mutation == "provenance":
        audits[0]["category_provenance"] = None
    else:
        audits[0]["category_relation"] = "CATEGORY_CONFLICT"
    with pytest.raises(ValueError):
        c.validate(normal, audits, {"ov1"}, {("data_id", "0x1:0x2")},
            {("ov1", "data_id", "0x1:0x2")}, {("ov1", "data_id", "0x1:0x2"): accepted()})


def test_optional_missing_category_preserves_unlinked_source_records():
    tables, summary = build([ov(), ov(oid="ov2")], [google(None)], [er()])
    assert len(tables["category_audit.parquet"]) == 2
    assert summary["coverage"]["baseline_zero_google_candidates"] == 1
    assert summary["hard_failures"] == 0


def test_evidence_raw_values_provenance_and_reordering_determinism():
    a = ov(); g = google()
    a["name"] = "Tên gốc"; a["address"] = "158-160 Hoàng Diệu"
    before = deepcopy(([a], [g], [er()]))
    tables, _ = build(*before)
    row = tables["category_audit.parquet"].to_pylist()[0]
    assert row["overture_address"] == a["address"] and row["overture_name"] == a["name"]
    assert row["reason"] and json.loads(row["evidence_json"])["overture"]["raw"]["taxonomy_primary"] == "restaurant"
    assert json.loads(row["source_provenance_json"])["google"]["input"] == REFS["places"]
    assert before == ([a], [g], [er()])
    other, _ = build(*before)
    assert all(_digest(t.to_pylist()) == _digest(other[k].to_pylist()) for k, t in tables.items())
    data = ([ov(), ov(oid="ov2")], [google(), google(gid="0x3:0x4")], [er(), er("ov2", "0x3:0x4")])
    x, _ = build(*data); y, _ = build(*[list(reversed(rows)) for rows in data])
    assert all(t.equals(y[k]) for k, t in x.items())


@pytest.mark.parametrize("part", ["overture", "google", "er"])
def test_duplicate_keys_are_hard_failures(part):
    o, g, e = [ov()], [google()], [er()]
    {"overture": o, "google": g, "er": e}[part].append(deepcopy({"overture": o, "google": g, "er": e}[part][0]))
    with pytest.raises(ValueError, match="Duplicate"):
        build(o, g, e)


def write_release(root, artifacts, signature):
    rid = _digest(signature)[:24]; d = root / "releases" / rid; d.mkdir(parents=True)
    specs = {}
    for name, rows in artifacts.items():
        path = d / name; path.parent.mkdir(parents=True, exist_ok=True)
        schema = {"silver/google_places.parquet": s.PLACE_SCHEMA,
                  "silver/source_links.parquet": s.LINK_SCHEMA}.get(name)
        t = pa.Table.from_pylist(rows, schema=schema); pq.write_table(t, path)
        if schema is not None:
            assert pq.ParquetFile(path).schema_arrow.equals(schema)
        specs[name] = dict(rows=t.num_rows, sha256=s.sha(path), logical_sha256=_digest(t.to_pylist()), schema=str(t.schema))
    (d / "manifest.json").write_text(s.dump(dict(release_id=rid, signature=signature, artifacts=specs)), encoding="utf-8")
    (root / "current.json").write_text(s.dump(dict(release_id=rid, manifest_sha256=s.sha(d / "manifest.json"))), encoding="utf-8")
    return d


def publication_fixture(tmp_path, monkeypatch):
    from experiments.overture_gold_contract import eligibility
    raw = tmp_path / "original" / "current_state.parquet"; raw.parent.mkdir()
    o = ov(); o.update(eligibility_reason="fixture", eligibility_policy_version="fixture")
    pq.write_table(pa.Table.from_pylist([{k: o[k] for k in ("source_id", "name", "address", "taxonomy_primary", "basic_category")}]), raw)
    eroot, groot, rroot = [tmp_path / s for s in ("eligibility", "google", "er")]
    ed = eroot / "releases" / "fixture"; ed.mkdir(parents=True)
    pq.write_table(pa.Table.from_pylist([o]), ed / "eligibility.parquet")
    em = dict(run_id="fixture", signature=dict(inputs_sha256={"output/overture-silver-m241/releases/2026-10-21.0/current_state.parquet": s.sha(raw)}), metrics=dict(counts=dict(candidates=1)))
    (ed / "manifest.json").write_text(s.dump(em)); (eroot / "current.json").write_text("{}")
    monkeypatch.setattr(eligibility, "read_active", lambda root: (ed, em))
    g = google(); g.update(current_observation_id=None, observed_at=None, source_run=None)
    gd = write_release(groot, {"silver/google_places.parquet": [g], "silver/source_links.parquet": [er()]}, dict(inputs_sha256={str(raw): s.sha(raw)}))
    hashes = {str(p): s.sha(p) for p in (raw, gd / "silver/google_places.parquet", gd / "silver/source_links.parquet")}
    write_release(rroot, {"er_results.parquet": [er()]}, dict(rule_version=c.ER_VERSION, inputs_sha256=hashes))
    mapping = tmp_path / "mapping.json"; mapping.write_bytes(c.DEFAULT_MAPPING.read_bytes())
    return eroot, groot, rroot, tmp_path / "derived", mapping


def test_publication_rerun_atomic_failure_and_checksums(tmp_path, monkeypatch):
    args = publication_fixture(tmp_path, monkeypatch)
    dest, summary = c.publish(*args)
    from fnb_platform.overture import eligibility_policy
    policy_path = str(Path(eligibility_policy.__file__).resolve())
    assert s.verify_release(dest)['signature']['code_sha256'][policy_path] == s.sha(policy_path)
    before = (args[3] / "current.json").read_bytes()
    dest2, summary2 = c.publish(*args)
    assert dest == dest2 and summary == summary2
    assert s.read_active(args[3]) == dest
    assert summary["coverage"]["normalized_rows"] == 2
    assert list(csv.DictReader((dest / "category_conflicts_for_review.csv").open(encoding="utf-8-sig"))) == []
    m = json.loads(args[4].read_text()); m["mapping_version"] = "fixture-v2"; args[4].write_text(json.dumps(m))
    for fail_at in ("after_stage_validation", "before_pointer_replace"):
        def fault(stage):
            if stage == fail_at:
                raise RuntimeError("injected failure")
        with pytest.raises(RuntimeError, match="injected"):
            c.publish(*args, fault=fault)
        assert (args[3] / "current.json").read_bytes() == before
        assert s.read_active(args[3]) == dest
    assert not list(args[3].glob(".stage-*"))
    assert not (args[3] / ".publish.lock").exists()


def test_publication_rejects_lineage_mismatch(tmp_path, monkeypatch):
    args = publication_fixture(tmp_path, monkeypatch)
    # A valid checksum on a different Overture snapshot must still fail lineage.
    root = args[1]; d = s.read_active(root); m = s.verify_release(d)
    path = next(iter(m["signature"]["inputs_sha256"])); Path(path).write_bytes(b"changed")
    with pytest.raises(ValueError, match="lineage mismatch"):
        c.publish(*args)
