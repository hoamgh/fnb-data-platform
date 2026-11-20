#!/bin/sh
set -eu
# psql variables quote credentials as SQL literals; do not echo them.
export PGPASSWORD="$(cat /run/secrets/admin_password)"
psql -v ON_ERROR_STOP=1 -U fnb_admin -d postgres \
  --set=crawler_password="$(cat /run/secrets/crawler_password)" \
  --set=airflow_password="$(cat /run/secrets/airflow_password)" <<'SQL'
SELECT 'CREATE ROLE fnb_owner NOLOGIN' WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname='fnb_owner') \gexec
SELECT format('CREATE ROLE fnb_crawler LOGIN PASSWORD %L', :'crawler_password') WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname='fnb_crawler') \gexec
SELECT format('CREATE ROLE fnb_airflow LOGIN PASSWORD %L', :'airflow_password') WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname='fnb_airflow') \gexec
SELECT 'CREATE DATABASE fnb_platform OWNER fnb_owner' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname='fnb_platform') \gexec
SELECT 'CREATE DATABASE airflow OWNER fnb_airflow' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname='airflow') \gexec
REVOKE ALL ON DATABASE fnb_platform FROM PUBLIC;
GRANT CONNECT ON DATABASE fnb_platform TO fnb_crawler;
REVOKE ALL ON DATABASE airflow FROM PUBLIC;
GRANT CONNECT ON DATABASE airflow TO fnb_airflow;
SQL
psql -v ON_ERROR_STOP=1 -U fnb_admin -d fnb_platform <<'SQL'
SET ROLE fnb_owner;
\i /fnb-migrations/001_crawler.sql
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA crawler TO fnb_crawler;
GRANT SELECT, INSERT, UPDATE ON crawler.store_state TO fnb_crawler;
SQL
