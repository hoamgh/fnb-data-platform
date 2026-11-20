"""Provision two isolated databases in the existing PostgreSQL service; never reset data."""

from pathlib import Path
import psycopg2
from psycopg2 import sql


def secret(name):
    return (Path("/run/secrets") / name).read_text().strip()


if __name__ == "__main__":
    connection = psycopg2.connect(
        host="postgres",
        dbname="postgres",
        user="fnb_admin",
        password=secret("admin_password"),
    )
    connection.autocommit = True
    with connection.cursor() as cursor:
        for role, file in [
            ("fnb_analytics_writer", "analytics_writer_password"),
            ("fnb_analytics_readonly", "analytics_readonly_password"),
            ("fnb_superset_metadata", "superset_metadata_password"),
        ]:
            cursor.execute(
                "select exists(select from pg_roles where rolname=%s)", [role]
            )
            if not cursor.fetchone()[0]:
                cursor.execute(
                    sql.SQL(
                        "create role {} login nosuperuser nocreatedb nocreaterole password %s"
                    ).format(sql.Identifier(role)),
                    [secret(file)],
                )
        for database, owner in [
            ("fnb_analytics", "fnb_analytics_writer"),
            ("fnb_superset", "fnb_superset_metadata"),
        ]:
            cursor.execute(
                "select exists(select from pg_database where datname=%s)", [database]
            )
            if not cursor.fetchone()[0]:
                cursor.execute(
                    sql.SQL("create database {} owner {}").format(
                        sql.Identifier(database), sql.Identifier(owner)
                    )
                )
            cursor.execute(
                sql.SQL("revoke all on database {} from public").format(
                    sql.Identifier(database)
                )
            )
        cursor.execute(
            "grant connect on database fnb_analytics to fnb_analytics_readonly"
        )
        cursor.execute(
            "alter role fnb_analytics_readonly set default_transaction_read_only = on"
        )
        cursor.execute(
            "alter role fnb_analytics_readonly set statement_timeout = '30s'"
        )
    connection.close()
    connection = psycopg2.connect(
        host="postgres",
        dbname="fnb_analytics",
        user="fnb_admin",
        password=secret("admin_password"),
    )
    with connection:
        with connection.cursor() as cursor:
            cursor.execute("revoke create on schema public from public")
    connection.close()
    print(
        "Analytics/BI databases provisioned without touching Airflow/crawler databases."
    )
