"""Transactional PostgreSQL serving refresh. Input is an immutable dimensional CSV bundle."""

import argparse
from contextlib import closing
from decimal import Decimal
import json
import math
from pathlib import Path
import re
import statistics
import time

from fnb_platform.analytics.warehouse import (
    MODELS,
    logical_hash,
    require,
    sha,
    verify_bundle,
)

TYPES = {
    "VARCHAR": "text",
    "BIGINT": "bigint",
    "INTEGER": "integer",
    "SMALLINT": "smallint",
    "DOUBLE": "double precision",
    "FLOAT": "real",
    "BOOLEAN": "boolean",
    "DATE": "date",
}
KEYS = dict(MODELS, release_audit=["gold_release_id"])


def identifier(value):
    require(
        bool(re.fullmatch("[a-z_][a-z0-9_]{0,62}", value)), "Invalid SQL identifier"
    )
    return '"' + value + '"'


def content_checks(cursor, schema, manifest):
    for name, spec in manifest["tables"].items():
        columns = ",".join(identifier(c) for c, t in spec["columns"])
        order = ",".join(str(i + 1) for i in range(len(spec["columns"])))
        cursor.execute(
            f"select {columns} from {identifier(schema)}.{identifier(name)} order by {order}"
        )
        rows = cursor.fetchall()
        require(
            len(rows) == spec["rows"] and logical_hash(rows) == spec["logical_sha256"],
            "PostgreSQL content mismatch: " + name,
        )
    for table, column, expected in [
        ("fact_store_snapshot", "gold_release_id", manifest["gold_release_id"]),
        ("bridge_store_source", "gold_release_id", manifest["gold_release_id"]),
        ("release_audit", "gold_release_id", manifest["gold_release_id"]),
        ("fact_review", "association_gold_release_id", manifest["gold_release_id"]),
        ("fact_review", "source_release_id", manifest["reviews_release_id"]),
        ("fact_store_snapshot", "places_release_id", manifest["places_release_id"]),
    ]:
        cursor.execute(
            f"select count(*) from {identifier(schema)}.{table} where {column} is distinct from %s",
            [expected],
        )
        require(
            cursor.fetchone()[0] == 0,
            "Warehouse release lineage mismatch: " + table + "." + column,
        )


def reconcile(cursor, bi, expected):
    cursor.execute(f"select metric,value from {identifier(bi)}.v_data_quality")
    observed = dict(cursor.fetchall())
    fields = {
        "canonical_records": "canonical_store_count",
        "accepted_links": "accepted_mapping_count",
        "source_only_records": "source_only_store_count",
        "source_mapping_rows": "source_mapping_rows",
        "nonaccepted_candidate_pairs": "unresolved_mapping_count",
        "logical_reviews": "current_logical_reviews",
        "attached_reviews": "attached_reviews",
        "unmatched_canonical_reviews": "unmatched_reviews",
    }
    for metric, field in fields.items():
        require(
            observed[metric] == expected[field],
            "Warehouse/Gold KPI mismatch: " + metric,
        )
    categories = expected["category_validation_counts"]
    require(
        observed["category_provisional"]
        == categories.get("PROVISIONAL_SOURCE_AGREEMENT", 0)
        and observed["category_pending"] == categories.get("PENDING", 0),
        "Gold taxonomy status reconciliation mismatch",
    )
    require(
        observed["logical_reviews"]
        == observed["attached_reviews"] + observed["unmatched_canonical_reviews"],
        "Review conservation mismatch",
    )
    return observed


def market_checks(cursor, bi, expected):
    cursor.execute(
        f"select * from {identifier(bi)}.v_area_market order by area_business_key"
    )
    names = [c[0] for c in cursor.description]
    rows = [dict(zip(names, r)) for r in cursor.fetchall()]
    require(len(rows) == 1, "Market reference area grain mismatch")
    row = rows[0]
    reference = expected["area_reference"]
    for field in [
        "area_business_key",
        "reference_area_km2",
        "reference_population",
        "reference_year",
        "reference_release_id",
        "reference_source_version",
        "reference_source_url",
        "reference_population_basis",
        "reference_area_basis",
        "reference_quality_status",
    ]:
        require(row[field] == reference[field], "Market reference mismatch: " + field)
    require(
        row["observed_store_count"] == expected["canonical_store_count"]
        and row["accepted_link_count"] == expected["accepted_mapping_count"]
        and row["source_only_count"] == expected["source_only_store_count"],
        "Market store reconciliation mismatch",
    )
    for field, denominator, factor in [
        ("observed_stores_per_km2", "reference_area_km2", 1),
        ("observed_stores_per_1000_residents", "reference_population", 1000),
    ]:
        value = row[denominator]
        if value:
            require(
                math.isclose(
                    row[field],
                    row["observed_store_count"] * factor / value,
                    rel_tol=1e-12,
                ),
                "Market density mismatch: " + field,
            )
        else:
            require(row[field] is None, "Missing/zero denominator must remain NULL")
    # PostgreSQL numeric division returns Decimal. Keep exact native values for
    # the checks above; only presentation/report measures become JSON numbers.
    return {
        key: float(value) if isinstance(value, Decimal) else value
        for key, value in row.items()
    }


def refresh(
    connection,
    bundle,
    *,
    bi="bi",
    readonly_role="fnb_analytics_readonly",
    fault=lambda stage: None,
):
    from psycopg2 import sql

    manifest = verify_bundle(bundle)
    require(
        re.fullmatch("[a-f0-9]{24}", manifest["bundle_id"]) is not None,
        "Invalid bundle ID",
    )
    version = "analytics_" + manifest["bundle_id"]
    identifier(bi)
    identifier(version)
    identifier(readonly_role)
    started = time.perf_counter()
    # This context commits once, after loading, content verification, views and KPI verification.
    with connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "select pg_advisory_xact_lock(hashtext('fnb_analytics_refresh'))"
            )
            cursor.execute(
                "select exists(select from pg_namespace where nspname=%s)", [version]
            )
            reused = cursor.fetchone()[0]
            if not reused:
                cursor.execute(
                    sql.SQL("create schema {}").format(sql.Identifier(version))
                )
                for name, spec in manifest["tables"].items():
                    columns = ",".join(
                        identifier(c) + " " + TYPES[t] for c, t in spec["columns"]
                    )
                    cursor.execute(
                        f"create table {identifier(version)}.{identifier(name)} ({columns})"
                    )
                    query = sql.SQL(
                        r"copy {}.{} from stdin with(format csv,header true,null '\N')"
                    ).format(sql.Identifier(version), sql.Identifier(name))
                    with (Path(bundle) / (name + ".csv")).open(
                        encoding="utf-8", newline=""
                    ) as stream:
                        cursor.copy_expert(query, stream)
                for name, keys in KEYS.items():
                    cursor.execute(
                        f'alter table {identifier(version)}.{identifier(name)} add primary key ({",".join(identifier(k) for k in keys)})'
                    )
                for table, column in [
                    ("dim_store", "canonical_store_id"),
                    ("dim_category", "category_business_key"),
                    ("dim_area", "area_business_key"),
                    ("dim_date", "full_date"),
                ]:
                    cursor.execute(
                        f"alter table {identifier(version)}.{table} add unique ({column})"
                    )
                cursor.execute(
                    f"alter table {identifier(version)}.fact_review add unique(source,source_review_id)"
                )
                for column in [
                    "source",
                    "source_review_id",
                    "google_id_kind",
                    "google_identity_value",
                    "source_release_id",
                ]:
                    cursor.execute(
                        f"alter table {identifier(version)}.fact_review alter column {column} set not null"
                    )
                cursor.execute(
                    f"alter table {identifier(version)}.fact_review add check(review_count=1 and (review_rating is null or review_rating in (1,2,3,4,5)))"
                )
                cursor.execute(
                    f"alter table {identifier(version)}.fact_store_snapshot add check(store_count=1 and accepted_link_flag in (0,1) and source_only_flag in (0,1) and accepted_link_flag+source_only_flag=1)"
                )
                for table, col, target, key in [
                    ("fact_store_snapshot", "store_key", "dim_store", "store_key"),
                    (
                        "fact_store_snapshot",
                        "category_key",
                        "dim_category",
                        "category_key",
                    ),
                    ("fact_store_snapshot", "area_key", "dim_area", "area_key"),
                    ("fact_review", "store_key", "dim_store", "store_key"),
                    ("fact_review", "review_date_key", "dim_date", "date_key"),
                    ("bridge_store_source", "store_key", "dim_store", "store_key"),
                ]:
                    cursor.execute(
                        f"alter table {identifier(version)}.{table} add foreign key ({col}) references {identifier(version)}.{target}({key})"
                    )
                index_specs = [
                    ("dim_store", "latitude,longitude"),
                    ("fact_store_snapshot", "category_key"),
                    ("fact_store_snapshot", "area_key"),
                    ("fact_review", "store_key"),
                    ("fact_review", "review_date_key"),
                    ("fact_review", "google_id_kind,google_identity_value"),
                ]
                for table, cols in index_specs:
                    cursor.execute(
                        f"create index on {identifier(version)}.{table} ({cols})"
                    )
                for name in manifest["tables"]:
                    cursor.execute(f"analyze {identifier(version)}.{identifier(name)}")
            content_checks(cursor, version, manifest)
            fault("before_switch")
            cursor.execute(
                sql.SQL("create schema if not exists {}").format(sql.Identifier(bi))
            )
            source = (
                Path(__file__).resolve().parents[1] / "docker/analytics/serving.sql"
            )
            # Only validated SQL identifiers enter the template, never arbitrary input SQL.
            cursor.execute(
                source.read_text(encoding="utf-8")
                .replace("@version@", identifier(version))
                .replace("@bi@", identifier(bi))
            )
            cursor.execute(
                f"create or replace view {identifier(bi)}.active_release as select %s::text as bundle_id, gold_release_id from {identifier(version)}.release_audit",
                [manifest["bundle_id"]],
            )
            cursor.execute(
                sql.SQL("grant usage on schema {} to {}").format(
                    sql.Identifier(bi), sql.Identifier(readonly_role)
                )
            )
            cursor.execute(
                sql.SQL("grant select on all tables in schema {} to {}").format(
                    sql.Identifier(bi), sql.Identifier(readonly_role)
                )
            )
            observed = reconcile(cursor, bi, manifest["expected"])
            market = market_checks(cursor, bi, manifest["expected"])
            fault("before_commit")
    return dict(
        status="REUSED_VERIFIED" if reused else "LOADED",
        bundle_id=manifest["bundle_id"],
        gold_release_id=manifest["gold_release_id"],
        kpis=observed,
        market_reference=market,
        refresh_seconds=round(time.perf_counter() - started, 3),
    )


def query_timings(connection, bi="bi"):
    queries = {
        "store_kpi": f"select sum(store_count),sum(accepted_link_flag) from {identifier(bi)}.v_store_overview",
        "category": f"select category_name,sum(store_count) from {identifier(bi)}.v_store_overview group by category_name",
        "bbox": f"select canonical_store_id from {identifier(bi)}.v_store_overview where latitude between 10.75 and 10.77 and longitude between 106.69 and 106.72",
    }
    result = {}
    with connection.cursor() as cursor:
        for name, query in queries.items():
            samples = []
            for _ in range(5):
                started = time.perf_counter()
                cursor.execute(query)
                cursor.fetchall()
                samples.append((time.perf_counter() - started) * 1000)
            result[name] = dict(
                first_ms=round(samples[0], 3),
                warm_median_ms=round(statistics.median(samples[1:]), 3),
                runs=5,
            )
    return result


if __name__ == "__main__":
    import psycopg2

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/analytics-out"))
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--expected-gold", required=True)
    args = parser.parse_args()
    pointer = json.loads((args.root / "current.json").read_text())
    require(
        re.fullmatch("[a-f0-9]{24}", pointer["bundle_id"]) is not None,
        "Invalid bundle pointer",
    )
    bundle = args.root / "releases" / pointer["bundle_id"]
    require(
        sha(bundle / "manifest.json") == pointer["manifest_sha256"],
        "Bundle pointer checksum mismatch",
    )
    manifest = verify_bundle(bundle)
    require(
        manifest["gold_release_id"] == args.expected_gold
        and re.fullmatch("[a-f0-9]{24}", args.expected_gold) is not None,
        "Loader requires the explicitly selected real Gold release",
    )
    # refresh() owns the transaction. closing() owns only the connection lifetime;
    # a psycopg2 connection context here would recursively re-enter refresh's context.
    with closing(
        psycopg2.connect(
            host="postgres",
            dbname="fnb_analytics",
            user="fnb_analytics_writer",
            password=Path("/run/secrets/analytics_writer_password").read_text().strip(),
        )
    ) as connection:
        report = refresh(connection, bundle)
        report["query_timings"] = query_timings(connection)
    with closing(
        psycopg2.connect(
            host="postgres",
            dbname="fnb_analytics",
            user="fnb_analytics_readonly",
            password=Path("/run/secrets/analytics_readonly_password")
            .read_text()
            .strip(),
        )
    ) as readonly:
        with readonly.cursor() as cursor:
            cursor.execute(
                "select current_setting('default_transaction_read_only'), has_table_privilege(current_user,'bi.v_store_overview','SELECT'), has_table_privilege(current_user,'bi.v_store_overview','INSERT')"
            )
            require(
                cursor.fetchone() == ("on", True, False),
                "BI credential is not read-only",
            )
            require(
                reconcile(cursor, "bi", manifest["expected"]) == report["kpis"],
                "Read-only serving KPI mismatch",
            )
            require(
                market_checks(cursor, "bi", manifest["expected"])
                == report["market_reference"],
                "Read-only market reference mismatch",
            )
        report["readonly_serving"] = "PASS"
    args.result.parent.mkdir(parents=True, exist_ok=True)
    args.result.write_text(
        json.dumps(report, indent=2, allow_nan=False), encoding="utf-8"
    )
    print(
        json.dumps(
            dict(
                status=report["status"],
                bundle_id=report["bundle_id"],
                kpi_reconciliation="PASS",
            )
        )
    )
