from copy import deepcopy
import csv
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from fnb_platform import er_auto_accept as p, er_category_review_export as r, human_decisions as h, google_silver as s
from test_human_decisions import data_fixture


POLICY = p.load_policy(p.DEFAULT_POLICY)


def fixture():
    data = data_fixture()
    for er in data[0]:
        e = json.loads(er["evidence_json"]); v = e["comparisons"]["v3"]
        v["signals"].update(name="SUPPORT", location="SUPPORT", candidate="UNKNOWN")
        v["address_features_v3"].update(strong_agreement=True, specific_agreement=True, uncertain_difference=False, reference_only=False)
        if v["address_features_v3"]["clear_conflict"]:
            v["signals"]["address"] = "CONFLICT"
        e["google"]["conflicting_values"] = {}
        er["evidence_json"] = s.dump(e)
    for g in data[3]:
        g.update(metadata_status="AVAILABLE", latitude=10.7501, longitude=106.7, has_metadata_conflict=False, conflict_fields=[], conflicting_values_json="{}")
    return data


def machine(data, oid, label="MATCH"):
    er = next(e for e in data[0] if e["overture_id"] == oid)
    return {h.key(er): dict(ai_suggested_label=label, ai_confidence="HIGH", ai_status="AI_PRE_REVIEW_NOT_APPROVED", ai_rationale="Fixture machine proposal")}


def evaluate(data=None, history=(), ai=None):
    data = data if data is not None else fixture()
    return p.build(data, data[3], list(history), ai or {}, POLICY, dict(fixture="immutable fixture references"))


def rows(table):
    return {r["overture_id"]: r for r in table.to_pylist()}


def history(data, decisions):
    expected = {e["overture_id"]: e for e in r.build(*data)[0]}
    candidates = []
    for oid, label in decisions:
        row = expected[oid]
        event = {f: row.get(f) for f in h.HISTORY_SCHEMA.names}
        event.update(decision_type="identity", human_label=label, reviewed_category="", reviewed_fnb_status="",
            reviewer="Human fixture", reviewer_note="Checked fixture identity")
        candidates.append(event)
    return h.append_decisions([], candidates, "fixture-time")[0]


def test_auto_accept_requires_positive_evidence_and_is_shadow_only():
    data = fixture(); before = deepcopy(data)
    table, summary = evaluate(data)
    clean = rows(table)["ov-clean"]
    assert clean["effective_status"] == "AUTO_ACCEPTED" and clean["decision_source"] == "AUTO_POLICY"
    assert clean["shadow_only"] and clean["policy_version"] == POLICY["policy_version"]
    assert not clean["human_label"] and clean["evidence_hash"] and clean["reason_codes"]
    assert summary["mode"] == "SHADOW_ONLY_NOT_ACTIVATED"
    assert data == before


@pytest.mark.parametrize("mode,code", [("category", "CATEGORY_FNB_NON_FNB_CONFLICT"), ("metadata", "METADATA_CONFLICT"),
    ("address", "CURRENT_ADDRESS_CONFLICT"), ("branch", "CURRENT_BRANCH_OR_NAME_WARNING"), ("location", "LOCATION_SUPPORT_MISSING"),
    ("missing", "ESSENTIAL_METADATA_MISSING_OR_INVALID"), ("uncertain", "UNCERTAIN_ADDRESS_DIFFERENCE"), ("reference", "REFERENCE_ONLY_ADDRESS"),
    ("premises", "SPECIFIC_ADDRESS_AGREEMENT_MISSING")])
def test_policy_holds_real_conflicts_and_missing_evidence(mode, code):
    data = fixture(); er = next(e for e in data[0] if e["overture_id"] == "ov-clean")
    e = json.loads(er["evidence_json"]); v = e["comparisons"]["v3"]
    gp = next(g for g in data[3] if g["google_identity_value"] == er["google_identity_value"])
    if mode == "category":next(c for c in data[1] if h.key(c) == h.key(er))["category_relation"] = "CATEGORY_CONFLICT"
    if mode == "metadata":gp.update(has_metadata_conflict=True, conflict_fields=["address_display"])
    if mode == "address":v["address_features_v3"]["clear_conflict"] = True
    if mode == "branch":v["name_features"]["effective_branch_warning_codes"] = ["DIFFERENT_NAME_NUMERIC_TOKENS"]
    if mode == "location":v["signals"]["location"] = "UNKNOWN"
    if mode == "missing":gp["latitude"] = None
    if mode == "uncertain":v["address_features_v3"]["uncertain_difference"] = True
    if mode == "reference":v["address_features_v3"]["reference_only"] = True
    if mode == "premises":v["address_features_v3"]["specific_agreement"] = False
    er["evidence_json"] = s.dump(e)
    item = rows(evaluate(data)[0])["ov-clean"]
    assert item["effective_status"] in {"CONFLICT", "PENDING"} and code in item["reason_codes"]


def test_ai_match_is_only_entry_and_never_human_truth():
    data = fixture(); ai = machine(data, "ov-review")
    item = rows(evaluate(data, ai=ai)[0])["ov-review"]
    assert item["effective_status"] == "AUTO_ACCEPTED" and item["decision_source"] == "AUTO_POLICY"
    assert item["human_label"] is None and item["ai_status"] == "AI_PRE_REVIEW_NOT_APPROVED"
    e = next(x for x in data[0] if x["overture_id"] == "ov-review")
    ev = json.loads(e["evidence_json"]); ev["comparisons"]["v3"]["signals"]["name"] = "UNKNOWN"; e["evidence_json"] = s.dump(ev)
    item = rows(evaluate(data, ai=ai)[0])["ov-review"]
    assert item["effective_status"] == "PENDING" and item["ai_policy_relation"] == "AI_MATCH_INSUFFICIENT_SUPPORT"


@pytest.mark.parametrize("label,expected", [("UNSURE", "PENDING"), ("NON_MATCH", "CONFLICT")])
def test_machine_labels_do_not_reject_or_override_unsure(label, expected):
    data = fixture(); item = rows(evaluate(data, ai=machine(data, "ov-clean", label))[0])["ov-clean"]
    assert item["effective_status"] == expected and item["human_label"] is None


@pytest.mark.parametrize("label,expected", [("MATCH", "HUMAN_ACCEPTED"), ("NON_MATCH", "REJECTED"), ("UNSURE", "PENDING")])
def test_human_override_always_has_priority(label, expected):
    data = fixture(); hist = history(data, [("ov-review", label)])
    ai = machine(data, "ov-review", "UNSURE" if label == "MATCH" else "MATCH")
    item = rows(evaluate(data, hist, ai)[0])["ov-review"]
    assert item["effective_status"] == expected and item["decision_source"] == "HUMAN"
    assert item["human_decision_id"] == hist[0]["decision_id"]


def test_human_identity_match_can_be_valid_despite_category_conflict():
    data = fixture(); hist = history(data, [("ov-conflict", "MATCH")])
    item = rows(evaluate(data, hist)[0])["ov-conflict"]
    assert item["effective_status"] == "HUMAN_ACCEPTED" and item["category_relation"] == "CATEGORY_CONFLICT"


def test_shared_id_cannot_auto_merge_and_human_group_checks_remain():
    data = fixture(); table, summary = evaluate(data)
    assert all(rows(table)[oid]["effective_status"] == "CONFLICT" for oid in ["ov-shared-a", "ov-shared-b"])
    assert summary["auto_safety_checks"]["shared_google_id_leaks"] == 0
    hist = history(data, [("ov-shared-a", "MATCH"), ("ov-shared-b", "MATCH")])
    assert all(rows(evaluate(data, hist)[0])[oid]["effective_status"] == "CONFLICT" for oid in ["ov-shared-a", "ov-shared-b"])
    hist = history(data, [("ov-shared-a", "MATCH"), ("ov-shared-b", "NON_MATCH")])
    result = rows(evaluate(data, hist)[0])
    assert result["ov-shared-a"]["effective_status"] == "HUMAN_ACCEPTED" and result["ov-shared-b"]["effective_status"] == "REJECTED"
    accepted = [x for x in result.values() if x["effective_status"] in {"AUTO_ACCEPTED", "HUMAN_ACCEPTED"}]
    assert len({h.key(x)[1:] for x in accepted}) == len(accepted)


def add_competitor(data):
    er = deepcopy(next(e for e in data[0] if e["overture_id"] == "ov-review")); original_key = h.key(er)
    er["google_identity_value"] = "0x9:0x9"; er["classification"] = "MATCH_CANDIDATE"; data[0].append(er)
    original_cat = next(c for c in data[1] if h.key(c) == original_key)
    original_cat.update(comparison_status="NEEDS_IDENTITY_RECHECK", multiple_google_candidates=True)
    c = deepcopy(original_cat); c.update(google_identity_value="0x9:0x9", er_classification="MATCH_CANDIDATE"); data[1].append(c)
    n = deepcopy(next(n for n in data[2] if n["source"] == "google" and n["google_identity_value"] == original_key[2])); n["google_identity_value"] = "0x9:0x9"; data[2].append(n)
    g = deepcopy(next(g for g in data[3] if g["google_identity_value"] == original_key[2])); g["google_identity_value"] = "0x9:0x9"; data[3].append(g)
    return er


def test_multiple_candidates_are_held_not_resolved_by_machine_scores():
    data = fixture(); add_competitor(data)
    table, _ = evaluate(data, ai=machine(data, "ov-review"))
    entries = [r for r in table.to_pylist() if r["overture_id"] == "ov-review"]
    assert len(entries) == 2 and all(r["effective_status"] == "CONFLICT" for r in entries)
    assert all("MULTIPLE_GOOGLE_CANDIDATES_UNRESOLVED" in r["policy_conflicts"] for r in entries)


def test_current_human_non_match_can_resolve_multiple_candidate_conflict():
    data = fixture(); competitor = add_competitor(data)
    # The published V3 signal may still record the old unresolved group. Keep
    # that evidence unchanged and check it against current human group decisions.
    source = next(e for e in data[0] if e["overture_id"] == "ov-review" and e["google_identity_value"] != "0x9:0x9")
    ev = json.loads(source["evidence_json"]); v = ev["comparisons"]["v3"]
    v.update(multiple_candidates=True); v["signals"]["candidate"] = "CONFLICT"; source["evidence_json"] = s.dump(ev)
    expected = r.build(*data)[0]
    row = next(x for x in expected if h.key(x) == h.key(competitor))
    candidate = {f:row.get(f) for f in h.HISTORY_SCHEMA.names}
    candidate.update(decision_type="identity", human_label="NON_MATCH", reviewer="Human fixture", reviewer_note="Wrong candidate", reviewed_category="", reviewed_fnb_status="")
    hist = h.append_decisions([], [candidate], "fixture-time")[0]
    table, _ = evaluate(data, hist, machine(data, "ov-review"))
    target = next(x for x in table.to_pylist() if h.key(x) == h.key(source))
    assert target["effective_status"] == "AUTO_ACCEPTED"
    assert "MULTIPLE_GOOGLE_CANDIDATES_UNRESOLVED" not in target["policy_conflicts"]


def test_deterministic_reordering_and_history_replay():
    data = fixture(); hist = history(data, [("ov-review", "NON_MATCH")]); ai = machine(data, "ov-review")
    table, summary = evaluate(data, hist, ai)
    reversed_data = tuple([list(reversed(x)) for x in data[:4]] + [data[4]])
    other, repeated = evaluate(reversed_data, deepcopy(hist), ai)
    assert table.equals(other) and summary == repeated
    assert evaluate(data, hist, ai)[0].equals(table)


def write_ai(tmp_path, data):
    expected = r.build(*data)[0]
    ai_path = tmp_path / "ai.csv"; audit_path = tmp_path / "audit.json"
    fields = r.IDENTITY_FIELDS + ["ai_suggested_label", "ai_confidence", "ai_status", "ai_rationale"]
    with ai_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields); writer.writeheader()
        for row in expected:writer.writerow(dict(row, ai_suggested_label="MATCH", ai_confidence="HIGH", ai_status="AI_PRE_REVIEW_NOT_APPROVED", ai_rationale="Machine fixture"))
    report = dict(status="PROVISIONAL_AI_ONLY_NO_DECISIONS_IMPORTED", integrity_checks=dict(fixture=True),
        releases={f:data[4][f] for f in ("er_release_id", "category_release_id", "google_silver_release_id")},
        rows=len(expected), ai_suggested_labels={"MATCH":len(expected)}, input_hashes={str(ai_path.resolve()):s.sha(ai_path)})
    audit_path.write_text(s.dump(report), encoding="utf-8")
    return ai_path, audit_path


def test_audited_ai_validated_without_trusting_or_importing_labels(tmp_path):
    data = fixture(); paths = write_ai(tmp_path, data); expected = r.build(*data)[0]
    actual = p.audited_ai(*paths, expected, data[4])
    assert len(actual) == len(expected)
    paths[0].write_text("modified", encoding="utf-8")
    with pytest.raises(ValueError, match="audited bytes"):
        p.audited_ai(*paths, expected, data[4])


def test_shadow_publication_replay_and_failure_preserve_active_and_human_history(tmp_path, monkeypatch):
    data = fixture(); sources = tmp_path / "source"; sources.mkdir()
    gp = sources / "google_places.parquet"; pq.write_table(pa.Table.from_pylist(data[3]), gp)
    data[4]["refs"]["google_places"] = dict(path=str(gp), sha256=s.sha(gp))
    monkeypatch.setattr(p.review, "load", lambda *args: (data, {str(gp):s.sha(gp)}, [sources]))
    ai_path, audit = write_ai(tmp_path, data)
    hroot = tmp_path / "human"; out = tmp_path / "shadow"
    args = ("er", "cat", hroot, ai_path, audit, out)
    d, summary = p.publish(*args)
    original = (out / "current.json").read_bytes()
    assert p.publish(*args)[0] == d and (out / "current.json").read_bytes() == original
    assert not hroot.exists()
    # A new audited machine annotation must produce a new shadow signature.
    text = ai_path.read_text(encoding="utf-8-sig").replace("HIGH", "MEDIUM")
    ai_path.write_text(text, encoding="utf-8-sig")
    a = json.loads(audit.read_text()); a["input_hashes"][str(ai_path.resolve())] = s.sha(ai_path); audit.write_text(s.dump(a))
    def fault(stage):
        if stage == "before_pointer_replace":raise RuntimeError("injected shadow failure")
    with pytest.raises(RuntimeError, match="injected"):
        p.publish(*args, fault=fault)
    assert (out / "current.json").read_bytes() == original and not hroot.exists()
    d2, _ = p.publish(*args)
    assert d2 != d and p.publish(*args)[0] == d2


def test_shadow_replays_actual_human_ledger_without_changing_it(tmp_path, monkeypatch):
    data = fixture(); sources = tmp_path / "source"; sources.mkdir()
    gp = sources / "google_places.parquet"; pq.write_table(pa.Table.from_pylist(data[3]), gp)
    data[4]["refs"]["google_places"] = dict(path=str(gp), sha256=s.sha(gp))
    monkeypatch.setattr(p.review, "load", lambda *args: (data, {str(gp):s.sha(gp)}, [sources]))
    ai_path, audit = write_ai(tmp_path, data)
    identity, category, _ = r.build(*data)
    identity_path, category_path = tmp_path / "identity.csv", tmp_path / "category.csv"
    def write(path, data, fields):
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields); w.writeheader(); w.writerows(data)
    target = next(e for e in identity if e["overture_id"] == "ov-review")
    target.update(human_label="UNSURE", reviewer="Human fixture", reviewer_note="Not enough independent evidence")
    write(identity_path, identity, r.IDENTITY_FIELDS); write(category_path, category, r.CATEGORY_FIELDS)
    hroot, out = tmp_path / "human", tmp_path / "shadow"
    h.run("er", "cat", identity_path, category_path, hroot, apply=True)
    before = {str(f):s.sha(f) for f in hroot.rglob("*") if f.is_file()}
    d, _ = p.publish("er", "cat", hroot, ai_path, audit, out)
    assert rows(pq.read_table(d / "effective_identity_mapping.parquet"))["ov-review"]["effective_status"] == "PENDING"
    assert all(s.sha(f) == digest for f, digest in before.items())
    target.update(human_label="NON_MATCH", reviewer_note="Found the wrong fixture branch")
    write(identity_path, identity, r.IDENTITY_FIELDS)
    h.run("er", "cat", identity_path, category_path, hroot, apply=True)
    after_human = {str(f):s.sha(f) for f in hroot.rglob("*") if f.is_file()}
    d2, _ = p.publish("er", "cat", hroot, ai_path, audit, out)
    assert d2 != d and rows(pq.read_table(d2 / "effective_identity_mapping.parquet"))["ov-review"]["effective_status"] == "REJECTED"
    assert all(s.sha(f) == digest for f, digest in after_human.items())
