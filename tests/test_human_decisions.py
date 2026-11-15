from copy import deepcopy
import csv
import json
import os
from pathlib import Path
import subprocess
import sys

import pyarrow.parquet as pq
import pytest

from fnb_platform import human_decisions as h, er_category_review_export as r, google_silver as s
from test_er_category_review_export import fixtures


def data_fixture():
    data = fixtures()
    cats = {h.key(row): row for row in data[1]}
    for row in data[0]:
        row.update({f: cats[h.key(row)][f] for f in ("overture_name", "google_name", "overture_address", "google_address")})
    return data


def csv_file(path, rows, fields):
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields); writer.writeheader(); writer.writerows(rows)
    return path


def setup(tmp_path, monkeypatch):
    data = data_fixture()
    identities, categories, _ = r.build(*data)
    identity = csv_file(tmp_path / "identity.csv", identities, r.IDENTITY_FIELDS)
    category = csv_file(tmp_path / "category.csv", categories, r.CATEGORY_FIELDS)
    input_dir = tmp_path / "sources"; input_dir.mkdir()
    source = input_dir / "immutable.txt"; source.write_text("source", encoding="utf-8")
    monkeypatch.setattr(h.review, "load", lambda *args: (data, {str(source): s.sha(source)}, [input_dir]))
    return data, identities, categories, identity, category, tmp_path / "decisions"


def annotate(rows, oid, label="MATCH", **extra):
    row = next(r for r in rows if r["overture_id"] == oid)
    row.update(human_label=label, reviewer="Human fixture reviewer", reviewer_note="Checked fixture branch evidence", **extra)


def invoke(env, *, apply=True, fault=lambda stage: None):
    return h.run("er", "cat", env[3], env[4], env[5], apply=apply, fault=fault)


def active_tables(env):
    active = s.read_active(env[5])
    return {name: pq.read_table(active / name).to_pylist() for name in s.verify_release(active)["artifacts"] if name.endswith(".parquet")}


def test_blank_and_ai_only_dry_run_writes_nothing(tmp_path, monkeypatch):
    env = setup(tmp_path, monkeypatch)
    for row in env[1]:
        row.update(ai_suggested_label="MATCH", ai_confidence="HIGH", ai_rationale="AI says yes")
    csv_file(env[3], env[1], r.IDENTITY_FIELDS + ["ai_suggested_label", "ai_confidence", "ai_rationale"])
    result = invoke(env, apply=False)
    assert result["writes"] == 0 and result["new_decisions"] == 0
    assert result["effective_identity_counts"] == {"PENDING": len(env[0][0]), "ACCEPTED": 0, "REJECTED": 0}
    assert not env[5].exists()


def test_partial_import_idempotent_and_source_preserved(tmp_path, monkeypatch):
    env = setup(tmp_path, monkeypatch)
    annotate(env[1], "ov-review")
    csv_file(env[3], [env[1][next(i for i, x in enumerate(env[1]) if x["overture_id"] == "ov-review")]], r.IDENTITY_FIELDS)
    original = env[3].read_bytes(); before = deepcopy(env[0])
    first = invoke(env)
    assert first["new_decisions"] == 1 and first["effective_identity_counts"]["ACCEPTED"] == 1
    pointer = (env[5] / "current.json").read_bytes()
    second = invoke(env)
    assert second["idempotent"] and second["new_decisions"] == 0
    assert pointer == (env[5] / "current.json").read_bytes()
    assert env[3].read_bytes() == original and env[0] == before
    assert len(active_tables(env)["decision_history.parquet"]) == 1


def test_revision_history_unsure_and_blank_never_revoke(tmp_path, monkeypatch):
    env = setup(tmp_path, monkeypatch)
    for label in ("MATCH", "NON_MATCH", "UNSURE", "MATCH"):
        annotate(env[1], "ov-review", label)
        csv_file(env[3], env[1], r.IDENTITY_FIELDS)
        invoke(env)
    tables = active_tables(env); history = tables["decision_history.parquet"]
    assert [e["revision"] for e in history] == [1, 2, 3, 4]
    assert [e["human_label"] for e in history] == ["MATCH", "NON_MATCH", "UNSURE", "MATCH"]
    assert len({e["decision_id"] for e in history}) == 4
    assert all(history[i]["parent_decision_id"] == history[i-1]["decision_id"] for i in range(1, 4))
    for row in env[1]:
        for field in r.IDENTITY_LABELS:row[field] = ""
    csv_file(env[3], env[1], r.IDENTITY_FIELDS)
    assert invoke(env)["idempotent"]
    assert next(x for x in active_tables(env)["effective_identity_mapping.parquet"] if x["overture_id"] == "ov-review")["effective_status"] == "ACCEPTED"


@pytest.mark.parametrize("missing", ["human_label", "reviewer", "reviewer_note"])
def test_incomplete_human_annotation_stays_pending(tmp_path, monkeypatch, missing):
    env = setup(tmp_path, monkeypatch)
    annotate(env[1], "ov-review")
    next(r for r in env[1] if r["overture_id"] == "ov-review")[missing] = "   "
    csv_file(env[3], env[1], r.IDENTITY_FIELDS)
    result = invoke(env, apply=False)
    assert result["new_decisions"] == 0 and result["identity_input"]["warnings"]
    assert result["effective_identity_counts"]["ACCEPTED"] == 0


@pytest.mark.parametrize("column", ["review_id", "overture_id", "er_release_id", "category_release_id", "evidence_hash", "er_evidence_json", "google_address", "distance_m", "source_provenance_json"])
def test_stale_or_modified_immutable_evidence_rejected_atomically(tmp_path, monkeypatch, column):
    env = setup(tmp_path, monkeypatch); annotate(env[1], "ov-review")
    env[1][0][column] += "tampered"
    csv_file(env[3], env[1], r.IDENTITY_FIELDS)
    with pytest.raises(ValueError, match="stale|mismatch"):
        invoke(env)
    assert not (env[5] / "current.json").exists()


def test_duplicate_ids_and_edited_category_evidence_rejected(tmp_path, monkeypatch):
    env = setup(tmp_path, monkeypatch)
    csv_file(env[3], env[1] + [env[1][0]], r.IDENTITY_FIELDS)
    with pytest.raises(ValueError, match="Duplicate"):
        invoke(env)
    csv_file(env[3], env[1], r.IDENTITY_FIELDS)
    env[2][0]["taxonomy_hierarchy"] = "[]tampered"
    csv_file(env[4], env[2], r.CATEGORY_FIELDS)
    with pytest.raises(ValueError, match="evidence mismatch"):
        invoke(env)


def test_shared_group_blocks_match_until_other_link_human_rejected(tmp_path, monkeypatch):
    env = setup(tmp_path, monkeypatch)
    annotate(env[1], "ov-shared-a")
    csv_file(env[3], env[1], r.IDENTITY_FIELDS)
    result = invoke(env)
    rows = {x["overture_id"]: x for x in active_tables(env)["effective_identity_mapping.parquet"]}
    assert rows["ov-shared-a"]["effective_status"] == "PENDING"
    assert "SHARED_GOOGLE_ID_UNRESOLVED" in rows["ov-shared-a"]["conflict_codes"]
    annotate(env[1], "ov-shared-b")
    csv_file(env[3], env[1], r.IDENTITY_FIELDS); invoke(env)
    assert all(x["effective_status"] == "PENDING" for x in active_tables(env)["effective_identity_mapping.parquet"] if "ov-shared" in x["overture_id"])
    annotate(env[1], "ov-shared-b", "NON_MATCH")
    csv_file(env[3], env[1], r.IDENTITY_FIELDS); invoke(env)
    rows = {x["overture_id"]: x for x in active_tables(env)["effective_identity_mapping.parquet"]}
    assert rows["ov-shared-a"]["effective_status"] == "ACCEPTED" and rows["ov-shared-b"]["effective_status"] == "REJECTED"


def test_multiple_candidates_group_uses_all_links():
    data = data_fixture(); more = deepcopy(data[0][0]); more["google_identity_value"] = "0x7:0x7"
    data[0].append(more)  # extra non-reviewed candidate is still a group blocker
    expected = r.build(*data_fixture())[:2]
    row = next(x for x in expected[0] if x["overture_id"] == "ov-review")
    decision = {f: row.get(f) for f in h.HISTORY_SCHEMA.names}
    decision.update(decision_type="identity", decision_id="test", parent_decision_id=None, revision=1,
        decision_rule_version=h.VERSION, human_label="MATCH", reviewer="human", reviewer_note="checked")
    tables = h.effective([decision], data, expected)
    selected = next(x for x in tables["effective_identity_mapping.parquet"].to_pylist() if h.key(x) == h.key(row))
    assert selected["effective_status"] == "PENDING"
    assert "MULTIPLE_GOOGLE_CANDIDATES_UNRESOLVED" in selected["conflict_codes"]


def test_category_independent_and_never_implicitly_accepts_identity(tmp_path, monkeypatch):
    env = setup(tmp_path, monkeypatch)
    row = next(x for x in env[2] if x["review_grain"] == "LINK")
    row.update(reviewed_fnb_status="NON_FNB", reviewed_category="non_fnb", reviewer="human", reviewer_note="Verified fixture business category")
    csv_file(env[4], env[2], r.CATEGORY_FIELDS)
    invoke(env)
    tables = active_tables(env)
    assert all(x["effective_status"] == "PENDING" for x in tables["effective_identity_mapping.parquet"])
    cat = next(x for x in tables["effective_category_decisions.parquet"] if x["review_grain"] == "LINK")
    assert cat["decision_status"] == "HUMAN_RECORDED" and cat["applicability_status"] == "WAITING_ACCEPTED_IDENTITY"
    annotate(env[1], "ov-conflict")
    csv_file(env[3], env[1], r.IDENTITY_FIELDS); invoke(env)
    cat = next(x for x in active_tables(env)["effective_category_decisions.parquet"] if x["review_grain"] == "LINK")
    assert cat["applicability_status"] == "IDENTITY_ACCEPTED_CATEGORY_RECORDED"


def test_stale_history_never_becomes_current_effective_acceptance(tmp_path, monkeypatch):
    env = setup(tmp_path, monkeypatch)
    annotate(env[1], "ov-review"); csv_file(env[3], env[1], r.IDENTITY_FIELDS); invoke(env)
    history = active_tables(env)["decision_history.parquet"]
    expected = r.build(*env[0])[:2]
    next(x for x in expected[0] if x["overture_id"] == "ov-review")["evidence_hash"] = "new-current-evidence"
    row = next(x for x in h.effective(history, env[0], expected)["effective_identity_mapping.parquet"].to_pylist() if x["overture_id"] == "ov-review")
    assert row["effective_status"] == "PENDING" and row["evidence_applicability"] == "STALE"
    expected[0][:] = [r for r in expected[0] if r["overture_id"] != "ov-review"]
    row = next(x for x in h.effective(history, env[0], expected)["effective_identity_mapping.parquet"].to_pylist() if x["overture_id"] == "ov-review")
    assert row["effective_status"] == "PENDING" and row["evidence_applicability"] == "STALE"


def test_category_revisions_source_only_and_rejected_identity_do_not_transfer(tmp_path, monkeypatch):
    env = setup(tmp_path, monkeypatch)
    linked = next(x for x in env[2] if x["review_grain"] == "LINK")
    source_only = next(x for x in env[2] if x["review_grain"] == "OVERTURE_SOURCE_ONLY")
    for row in (linked, source_only):
        row.update(reviewed_fnb_status="FNB", reviewed_category="restaurant", reviewer="human", reviewer_note="Checked category fixture")
    annotate(env[1], "ov-conflict", "NON_MATCH")
    csv_file(env[3], env[1], r.IDENTITY_FIELDS); csv_file(env[4], env[2], r.CATEGORY_FIELDS)
    invoke(env)
    cats = active_tables(env)["effective_category_decisions.parquet"]
    assert next(x for x in cats if x["review_grain"] == "LINK")["applicability_status"] == "IDENTITY_REJECTED_DO_NOT_TRANSFER"
    assert next(x for x in cats if x["review_grain"] == "OVERTURE_SOURCE_ONLY")["applicability_status"] == "SOURCE_ONLY_DECISION_NO_CANONICAL_MAPPING"
    assert next(x for x in cats if x["review_grain"] == "OVERTURE_SOURCE_ONLY")["google_identity_value"] is None
    linked.update(reviewed_fnb_status="UNSURE", reviewed_category="")
    csv_file(env[4], env[2], r.CATEGORY_FIELDS); invoke(env)
    assert next(x for x in active_tables(env)["effective_category_decisions.parquet"] if x["review_grain"] == "LINK")["decision_status"] == "PENDING"
    history = active_tables(env)["decision_history.parquet"]
    assert len([x for x in history if x["review_id"] == linked["review_id"]]) == 2


@pytest.mark.parametrize("label,category", [("FNB", "non_fnb"), ("NON_FNB", "restaurant"), ("UNSURE", "cafe_coffee"), ("FNB", "fabricated")])
def test_conflicting_category_annotations_rejected(tmp_path, monkeypatch, label, category):
    env = setup(tmp_path, monkeypatch)
    env[2][0].update(reviewed_fnb_status=label, reviewed_category=category, reviewer="human", reviewer_note="checked")
    csv_file(env[4], env[2], r.CATEGORY_FIELDS)
    with pytest.raises(ValueError, match="category"):
        invoke(env, apply=False)


def test_all_multiple_candidate_decisions_must_be_resolved():
    data = data_fixture(); expected = r.build(*data)[:2]
    extra = deepcopy(data[0][0]); extra["google_identity_value"] = "0x7:0x7"; data[0].append(extra)
    original_review = next(x for x in expected[0] if x["overture_id"] == "ov-review")
    other = dict(original_review, google_identity_value="0x7:0x7", review_id="identity_" + h._digest(dict(grain="LINK", key=list(h.key(extra)))))
    expected[0].append(other)
    candidates = []
    for row, label in ((original_review, "MATCH"), (other, "UNSURE")):
        candidate = {f: row.get(f) for f in h.HISTORY_SCHEMA.names}
        candidate.update(decision_type="identity", human_label=label, reviewer="human", reviewer_note="checked")
        candidates.append(candidate)
    history, _ = h.append_decisions([], candidates, "fixture-import-time")
    selected = next(x for x in h.effective(history, data, expected)["effective_identity_mapping.parquet"].to_pylist() if h.key(x) == h.key(original_review))
    assert selected["effective_status"] == "PENDING"
    candidates[1]["human_label"] = "NON_MATCH"
    history, _ = h.append_decisions(history, candidates, "next-fixture-import-time")
    selected = next(x for x in h.effective(history, data, expected)["effective_identity_mapping.parquet"].to_pylist() if h.key(x) == h.key(original_review))
    assert selected["effective_status"] == "ACCEPTED"


def test_input_change_before_publication_keeps_previous_active(tmp_path, monkeypatch):
    env = setup(tmp_path, monkeypatch)
    invoke(env); original = (env[5] / "current.json").read_bytes()
    annotate(env[1], "ov-review"); csv_file(env[3], env[1], r.IDENTITY_FIELDS)
    def fault(stage):
        if stage == "after_stage_validation":
            env[3].write_text("changed-during-publication", encoding="utf-8")
    with pytest.raises(ValueError, match="Input/code changed"):
        invoke(env, fault=fault)
    assert (env[5] / "current.json").read_bytes() == original
    assert active_tables(env)["decision_history.parquet"] == []


@pytest.mark.parametrize("crash", ["after_stage_validation", "after_release_rename", "before_pointer_replace", "after_pointer_replace"])
def test_crash_recovery_and_no_duplicate_decisions(tmp_path, monkeypatch, crash):
    env = setup(tmp_path, monkeypatch)
    baseline = invoke(env); old_pointer = (env[5] / "current.json").read_bytes()
    annotate(env[1], "ov-review"); csv_file(env[3], env[1], r.IDENTITY_FIELDS)
    def fault(stage):
        if stage == crash:raise RuntimeError("simulated crash")
    with pytest.raises(RuntimeError, match="simulated"):
        invoke(env, fault=fault)
    if crash != "after_pointer_replace":
        assert (env[5] / "current.json").read_bytes() == old_pointer
        assert active_tables(env)["decision_history.parquet"] == []
    retry = invoke(env)
    assert retry["effective_identity_counts"]["ACCEPTED"] == 1
    assert len(active_tables(env)["decision_history.parquet"]) == 1
    assert invoke(env)["idempotent"]
    assert not list(env[5].glob(".stage-*"))


def test_os_lock_blocks_concurrent_writer_and_recovers_after_process_death(tmp_path):
    with h.publication_lock(tmp_path):
        with pytest.raises(ValueError, match="Another"):
            with h.publication_lock(tmp_path):pass
    env = dict(os.environ); env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    code = "import os,sys;from pathlib import Path;from fnb_platform.human_decisions import publication_lock;\nwith publication_lock(Path(sys.argv[1])): os._exit(0)"
    subprocess.run([sys.executable, "-c", code, str(tmp_path)], env=env, check=True, timeout=20)
    with h.publication_lock(tmp_path):pass
