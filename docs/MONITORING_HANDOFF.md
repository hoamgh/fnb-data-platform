# Monitoring handoff

Scope: sources for a future monitoring implementation. No Prometheus/Grafana/lineage service or monitoring agent is deployed here.

## Architecture and persistence

Crawler → Bronze/Silver Parquet → canonical ER / optional Human Review → Gold Parquet → DuckDB/dbt → immutable read-only DuckDB snapshot → Superset.

| Store | Actual responsibility / implementation |
|---|---|
| SQLite `reviews.sqlite3` / configured state DB | `ReviewRepository`: review identities/hashes, `pending_review_events` durable outbox, checkpoint/incremental/reconciliation metadata |
| SQLite same state DB | `PlaceAccessRepository`: access outcomes, retries/cooldown and next eligibility |
| SQLite `dispatch.sqlite3` | `scripts/google_live_workers.py`: task ownership and PENDING/RUNNING/result queue; crashed RUNNING work is not silently reclaimed |
| JSON / JSONL files | `CheckpointRepository`, `CrawlArtifacts`: per-store progress, failure stage, timings, retries, `dlq.jsonl`, run/worker summaries |
| Parquet | `ParquetReviewWriter`: append-only change evidence, atomic batches, event membership; Silver selects logical reviews later |
| PostgreSQL `fnb_platform.crawler.store_state` | `PostgresCrawlState`: status, last success, watermark, run ID, SQLite checkpoint reference, submission count; not raw reviews or the dispatch queue |
| PostgreSQL `airflow` | Airflow metadata; task/DagRun states, attempts and scheduling metadata |
| PostgreSQL `fnb_superset` | Superset users, database/dataset/chart/dashboard definitions; no analytics serving tables |
| DuckDB | Seven dimensional tables, three marts, release audit and shared `bi` views; separate immutable snapshot for BI |

PostgreSQL incremental state is opt-in through `FNB_CRAWL_STATE_BACKEND=postgres` and private `FNB_CRAWL_STATE_DSN`, or `IncrementalConfig(state_backend=...)`. SQLite remains active even with that backend. `PostgresCrawlState.complete()` flushes SQLite outbox, proves persisted Parquet identity/hash coverage, then commits PostgreSQL state. Failed/incomplete capture does not advance the watermark. `attempt_count` counts checkpoint submissions, including replay; it is not the number of browser retries. PostgreSQL mode currently disables watermark/known-streak early stopping because modification-time ordering is unverified.

Airflow `fnb_google_crawl.state` explicitly reads PostgreSQL regardless of the standalone incremental opt-in. Its live `checkpoint` task explicitly submits PostgreSQL state; replay returns `REPLAY_STATE_UNCHANGED`. The incremental hook in the working tree depends on pending crawler changes excluded from the mentor package. Do not report PostgreSQL as the sole/default crawler persistence backend.

## Evidence sources and useful metrics

| Source | Metrics / how to obtain them |
|---|---|
| Airflow UI/API/metadata | DagRun/task state, retry attempt, start/end/duration, queue age and last successful run. Three manual DAGs; a successful native runner or `airflow dags test` is not proof of a scheduled DagRun |
| `airflow_logs` volume at `/opt/airflow/logs` | Airflow task logs; webserver/scheduler stdout via Docker logs. Access through the deployment's own Compose project |
| `/demo/airflow/<dag>/<campaign>/` | `*.result.json`, `*.task.log`, stage logs and `configuration.sha256`; inspect explicit stage status, run ID, release ID/hash. Admission/report-only stages do not necessarily publish releases |
| Crawler `checkpoints/<store>.json`, `summary.json`, `progress*.jsonl`, `dlq.jsonl`, worker summaries | Completion/partial/limited/error counts, failure stage, capture duration, browser startup cost, retries/cooldown, failed/unresolved jobs. These can contain source identities; restrict access |
| SQLite outbox/dispatch/access tables | Pending event count, queue state counts and oldest pending age, failed jobs, next eligibility. Query read-only; do not treat review change-event count as unique logical reviews |
| PostgreSQL `crawler.store_state` | Last success age, watermark age, status distribution and checkpoint submissions. Exclude known synthetic `test-` rows when assessing live activity |
| Publication `<root>/current.json` + `releases/<id>/manifest.json` | Exact current/pinned release, manifest/artifact SHA256, provenance, row counts and lineage. Never infer success from a directory existing or an exit code alone |
| dbt `dbt-target/run_results.json` + `manifest.json` | Models/tests pass/fail/error/skip, execution time and lineage. Check matching invocation/artifact hashes; do not accept stale successful results |
| DuckDB `dimensional.release_audit`, `bi.v_data_quality` | Store/mapping/review reconciliation, unresolved links, taxonomy unknown, missing addresses/dates/ratings; compare within one pinned Gold release |
| `.local/duckdb-final/<run>/warehouse/` | `current.json`, bundle manifest, `serving-current.json`, `serving/<id>/snapshot.json`, `superset-assets.json`, `query-timings.json`; snapshot identity/hash, serving URI, backend and query samples |
| `.local/duckdb-final/<run>/` | `checkpoint.json`, `verification.json`, stage logs, `browser-*/superset-verification.json`, `resources.jsonl`; fresh browser checks, basemap responses and sampled container RAM |

Initial alert candidates: failed/stuck tasks, old last-success/watermark, growing pending outbox/queue, missing/corrupt publication, dbt failures, changed source mapping fanout, review conservation failures and unavailable UI/tiles. Set freshness thresholds against the chosen refresh schedule; no live schedule/SLO is implied by this manual pilot.

dbt Docs are served by the setup runner with project profiles and database variables selected automatically. Four projects remain separate; missing Gold/Area build evidence must be reported rather than replaced with synthetic lineage. Superset reads a versioned snapshot read-only; refresh imports a new exact URI and retains old files for existing readers.

The browser-verification job shares Superset's network namespace, so `127.0.0.1:8088` refers to the server container, not the Docker host. This tests the actual localhost development origin while retaining the original light Alidade Smooth map. Treat provider tile HTTP 401/429 separately from failed warehouse queries; provider domain/key rules apply outside local development.

## Verification boundaries

- M5 Real `m5-real-01` at `97c7bf1`: Docker/dbt/BI PASS; 725 stores, 302 accepted, 423 source-only, 10,139 reviews = 6,942 attached + 3,197 unmatched; 7 models/41 tests, 20 charts/3 tabs and pre/during/post-refresh UI PASS.
- Earlier public `demo-02` passed on synthetic data. It is not market evidence.
- Windows Docker-only fresh-source workflow `mentor-fresh-01`: PASS for first setup, stop, rerun and restart on an isolated Git export plus explicit DX files. The corrected driver resumed the unchanged frozen export after verifying file hashes and the first manifest; it did not rewrite child fingerprints. Synthetic Demo dbt passed 7 models/41 tests; fresh-image focused tests were 55 PASS / 9 platform-dependent SKIP. Browser checks passed 27/27 before/during/after refresh and after restart. Logical table hashes/counts were conserved and dbt Docs opened. Environment/frozen-source regressions passed as part of 23 host tests. Evidence: `.local/mentor-verification/mentor-fresh-01/verification.json`. Linux-host execution remains NOT_VERIFIED.
- Airflow business E2E and live crawler E2E are not certified by M5 BI. The Google live worker explicitly requires Windows native containment and operator permission/session; the default Docker crawler service only runs a synthetic browser smoke.
- Keep operational monitoring credentials separate from user cookies/sessions. Do not publish source review text, profiles, credentials, DSNs, raw logs or database files in Git.
