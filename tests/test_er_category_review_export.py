from copy import deepcopy
import csv
import json

import pytest

from fnb_platform import category_quality as category, er_category_review_export as export, google_silver as silver


def fixtures():
    ov = []
    google = []
    er = []
    cases = [
        ("ov-review", "0x1:0x1", "REVIEW", "Nhà hàng", False),
        ("ov-conflict", "0x2:0x2", "MATCH_CANDIDATE", "Tiệm cắt tóc", False),
        ("ov-shared-a", "0x3:0x3", "MATCH_CANDIDATE", "Nhà hàng", False),
        ("ov-shared-b", "0x3:0x3", "MATCH_CANDIDATE", "Nhà hàng", False),
        ("ov-contradiction", "0x4:0x4", "REVIEW", "Nhà hàng", True),
        ("ov-clean", "0x5:0x5", "MATCH_CANDIDATE", "Nhà hàng", False),
    ]
    for oid, gid, classification, conflict in [(a, b, c, e) for a, b, c, d, e in cases]:
        ov.append(dict(source_id=oid, name="Tên gốc " + oid, address="122A Đường fixture", taxonomy_primary="restaurant",
            taxonomy_hierarchy=["food_and_drink", "restaurant"], taxonomy_alternates=[], basic_category="restaurant",
            is_fnb_candidate=True, inside_ward=True, source_release="source-fixture", source_manifest_sha256="source-hash"))
        evidence = dict(comparisons=dict(v3=dict(applied_rule="fixture-rule", review_reason_codes=["CURRENT_COMPONENT_CONFLICT"] if conflict else [],
            signals=dict(address="CONFLICT" if conflict else "SUPPORT"), address_features_v3=dict(clear_conflict=conflict,
            house_state="CONFLICT" if conflict else "SUPPORT", overture=dict(house="122A", street="fixture", original_parse_status="PARSED"),
            google=dict(house="128" if conflict else "122A", street="fixture", original_parse_status="PARSED")), name_features=dict(token_dice=0.9))),
            historical_m1_flags=["STREET_CONFLICT"], overture=dict(latitude=10.75, longitude=106.7), google=dict(latitude=10.7501, longitude=106.7))
        er.append(dict(overture_id=oid, google_id_kind="data_id", google_identity_value=gid,
            classification=classification, er_status="ER_PENDING", rule_version=category.ER_VERSION, reason="Recorded fixture reason",
            observation_ref="fixture#" + oid, evidence_json=silver.dump(evidence), flags_json="[]", distance_m=11.1, name_similarity=0.91))
    for oid, gid, classification, label, conflict in cases:
        if not any(g["google_identity_value"] == gid for g in google):
            google.append(dict(google_id_kind="data_id", google_identity_value=gid, name_display="Google " + gid,
                address_display="128 Đường fixture", category_display=label, category_normalized=silver.normalized(label),
                field_provenance_json="{}", conflicting_values_json="{}", source_url="https://www.google.com/maps/place/observed/"))
    ov.append(dict(source_id="ov-source-only", name="Nhà hàng chưa có taxonomy", address=None, taxonomy_primary=None,
        taxonomy_hierarchy=[], taxonomy_alternates=[], basic_category=None, is_fnb_candidate=False, inside_ward=True,
        source_release="source-fixture", source_manifest_sha256="source-hash"))
    refs = {k: dict(path=k + ".parquet", sha256="fixture-sha") for k in ("er", "overture", "places")}
    tables, _ = category.build(ov, google, er, category.load_mapping(), refs)
    context = dict(er_release_id="er-fixture", category_release_id="cat-fixture", google_silver_release_id="google-fixture", refs=refs)
    return er, tables["category_audit.parquet"].to_pylist(), tables["category_normalization.parquet"].to_pylist(), google, context


def test_coverage_dedup_grain_and_unlabelled_columns():
    data = fixtures(); original = deepcopy(data)
    identity, categories, summary = export.build(*data)
    assert len(identity) == 5  # two ER reviews + category conflict + two shared-ID warnings
    assert len(categories) == 2
    assert {r["overture_id"] for r in identity} == {"ov-review", "ov-conflict", "ov-shared-a", "ov-shared-b", "ov-contradiction"}
    assert {r["review_grain"] for r in categories} == {"LINK", "OVERTURE_SOURCE_ONLY"}
    assert all(r[f] == "" for r in identity for f in export.IDENTITY_LABELS)
    assert all(r[f] == "" for r in categories for f in export.CATEGORY_LABELS)
    assert summary["category"]["category_conflicts"] == 1 and summary["category"]["taxonomy_name_warnings"] == 1
    assert len({r["review_id"] for r in identity}) == len(identity)
    assert len({export.key(r) for r in categories}) == len(categories)
    assert data == original


def test_true_conflict_and_shared_warning_have_different_priorities():
    identity, _, _ = export.build(*fixtures())
    rows = {r["overture_id"]: r for r in identity}
    assert rows["ov-conflict"]["priority"] == "P1_TRUE_CATEGORY_CONFLICT"
    assert rows["ov-contradiction"]["priority"] == "P2_CURRENT_IDENTITY_CONTRADICTION"
    assert rows["ov-review"]["priority"] == "P3_ER_REVIEW"
    assert rows["ov-shared-a"]["priority"] == "P4_SHARED_ID_WARNING"
    # Historical flags do not become current conflicts.
    assert rows["ov-shared-b"]["priority"] == "P4_SHARED_ID_WARNING"


def test_stable_keys_versions_and_evidence_hash_changes():
    data = fixtures(); i, c, _ = export.build(*data)
    repeated = [list(reversed(rows)) for rows in data[:4]] + [data[4]]
    assert export.build(*repeated) == export.build(*data)
    assert all(r["er_release_id"] == "er-fixture" and r["category_release_id"] == "cat-fixture" for r in i + c)
    assert all(r["category_rule_version"] == category.VERSION and r["review_schema_version"] == export.VERSION for r in i + c)
    assert all(len(r["evidence_hash"]) == 64 and r["category_evidence_hash"] for r in i + c)
    before = {r["review_id"]: r["evidence_hash"] for r in i}
    data[0][0]["evidence_json"] = silver.dump(dict(comparisons=dict(v3=dict(applied_rule="new-evidence"))))
    after, _, _ = export.build(*data)
    changed = next(r for r in after if r["overture_id"] == "ov-review")
    assert changed["review_id"] in before and changed["evidence_hash"] != before[changed["review_id"]]


def test_raw_null_addresses_categories_and_hierarchy_preserved():
    _, rows, _ = export.build(*fixtures())
    source = next(r for r in rows if r["review_grain"] == "OVERTURE_SOURCE_ONLY")
    assert source["overture_address"] == "" and source["google_identity_value"] == ""
    assert source["google_maps_url"] == "" and source["google_categories_raw"] == ""
    assert json.loads(source["taxonomy_hierarchy"]) == []
    linked = next(r for r in rows if r["review_grain"] == "LINK")
    assert linked["overture_name"] == "Tên gốc ov-conflict"
    assert json.loads(linked["google_categories_raw"])["category_display"] == "Tiệm cắt tóc"
    assert json.loads(linked["source_evidence_json"])["overture"]["raw"]["taxonomy_primary"] == "restaurant"
    assert json.loads(linked["input_refs_json"])["er"]["sha256"] == "fixture-sha"


@pytest.mark.parametrize("bad_url", [None, "http://www.google.com/maps/a", "https://evil.test/maps/a", "https://www.google.com/search?q=test"])
def test_only_observed_maps_urls_are_used(bad_url):
    data = fixtures(); data[3][0]["source_url"] = bad_url
    rows, _, _ = export.build(*data)
    assert next(r for r in rows if r["overture_id"] == "ov-review")["google_maps_url"] == ""


def test_bad_versions_duplicate_and_dangling_records_fail_closed():
    data = fixtures(); data[0][0]["rule_version"] = "unknown"
    with pytest.raises(ValueError, match="evidence version"):
        export.build(*data)
    data = fixtures(); data[1].append(deepcopy(data[1][0]))
    with pytest.raises(ValueError, match="Duplicate"):
        export.build(*data)
    data = fixtures(); data[1].pop(0)
    with pytest.raises(ValueError, match="coverage mismatch"):
        export.build(*data)


def test_export_never_overwrites_annotations_and_csv_round_trip(tmp_path, monkeypatch):
    data = fixtures(); root = tmp_path / "input"; root.mkdir()
    source = root / "pinned.txt"; source.write_text("immutable source", encoding="utf-8")
    hashes = {str(source): silver.sha(source)}
    monkeypatch.setattr(export, "load", lambda *args: (data, hashes, [root]))
    out = tmp_path / "review"
    first, summary = export.export("er", "cat", out)
    identity = first / "identity_review.csv"
    with identity.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream); fields = reader.fieldnames; rows = list(reader)
    rows[0].update(human_label="UNSURE", reviewer_note="Giữ nguyên ghi chú", reviewer="reviewer")
    with identity.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields); writer.writeheader(); writer.writerows(rows)
    snapshot = {p.name: p.read_bytes() for p in first.iterdir()}
    second, again = export.export("er", "cat", out)
    assert first != second and again == summary
    assert {p.name: p.read_bytes() for p in first.iterdir()} == snapshot
    assert source.read_text() == "immutable source"
    for name, labels in (("identity_review.csv", export.IDENTITY_LABELS), ("category_review.csv", export.CATEGORY_LABELS)):
        p = second / name
        assert p.read_bytes().startswith(b"\xef\xbb\xbf")
        with p.open(encoding="utf-8-sig", newline="") as stream:
            assert all(all(r[f] == "" for f in labels) for r in csv.DictReader(stream))
        assert silver.sha(p) == summary["artifacts"][name]["sha256"]
    assert not list(out.glob(".stage-*"))


def test_input_change_does_not_publish_partial_queues(tmp_path, monkeypatch):
    data = fixtures(); root = tmp_path / "input"; root.mkdir()
    source = root / "pinned.txt"; source.write_text("changed", encoding="utf-8")
    monkeypatch.setattr(export, "load", lambda *args: (data, {str(source): "wrong-hash"}, [root]))
    out = tmp_path / "review"
    with pytest.raises(ValueError, match="Input changed"):
        export.export("er", "cat", out)
    assert list(out.iterdir()) == []
