# Portfolio repository audit

Audit date: 2026-11-08. Scope: the current pipeline worktree, including existing uncommitted implementation files and locally retained result artifacts. This audit changed only the root README and this report. Existing changes were preserved; no application/data changes, live collection, package installation, commit or push were performed.

## GitHub publication check — 8 November 2026

The complete pipeline worktree was checked before publication on
`feature/fnb-data-pipeline`: **604 tests passed in 13.99s** across 28 explicitly
selected offline modules (modified/new test modules plus catalog, categories,
prepare_catalog, OSM, Google reliability and Google queue regressions). No live
browser collection, source download or dataset rebuild was invoked. The existing
CPython environment was reused; this is not a clean-environment installation test.

GitHub Actions now also selects the new eligibility, spatial, Silver, comparison,
ER V1–V3, Google address enrichment, address-reference pilot, candidate ranking
and POI-input tests. A local pass does not claim a remote Actions run has passed.

Code, documentation and bounded test fixtures accompany publication. Ignored
`data/`, `output/`, databases, browser profiles, cookies and private session files
remain local. Saved real-data results referenced below are not downloadable from
a fresh clone. Changed files were checked for secret patterns and runtime-data
paths before staging; no credential values were included in the check output.

## Project narrative and maturity

The strongest portfolio narrative is a **multi-source F&B location integration platform**: versioned acquisition, explicit geography/taxonomy, source-preserving normalization, technical observation deduplication, explainable entity-resolution proposals and recoverable local processing. Crawling supplies observations to this system.

| Maturity | Evidence-backed assessment |
|---|---|
| Implemented and fixture-validated | Catalog preparation/validation, OSM JSON-element adapter, Google acquisition/review reliability mechanisms, fixed-scope Overture comparison, Silver normalization, comparison features, proposal policies and address enrichment |
| Saved real-data pilot outputs | Overture eligibility/spatial pairs; Google Silver; ER V1/V2/V3; observed addresses/street summaries; source profiling |
| Experimental | Matching thresholds and V3 lexical rules; Overture experiment publishers; Foody source evaluation; browser transport comparison; isolated OSM road-reference/address feasibility pilot |
| Planned or unverified | Accepted cross-source identity map, canonical Gold business catalog, complete review coverage, recommendation model/service, scheduled end-to-end deployment and HCMC-wide capacity/coverage |

The old README described an earlier restoration milestone and incorrectly relegated now-present Google/Silver/ER code to future work. Conversely, numbered architecture documents describe targets that are still ahead of the implementation. The rewrite resolves both discrepancies.

## Evidence ledger

Paths below are local ignored artifacts, shown as code rather than public download links. Their absence in a clone is expected. Source/report links remain navigable in this workspace; some implementation reports are currently untracked and must accompany any later portfolio publication.

| Claim | Evidence inspected |
|---|---|
| 6,199 contextual Silver rows; 725 candidates = 557 original + 168 hierarchy additions; 32 missing addresses; 24 missing-from-release records | `output/overture-fnb-eligibility-m1/releases/3bf02697d8f1d8fc34ccb752/dq_metrics.json`; [eligibility code](../experiments/overture_gold_contract/eligibility.py); [schema inventory](CURRENT_DATA_SCHEMA.md) |
| 3,442 spatial pairs at 50 m; 4,206 distance comparisons; 3.0456605 s runtime; zero heuristic likely-duplicate flags | `output/spatial-prededup-v2/summary.json`; [implementation/method](../experiments/overture_gold_contract/SPATIAL_PREDEDUP.md). Timing is one saved local run with no controlled repeated benchmark |
| 452 metadata observations; 426 identities with metadata; 460 current rows; 476 source links; 251 zero-candidate inventory IDs; 16 shared identities | `output/google-silver-m1/releases/bc742a4d46f7e312cd248550/reports/google_silver_dq_summary.json`; both Silver Parquet footers; [code](../src/fnb_platform/google_silver.py) |
| ER V1/V2/V3 classify the same 476 relationships; V3 = 346 positive, 6 negative, 90 review, 34 insufficient-data | `output/er-m2-v1`, `output/er-m2-v2`, `output/er-m2-v3-experimental`: active release pointers, manifests, summaries and `er_results.parquet`; [V3 implementation report](M2_ER_V3_EXPERIMENTAL.md) |
| 460 address identity rows, 725 provisional source rows, 381 parsed addresses, 392 analytical-street identities, 346 provisional attachments, 46 normalized street names represented by 47 dictionary rows | `output/google-address-enrichment-v1/releases/4470ff1dee0accf7dfc86568/summary.json` and three Parquet footers; [report](GOOGLE_ADDRESS_ENRICHMENT_V1.md) |
| Complementary Overture/Foody field presence; source-selection decision inconclusive | [Metric CSV](../experiments/source_evaluation/results/20261101-khanh-hoi-v3/quality_metrics.csv), [decision](../experiments/source_evaluation/results/20261101-khanh-hoi-v3/04_source_selection_decision.md), detailed profiling JSON/CSV and code |
| No defensible browser-framework winner | [Benchmark report](benchmarks/browser-framework-comparison.md): stopped after one of 48 trials on source restriction, remaining trials unrun |
| Outbox replay, incremental stop and incomplete-coverage safeguards | [Review repository](../crawl_experiment/storage/review_repository.py), [Parquet writer](../crawl_experiment/storage/parquet_review_writer.py), [incremental window](../crawl_experiment/orchestration/review_incremental.py), [reconciliation](../crawl_experiment/orchestration/review_reconciliation.py), fixture tests |

Checksums of the active manifests matched `current.json` for Google Silver, ER V1/V2/V3 and address enrichment. Parquet footer counts matched the advertised grains: 460 identities, 476 relationships per ER version, 725 provisional address links and 47 dictionary rows. Artifact payload checksums were also checked against these manifests. Stored V3 validation evidence reports deterministic reruns, reversed-input equality and preserved inputs; this audit did not rebuild real-data publications.

## Validation performed

- **482 tests passed in 10.07 s** across 21 explicitly selected fixture modules: catalog, categories, prepare_catalog, osm, overture_eligibility, spatial_prededup, google_silver, comparison_features, entity_resolution, entity_resolution_v2, entity_resolution_v3, google_address_enrichment, google_reliability, google_live_queue, google_candidate_ranking, google_candidate_recall, google_place_resolution, google_place_details, google_poi_input, google_vietnamese_review_ui and google_search_discovery.
- Used existing CPython 3.12.7 environment at `D:/Projects/integration-recomentdation/output/ci01-2ac84b90/venv/Scripts/python.exe`, with `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`, `-B -m pytest`, explicit module paths, `-q -p no:cacheprovider` and temporary test output outside the repository. No live crawl or source acquisition was invoked.
- An initial stricter socket-blocked attempt produced nine failures because blocking every socket connection interfered with Windows asyncio loopback initialization. Re-running the selected fixtures without that blanket patch passed. This is a validation-harness limitation, not evidence of nine application regressions. The repository's separate socket-blocked baseline selects only four core modules.
- Executed `--help` for catalog preparation and all four documented offline wrappers; checked acquisition flags against argparse and snapshot-protection code. No package install, real download, catalog rebuild or publication rerun was attempted. Fresh-environment installation remains unverified.
- Checked Markdown fences, local link targets, Mermaid flowchart structure and node references, Git whitespace, and documentation for credential/session-value patterns. Remote Mermaid rendering services returned HTTP 403; rendered layout remains unverified.
- Reviewed [MIT](../LICENSE), [NOTICE](../NOTICE.md), boundary attribution, ignored runtime/session paths and review schema privacy exposure. Consulted official [Overture attribution](https://docs.overturemaps.org/attribution/), [OSM licensing](https://www.openstreetmap.org/copyright) and [Google Maps terms](https://www.google.com/intl/en/help/terms_maps/). Foody collection/storage/commercial permissions remain unverified; no legal clearance is claimed.

## Gaps before HCMC expansion

1. **Identity quality:** independent branch-sensitive labels, held-out precision/recall, blocking recall, reviewed decisions and stable canonical IDs. Proposal counts cannot substitute for these measurements.
2. **Rights and privacy:** permitted feeds, record-level attribution, review minimization/retention and a rights-cleared public demonstration dataset. Browser profiles and sessions must remain private.
3. **Reproducibility:** a complete browser/experiment dependency specification, sanitized fixtures, portable input locations and a clean installation/rerun. The shell's default Python is MSYS2 and lacks the core dependencies; the successful test environment is separate. Several inputs remain in a historical sibling worktree.
4. **Geographic coverage:** a versioned ward registry, cross-boundary entity handling, consistent extraction scopes and administrative-history policy. Current source candidates do not establish complete Khánh Hội coverage.
5. **Capacity and operations:** representative density/resource benchmarks, acquisition restrictions/cost, shared job ownership, publication contention tests, backup/restore and lake-wide review identity strategy. Neither distributed execution nor city-scale throughput has been validated.
6. **Data products:** publish canonical Gold only after identity/rights gates; then add SQL analytics and independently evaluated recommendations. Intended users and applications are a product direction, not demonstrated adoption.

## HCMC-area baseline verification

The recruiter-focused README revision independently read the retained September snapshot in the sibling historical worktree, without modifying it:

- Input: `../integration-recomentdation-fnb-data-platform-20261028/data/bronze/overture/places.parquet` and adjacent `source_metadata.json`.
- Release: `2026-09-23.1`; retrieved `2026-09-30T17:00:49.160859+00:00`.
- Actual Parquet rows: **378,036**. Recounting rows whose taxonomy hierarchy contains `food_and_drink` gives **77,966**, matching metadata.
- Input SHA-256 matches metadata: `aa3ef77fadae38aaa5982953718a0c74d3b3c20e1512935833c1b58623253af8`.
- Bbox: west 106.55, south 10.60, east 106.90, north 10.95. Metadata explicitly sets `bbox_is_not_city_total=true` and `hcm_city_polygon_available=false`.

Therefore, **77K+ HCMC-area source records** is supported. A complete HCMC business census, 77K crawled/enriched Google entities, or 77K integrated canonical businesses is not supported. The later 725-record Khánh Hội candidate set uses a different release and geography/policy; it is not a city-wide processing result.

## Incremental results

Inspected the saved manifest at `../integration-recomentdation-fnb-data-platform-20261028/output/overture-m22/khanh-hoi-2026-09-23.1_to_2026-10-21.0-v1/manifest.json`. Its common eight-field projection compares fixed-scope buffered ward extracts, not full HCMC snapshots:

| State | Records |
|---|---:|
| NEW | 546 |
| UPDATED | 2,665 |
| UNCHANGED | 2,988 |
| MISSING_FROM_RELEASE | 24 |

The manifest identifies 1,859 metadata-only updates. These states describe source changes; they do not prove business openings/closures. [Comparison code](../src/fnb_platform/overture_incremental.py) rejects incompatible schemas/scopes. [Overture Silver code](../experiments/overture_silver/consumer.py) executes DuckDB SQL against staged Parquet for row count, distinct source IDs and DQ counts. DuckDB is implemented in this experimental path; it is not the sole transformation engine.

## CV claim review

The following assessment uses the four CV bullets supplied by the owner after the first audit. Their intended business applications are reasonable project goals, not demonstrated adoption.

| Original claim | Assessment | Required qualification |
|---|---|---|
| Built a multi-source platform targeting 77K+ source locations across HCMC | Supported as a target and retained regional baseline | Specify 77,966 Overture F&B records in an HCMC-area bbox; actual integration pilot is Khánh Hội. No completed 77K crawl or full-city coverage |
| Automated crawlers with pagination, controlled concurrency and retries for Google Maps, Foody and Overture | Substantially supported mechanisms, overly broad source grouping | Google/Foody have acquisition experiments; Overture is release-based ingestion, not browser crawling. Foody remains outside current Silver/ER; review completeness and live-source reliability are not validated |
| Cleaning, standardization, ER and modeling to match duplicates and maintain source mappings | Implemented features and candidate mappings; verified deduplication would overclaim | Say explainable ER proposals and candidate source links. V3 has 346 positive proposals; all 476 relationships remain ER_PENDING. No accepted canonical business merge |
| Incremental ETL with Airflow orchestration, DQ, monitoring/logging, idempotency, atomic publication and recovery | Incremental/reliability mechanisms supported; executable Airflow orchestration unsupported | Airflow appears in future design only, with no DAG/dependency/runtime evidence. Logging/metrics are local run artifacts, not a deployed monitoring service. Atomicity concerns specific file/pointer publications, not a cross-store transaction |

**Tech stack correction:** list Python, DuckDB, PyArrow/Parquet, Shapely, SQLite and SeleniumBase; Playwright is supported for experiments. Remove Apache Airflow from the implemented stack or explicitly label it **planned orchestration design**.

Suggested evidence-aligned CV bullets:

1. Built a local F&B integration pipeline with a 77,966-record Overture HCMC-area baseline and a 725-candidate Khánh Hội pilot for market research and location analytics.
2. Developed Google Maps and Foody acquisition workflows with pagination, bounded concurrency, retries and checkpoints, alongside versioned Overture snapshot ingestion; staged Google metadata for cross-source comparison.
3. Standardized Vietnamese names and addresses and implemented explainable ER V3, producing 476 candidate source links with 346 positive match proposals while preserving uncertain cases for review.
4. Implemented snapshot change detection, data quality checks, structured run logs, SQLite outbox replay and validated versioned publication; documented a future Airflow orchestration design.

The latest local active pointers and payload checksums were rechecked for Google Silver, ER V3 and address enrichment. Actual ER Parquet values confirm the reported classification counts and `ER_PENDING` on every row. No Airflow runtime was inferred from design documents.

The shortened README's exact four-module pytest selection was rerun: **112 passed in 3.31 s**. All four documented processing wrappers returned their expected `--help` output. Local Markdown links/anchors, code fences, Mermaid static syntax and Git whitespace checks passed. No new renderer result is claimed. A complete `git diff -- README.md` was exported outside the repository for review; it compares the working tree against HEAD, which predates both documentation revisions.
