"""Bounded live PostgreSQL contract test, using only owned synthetic schemas/role.

Run explicitly on the existing localhost PostgreSQL. Never touches Airflow/Gold tables.
"""

import importlib.util
import json
from decimal import Decimal
from pathlib import Path
import sys
import uuid
import runpy

import psycopg2
from psycopg2 import sql
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from fnb_platform.analytics import warehouse as wh


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


fixture = module("dimensional_fixture", ROOT / "tests/test_dimensional_analytics.py")
loader = module("analytics_loader", ROOT / "scripts/load_analytics.py")


def bundle(folder, population=4000):
    sources = folder / "sources"
    datasets = fixture.fixture_inputs(sources)
    datasets["areas"][0]["reference_population"] = population
    fixture.pq.write_table(
        fixture.pa.Table.from_pylist(datasets["areas"]), sources / "areas.parquet"
    )
    con, *_ = fixture.model_connection(sources)
    expected = dict(
        canonical_store_count=2,
        accepted_mapping_count=1,
        source_only_store_count=1,
        source_mapping_rows=3,
        unresolved_mapping_count=1,
        current_logical_reviews=4,
        attached_reviews=2,
        unmatched_reviews=2,
        category_validation_counts={"PROVISIONAL_SOURCE_AGREEMENT": 1, "PENDING": 1},
        google_places_count=1,
        google_available=1,
        google_incomplete=0,
        google_missing=0,
    )
    con.execute("create table release_audit(gold_release_id varchar,payload varchar)")
    con.execute(
        "insert into release_audit values (?,?)",
        ["synthetic-gold", json.dumps(expected)],
    )
    for name in wh.MARTS:
        con.execute(f"create table {name} as select * from dim_store")
    area = con.execute("select * from dim_area").fetchone()
    area = dict(zip([c[0] for c in con.description], area))
    expected["area_reference"] = {
        k: v
        for k, v in area.items()
        if k == "area_business_key" or k.startswith("reference_")
    }
    signature = {"fixture_only": True, "nonce": uuid.uuid4().hex}
    import hashlib

    rid = hashlib.sha256(wh.dump(signature).encode()).hexdigest()[:24]
    root = folder / rid
    root.mkdir()
    tables = {}
    artifacts = {}
    for name in [*wh.MODELS, *wh.MARTS, "release_audit"]:
        cursor = con.execute(f"select * from {name} order by all")
        description = [(c[0], str(c[1])) for c in cursor.description]
        rows = cursor.fetchall()
        dest = (root / (name + ".csv")).as_posix().replace("'", "''")
        con.execute(
            rf"copy (select * from {name} order by all) to '{dest}' (format csv,header true,null '\N',force_quote *)"
        )
        tables[name] = dict(
            columns=description, rows=len(rows), logical_sha256=wh.logical_hash(rows)
        )
        artifacts[name + ".csv"] = wh.sha(root / (name + ".csv"))
    con.close()
    manifest = dict(
        kind=wh.KIND,
        bundle_id=rid,
        gold_release_id="synthetic-gold",
        places_release_id="synthetic-places",
        reviews_release_id="synthetic-reviews",
        signature=signature,
        tables=tables,
        artifacts=artifacts,
        expected=expected,
        fixture_only=True,
    )
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


@pytest.mark.parametrize("population", [4000, 0, None])
def test_live_postgres_atomic_refresh_idempotency_and_readonly(tmp_path, population):
    secret = ROOT / ".auth/fnb/admin_password"
    assert (
        secret.is_file()
    ), "Existing PostgreSQL admin secret required; no substitute server or fake PASS"
    connection = psycopg2.connect(
        host="127.0.0.1",
        port=15432,
        dbname="postgres",
        user="fnb_admin",
        password=secret.read_text().strip(),
        connect_timeout=5,
    )
    first = bundle(tmp_path / "first", population)
    second = bundle(tmp_path / "second", population)
    bi = "test_bi_" + uuid.uuid4().hex[:16]
    role = "test_analytics_ro_" + uuid.uuid4().hex[:16]
    versions = ["analytics_" + p.name for p in [first, second]]
    owned = []
    created_role = False
    try:
        with connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "select count(*) from pg_namespace where nspname=any(%s)",
                    [[bi, *versions]],
                )
                assert cursor.fetchone()[0] == 0, "Test namespaces must be fresh"
                cursor.execute("select count(*) from pg_roles where rolname=%s", [role])
                assert cursor.fetchone()[0] == 0
                cursor.execute(
                    sql.SQL(
                        "create role {} nologin nosuperuser nocreatedb nocreaterole"
                    ).format(sql.Identifier(role))
                )
                created_role = True
        owned = [bi, *versions]
        one = loader.refresh(connection, first, bi=bi, readonly_role=role)
        two = loader.refresh(connection, first, bi=bi, readonly_role=role)
        assert one["status"] == "LOADED" and two["status"] == "REUSED_VERIFIED"
        assert one["kpis"] == two["kpis"]
        assert one["market_reference"] == two["market_reference"]
        assert one["market_reference"]["observed_stores_per_1000_residents"] == (
            0.5 if population else None
        )
        with connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"select observed_stores_per_1000_residents from {loader.identifier(bi)}.v_area_market"
                )
                native = cursor.fetchone()[0]
        if population:
            assert isinstance(
                native, Decimal
            ), "Regression requires a real PostgreSQL NUMERIC result"
            assert isinstance(
                two["market_reference"]["observed_stores_per_1000_residents"], float
            )
        else:
            assert native is None
        # Exercise the report-file boundary missed by row/transaction checks.
        report_path = tmp_path / "refresh-report.json"
        report_path.write_text(
            json.dumps(two, indent=2, allow_nan=False), encoding="utf-8"
        )
        decoded = json.loads(report_path.read_text())
        assert decoded["market_reference"] == two["market_reference"]
        assert decoded["kpis"] == two["kpis"]
        assert isinstance(decoded["market_reference"]["observed_store_count"], int)
        assert (
            one["kpis"]["canonical_records"] == 2
            and one["kpis"]["logical_reviews"] == 4
        )
        from jinja2 import Environment

        env = Environment()
        env.filters["where_in"] = (
            lambda values: "("
            + ",".join("'" + v.replace("'", "''") + "'" for v in values)
            + ")"
        )
        template = env.from_string(
            fixture.native_dataset("map_catalog")["sql"].replace(
                "bi.v_store_overview", loader.identifier(bi) + ".v_store_overview"
            )
        )
        with connection:
            with connection.cursor() as cursor:
                for values, level in [
                    ({}, "GRID"),
                    ({"area_name": ["Synthetic Area"]}, "DETAIL"),
                ]:
                    cursor.execute(
                        template.render(
                            filter_values=lambda key, **kw: values.get(key, [])
                        )
                    )
                    mapped = cursor.fetchall()
                    assert mapped and {row[-1] for row in mapped} == {level}
                    assert sum(row[-2] for row in mapped) == 2
                    if level == "DETAIL":
                        assert len(mapped) == 2
                # Thin dashboard SQL must preserve canonical/review grains and
                # compute Place medians separately from logical-review means.
                catalog = f"select * from {loader.identifier(bi)}.v_catalog_profile"
                cursor.execute(
                    "select count(*),count(distinct canonical_store_id) from ("
                    + catalog
                    + ") q"
                )
                assert cursor.fetchone() == (2, 2)
                metrics = {
                    m["metric_name"]: m["expression"]
                    for m in fixture.native_dataset("v_catalog_profile")["metrics"]
                }
                category = (
                    "select category_label,"
                    + ",".join(
                        metrics[name] + " as " + name
                        for name in (
                            "catalog_records",
                            "rated_places",
                            "median_google_rating",
                        )
                    )
                    + f" from {loader.identifier(bi)}.v_catalog_profile"
                )
                for source_values, expected_count in [
                    ([], 2),
                    (["Overture + Google"], 1),
                ]:
                    # Superset pushes native source filters before aggregation.
                    query = category
                    if source_values:
                        query += " where sources in ('Overture + Google')"
                    query += " group by category_label"
                    cursor.execute(
                        "select sum(catalog_records),sum(rated_places),max(median_google_rating) from ("
                        + query
                        + ") q"
                    )
                    assert cursor.fetchone() == (expected_count, 1, 4.7)
                profile = f"select * from {loader.identifier(bi)}.v_review_profile"
                cursor.execute(
                    "select count(*),count(distinct logical_review_id) from ("
                    + profile
                    + ") q"
                )
                assert cursor.fetchone() == (4, 4)
                kpi = f"select * from {loader.identifier(bi)}.v_review_kpi"
                cursor.execute(kpi)
                names = [c[0] for c in cursor.description]
                review_summary = dict(zip(names, cursor.fetchone()))
                assert review_summary["logical_reviews"] == 4
                assert (
                    review_summary["attached_reviews"]
                    == review_summary["unmatched_reviews"]
                    == 2
                )
                assert (
                    review_summary["stores_with_reviews"] == 1
                    and review_summary["store_review_coverage_pct"] == 50
                )
                assert float(review_summary["review_sample_mean"]) == pytest.approx(
                    10 / 3
                )
        with connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL("set local role {}").format(sql.Identifier(role))
                )
                cursor.execute(
                    f"select count(*) from {loader.identifier(bi)}.v_store_overview"
                )
                assert cursor.fetchone()[0] == 2
                cursor.execute("reset role")
                cursor.execute(
                    "select has_schema_privilege(%s,%s,'USAGE'),has_table_privilege(%s,%s,'INSERT')",
                    [role, versions[0], role, versions[0] + ".fact_review"],
                )
                assert cursor.fetchone() == (False, False)

        def fail(stage):
            if stage == "before_commit":
                raise RuntimeError("SYNTHETIC injected pre-commit failure")

        with pytest.raises(RuntimeError, match="pre-commit"):
            loader.refresh(connection, second, bi=bi, readonly_role=role, fault=fail)
        with connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"select bundle_id from {loader.identifier(bi)}.active_release"
                )
                assert cursor.fetchone()[0] == first.name
                cursor.execute(
                    "select count(*) from pg_namespace where nspname=%s", [versions[1]]
                )
                assert cursor.fetchone()[0] == 0
                cursor.execute(
                    f"select collected_review_mean_rating from {loader.identifier(bi)}.v_store_review_summary where canonical_store_id=%s",
                    ["synthetic-a"],
                )
                assert cursor.fetchone()[0] == 3.0
        metadata_path = second / "manifest.json"
        original_metadata = metadata_path.read_text()
        metadata = json.loads(original_metadata)
        metadata["reviews_release_id"] = "synthetic-wrong-reviews"
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        with pytest.raises(ValueError, match="lineage"):
            loader.refresh(connection, second, bi=bi, readonly_role=role)
        metadata_path.write_text(original_metadata, encoding="utf-8")
        target = second / "fact_review.csv"
        target.write_text(
            target.read_text() + "CORRUPT_SYNTHETIC_TEST", encoding="utf-8"
        )
        with pytest.raises(ValueError, match="checksum"):
            loader.refresh(connection, second, bi=bi, readonly_role=role)
    finally:
        connection.rollback()
        with connection:
            with connection.cursor() as cursor:
                # Exact random names verified absent before this test; never wildcard cleanup.
                for name in owned:
                    cursor.execute(
                        "select pg_get_userbyid(nspowner)=current_user from pg_namespace where nspname=%s",
                        [name],
                    )
                    owner = cursor.fetchone()
                    if owner is not None:
                        assert owner[0], "Refuse cleanup of an unowned namespace"
                        cursor.execute(
                            sql.SQL("drop schema {} cascade").format(
                                sql.Identifier(name)
                            )
                        )
                if created_role:
                    cursor.execute(sql.SQL("drop role {}").format(sql.Identifier(role)))
        connection.close()


def test_cli_uses_single_real_transaction_context_and_closes_on_failure(
    tmp_path, monkeypatch
):
    """Run the real CLI with a native connection, stopping before any SQL/write.

    The old outer connection context raises ProgrammingError instead of reaching
    cursor(), so this specifically covers the entrypoint missed by refresh tests.
    """

    class ReachedRefreshSQL(Exception):
        pass

    class StopBeforeSQL(psycopg2.extensions.connection):
        def cursor(self, *args, **kwargs):
            raise ReachedRefreshSQL("Synthetic stop before SQL")

    real_connection = psycopg2.connect(
        host="127.0.0.1",
        port=15432,
        dbname="postgres",
        user="fnb_admin",
        password=(ROOT / ".auth/fnb/admin_password").read_text().strip(),
        connect_timeout=5,
        connection_factory=StopBeforeSQL,
    )
    rid = "a" * 24
    gold = "b" * 24
    release = tmp_path / "releases" / rid
    release.mkdir(parents=True)
    (release / "manifest.json").write_text('{"fixture_only":true}', encoding="utf-8")
    (tmp_path / "current.json").write_text(
        json.dumps(
            dict(bundle_id=rid, manifest_sha256=wh.sha(release / "manifest.json"))
        ),
        encoding="utf-8",
    )
    # This is a CLI wiring fixture, not a forged published release. Source/row
    # integrity is independently exercised by the real bundle refresh test above.
    monkeypatch.setattr(
        wh, "verify_bundle", lambda path: dict(bundle_id=rid, gold_release_id=gold)
    )
    monkeypatch.setattr(psycopg2, "connect", lambda **kwargs: real_connection)
    original_read = Path.read_text

    def read_text(path, *args, **kwargs):
        if path.as_posix().startswith("/run/secrets/"):
            return "synthetic-cli-fixture-only"
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "load_analytics.py",
            "--root",
            str(tmp_path),
            "--result",
            str(tmp_path / "result.json"),
            "--expected-gold",
            gold,
        ],
    )
    try:
        with pytest.raises(ReachedRefreshSQL, match="before SQL"):
            runpy.run_path(str(ROOT / "scripts/load_analytics.py"), run_name="__main__")
        assert real_connection.closed, "The CLI must close the connection on failure"
        assert not (
            tmp_path / "result.json"
        ).exists(), "A failed load must not emit success evidence"
    finally:
        real_connection.close()
