"""Technical identity dedup, coverage, deterministic fallback and atomic visibility."""
import json
import sqlite3
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from fnb_platform import google_silver as g


ID_A = "0xAB:0xCD"
ID_B = "0x12:0x34"


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def make_run(base, name, jobs):
    root = base / name
    root.mkdir()
    write(root / "summary.json", {"status": "FINISHED"})
    db = sqlite3.connect(root / "dispatch.sqlite3")
    db.execute("CREATE TABLE dispatch(id INTEGER PRIMARY KEY,place_key TEXT,payload TEXT,state TEXT,result TEXT)")
    for i, job in enumerate(jobs):
        source = job["source"]
        payload = dict(source="overture", place_key="overture:" + source, name="source " + source)
        result = job.get("result")
        path = root / "worker-01" / f"job-{i:02d}"
        if "metadata" in job:
            m = dict(job["metadata"], store_id="store-" + source)
            meta = path / "crawl" / name / "metadata" / ("store-" + source + ".place_details.json")
            write(meta, m)
            write(path / "input.json", dict(source_record=payload, store=dict(id="store-" + source)))
            if not job.get("orphan"):
                result = dict(result or {}, google_place_details_path=str(meta.resolve()))
        if result is not None:
            result = dict(result, id="store-" + source, selection_policy_version=name,
                          identity_status="VERIFIED", status="PARTIAL_LIMIT")
        db.execute("INSERT INTO dispatch VALUES (?,?,?,?,?)", (i, payload["place_key"], json.dumps(payload),
                   job.get("state", "FINISHED"), json.dumps(result) if result is not None else None))
    db.commit()
    db.close()
    return root


def metadata(identifier=ID_A, date="2026-11-01T00:00:00Z", **kwargs):
    return dict(google_identifier=identifier, google_identifier_type="data_id", observed_at=date,
        name="Trà Phúc Kiến", address="\ue3c8\n86 Xóm Chiếu", phone="+84 28 1234 5678",
        category="Trà sữa", latitude=10.76, longitude=106.70, rating=4.1, review_count=12,
        business_status="OPERATIONAL", open_now=True, **kwargs)


@pytest.fixture
def inputs(tmp_path):
    silver = tmp_path / "overture.parquet"
    pq.write_table(pa.table({"source_id": ["ov1", "ov2", "ov3", "ov4", "ov5"]}), silver)
    old = metadata()
    newer = dict(metadata(date="2026-11-02T00:00:00Z"), address=None, phone=None, rating=4.5,
                 open_now=None, business_status="UNKNOWN")
    roots = [make_run(tmp_path, "google-old", [dict(source="ov1", metadata=old),
        dict(source="ov2", metadata=old), dict(source="ov3", result=None, state="PENDING")]),
        make_run(tmp_path, "google-new", [dict(source="ov1", metadata=newer),
        dict(source="ov1", metadata=metadata(ID_B)),
        dict(source="ov4", result=dict(selected_candidate=dict(candidate_id="bad"))),
        dict(source="ov5", result=dict(selected_candidate=dict(candidate_id="0x99:0x88")))])]
    return roots, silver


def test_technical_dedup_preserves_sources_and_null_fallback(inputs):
    loaded = g.load(*inputs)
    tables, report, details = g.build(loaded)
    places = {p["google_identity_value"]: p for p in tables["silver/google_places.parquet"].to_pylist()}
    a = places[ID_A.lower()]
    assert len(places) == 3 and a["observation_count"] == 3
    assert a["rating"] == 4.5 and a["address_display"].startswith("\ue3c8")
    assert a["address_normalized"] == "86 xom chieu" and a["phone_normalized"] == "+842812345678"
    assert a["business_status"] == "OPERATIONAL" and a["latest_business_status_raw"] == "UNKNOWN"
    assert a["open_now"] is None  # No stale volatile fallback.
    assert "address_display" in a["fallback_fields"] and "rating" in a["conflict_fields"]
    assert json.loads(a["field_provenance_json"])["address_display"]["observed_at"].startswith("2026-11-01")
    links = tables["silver/source_links.parquet"].to_pylist()
    assert len(links) == 4 and {l["overture_id"] for l in links} == {"ov1", "ov2", "ov5"}
    assert all(l["er_status"] == "ER_PENDING" for l in links)  # Crawler VERIFIED is not ER.
    assert report["google_identity_collision_count"] == 1
    assert report["overture_ids_with_conflicting_candidates"] == 1
    assert report["technical_duplicate_observation_count"] == 2
    assert places["0x99:0x88"]["metadata_status"] == "MISSING_METADATA"
    zero = {r["overture_id"]: r["status"] for r in report["zero_candidate_inventory"]}
    assert zero == {"ov3": "INPUT_ONLY_NOT_ATTEMPTED", "ov4": "SELECTED_ID_INVALID"}
    # Rebuilding the same loaded snapshot does not mutate issue counts.
    second = g.build(loaded)
    assert report == second[1] and details == second[2]


def test_invalid_numeric_metadata_never_leaks_into_current_state(tmp_path):
    silver = tmp_path / "silver.parquet"
    pq.write_table(pa.table({"source_id": ["ov1"]}), silver)
    bad = dict(metadata(), latitude=100, longitude=float("nan"), rating=8, review_count=-1)
    root = make_run(tmp_path, "google-bad", [dict(source="ov1", metadata=bad)])
    tables, report, _ = g.build(g.load([root], silver))
    row = tables["silver/google_places.parquet"].to_pylist()[0]
    assert row["latitude"] is row["longitude"] is row["rating"] is row["review_count"] is None
    assert "INVALID_COORDINATES" in row["dq_flags"] and report["issue_counts"]["INVALID_RATING"] == 1


def test_identity_validation_keeps_data_id_separate_from_place_id():
    assert g.identity(dict(google_identifier=ID_A, google_identifier_type="data_id"))[0] == ("data_id", ID_A.lower())
    assert g.identity(dict(url="https://www.google.com/maps/place/x/data=!1s0xAB:0xCD!3d1!4d2"))[0] == ("data_id", ID_A.lower())
    assert g.identity(dict(google_place_id="ChIJabc123"))[0] == ("place_id", "ChIJabc123")
    assert g.identity(dict(google_identifier=ID_A, url="https://www.google.com/maps/place/x/data=!1s0x12:0x34!"))[0] is None
    assert g.identity(dict(url="https://example.com/maps/place/x/data=!1s0xAB:0xCD!"))[0] is None


def test_orphan_metadata_is_loaded_via_job_input_not_silently_lost(tmp_path):
    silver = tmp_path / "silver.parquet"
    pq.write_table(pa.table({"source_id": ["ov1"]}), silver)
    root = make_run(tmp_path, "google-interrupted", [dict(source="ov1", metadata=metadata(), orphan=True, state="RUNNING")])
    tables, report, _ = g.build(g.load([root], silver))
    assert tables["silver/source_links.parquet"].num_rows == 1
    assert report["input_runs"][0]["unreferenced_metadata_files"] == 1


def test_ranked_but_unselected_results_do_not_become_source_links(tmp_path):
    silver = tmp_path / "silver.parquet"
    pq.write_table(pa.table({"source_id": ["ov1"]}), silver)
    root = make_run(tmp_path, "google-search-only", [dict(source="ov1", result=dict(
        candidate_rankings=[dict(candidate_id=ID_A)], resolution_outcome="UNRESOLVED"))])
    tables, report, _ = g.build(g.load([root], silver))
    assert tables["silver/source_links.parquet"].num_rows == 0
    assert report["zero_candidate_inventory"][0]["status"] == "ZERO_SELECTED_OBSERVED"


def test_deterministic_ties_and_byte_identical_idempotent_publication(inputs, tmp_path):
    roots, silver = inputs
    protected = {p: g.sha(p) for r in roots for p in r.rglob("*") if p.is_file()}
    review = roots[0] / "worker-01/job-00/lake/do-not-read.parquet"
    review.parent.mkdir(parents=True)
    review.write_bytes(b"not parquet: reviews must not be opened")
    output = tmp_path / "silver-output"
    release, first = g.publish(roots, silver, output)
    files = {p.relative_to(release).as_posix(): g.sha(p) for p in release.rglob("*") if p.is_file()}
    pointer = (output / "current.json").read_bytes()
    same, second = g.publish(list(reversed(roots)), silver, output)
    assert release == same and first == second and pointer == (output / "current.json").read_bytes()
    assert files == {p.relative_to(same).as_posix(): g.sha(p) for p in same.rglob("*") if p.is_file()}
    assert all(g.sha(p) == digest for p, digest in protected.items())
    assert review.read_bytes().startswith(b"not parquet")


@pytest.mark.parametrize('failure_stage',['after_stage_validation','before_pointer_replace'])
def test_failed_publication_does_not_expose_a_partial_bundle(inputs, tmp_path, failure_stage):
    roots, silver = inputs
    output = tmp_path / "published"
    release, _ = g.publish(roots, silver, output)
    before = (output / "current.json").read_bytes()
    extra = make_run(tmp_path, "google-extra", [dict(source="ov2", metadata=metadata(ID_B))])
    def fail(stage):
        if stage == failure_stage: raise RuntimeError("simulated publication failure")
    with pytest.raises(RuntimeError):
        g.publish(roots + [extra], silver, output, fault=fail)
    assert (output / "current.json").read_bytes() == before
    assert g.verify_release(release)
    assert not (output / ".publish.lock").exists()


def test_active_runs_are_not_snapshotted(inputs):
    roots, silver = inputs
    write(roots[0] / "summary.json", {"status": "RUNNING"})
    with pytest.raises(ValueError, match="Active"):
        g.load(roots, silver)


def test_equal_timestamp_order_is_independent_of_input_order(inputs):
    loaded = g.load(*inputs)
    a, report, _ = g.build(loaded)
    loaded[1].reverse()
    loaded[2].reverse()
    b, other, _ = g.build(loaded)
    assert report == other
    assert all(a[name].equals(b[name]) for name in a)


def test_latest_evidence_and_opened_rejected_identity_are_both_preserved(tmp_path):
    silver=tmp_path/'silver.parquet'
    pq.write_table(pa.table({'source_id':['ov1']}),silver)
    root=make_run(tmp_path,'google-attempts',[dict(source='ov1',result=dict(
        finished_at='2026-11-03T00:00:00Z',selected_candidate=dict(candidate_id=ID_A,score=.7),
        candidate_attempts=[dict(candidate_id=ID_A,street_conflict=True),dict(candidate_id=ID_B,identifier_conflict=True)]))])
    tables,summary,_=g.build(g.load([root],silver))
    links=tables['silver/source_links.parquet'].to_pylist()
    assert len(links)==2 and all(l['er_status']=='ER_PENDING' for l in links)
    a=next(l for l in links if l['google_identity_value']==ID_A.lower())
    assert json.loads(a['selection_evidence_json'])['score']==.7
    assert json.loads(a['opened_evidence_json'])['street_conflict']
    assert summary['missing_metadata_google_ids']==2


def test_invalid_identity_snapshot_not_associated_with_a_valid_different_url(tmp_path):
    silver=tmp_path/'silver.parquet'
    pq.write_table(pa.table({'source_id':['ov1']}),silver)
    root=make_run(tmp_path,'google-ids',[dict(source='ov1',metadata=dict(metadata(),
        maps_url='https://www.google.com/maps/place/x/data=!1s0x12:0x34!'))])
    tables,summary,_=g.build(g.load([root],silver))
    assert tables['silver/google_places.parquet'].num_rows==0
    assert summary['invalid_metadata_observation_count']==1
    assert summary['zero_candidate_overture_id_count']==1


def test_hours_normalization_ignores_localized_day_labels_and_order(tmp_path):
    silver=tmp_path/'silver.parquet';pq.write_table(pa.table({'source_id':['ov1']}),silver)
    hours=[dict(iso_weekday=2,day='Tuesday',raw_hours='8AM–5PM',status='OPEN',
                intervals=[dict(opens='08:00',closes='17:00',closes_day_offset=0)]),
           dict(iso_weekday=1,day='Thứ Hai',raw_hours='Đóng cửa',status='CLOSED',intervals=[])]
    first=dict(metadata(),weekly_opening_hours=hours)
    second=dict(metadata(date='2026-11-02T00:00:00Z'),weekly_opening_hours=list(reversed(hours)))
    a=make_run(tmp_path,'google-hours-a',[dict(source='ov1',metadata=first)])
    b=make_run(tmp_path,'google-hours-b',[dict(source='ov1',metadata=second)])
    tables,_,_=g.build(g.load([a,b],silver))
    p=tables['silver/google_places.parquet'].to_pylist()[0]
    assert 'opening_hours_json' not in p['conflict_fields']
    normalized=json.loads(p['opening_hours_json'])
    assert [d['iso_weekday'] for d in normalized]==[1,2]
    assert all('raw_hours' not in d for d in normalized)


def test_invalid_hours_are_flagged_not_fabricated(tmp_path):
    silver=tmp_path/'silver.parquet';pq.write_table(pa.table({'source_id':['ov1']}),silver)
    bad=dict(metadata(),weekly_opening_hours=[dict(iso_weekday=1,status='OPEN',
        intervals=[dict(opens='25:00',closes='17:00',closes_day_offset=0)])])
    root=make_run(tmp_path,'google-bad-hours',[dict(source='ov1',metadata=bad)])
    tables,_,_=g.build(g.load([root],silver))
    p=tables['silver/google_places.parquet'].to_pylist()[0]
    assert p['opening_hours_json'] is None and 'INVALID_OPENING_HOURS' in p['dq_flags']


def test_corrupted_current_pointer_is_not_silently_overwritten(inputs,tmp_path):
    output=tmp_path/'published';g.publish(*inputs,output)
    pointer=output/'current.json';p=json.loads(pointer.read_text());p['manifest_sha256']='bad'
    write(pointer,p)
    before=pointer.read_bytes()
    with pytest.raises(ValueError,match='checksum'):
        g.publish(*inputs,output)
    assert pointer.read_bytes()==before


def test_missing_timestamp_is_explicitly_incomplete(tmp_path):
    silver=tmp_path/'silver.parquet';pq.write_table(pa.table({'source_id':['ov1']}),silver)
    root=make_run(tmp_path,'google-no-time',[dict(source='ov1',metadata=metadata(date=None))])
    tables,_,_=g.build(g.load([root],silver))
    row=tables['silver/google_places.parquet'].to_pylist()[0]
    assert row['observed_at'] is None and row['metadata_status']=='INCOMPLETE_METADATA'
    assert 'MISSING_OR_INVALID_METADATA_TIME' in row['dq_flags']
