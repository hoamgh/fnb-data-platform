# Google Reviews Silver V1 (offline)

This transformation consumes persisted review events, not a new Google crawl.
It does not create `fact_review`, canonical-store assignments, ER decisions or
review observations for encounters the crawler did not persist.

## Input scope and integrity

The active Google Places Silver pointer and manifest select the inputs. The
`input_runs` in its DQ summary must reference run plans, summaries and dispatch
databases pinned by that manifest. No names or Overture–Google relationship joins
are used. V1's audited real input is 12 runs, 389 review Parquet files, 10,929
persisted events and 10,139 unique source review IDs.

All relevant input JSON, Parquet and SQLite files are checksummed before use and
rechecked before activation. SQLite is opened read-only for state/outbox counts;
review payloads come from Parquet, not the legacy `reviews` compatibility view.
Nonempty WALs and unflushed outboxes block publication until inputs are frozen.
File membership is checked again at publication. Original raw files are unchanged.

The separate `excluded_review_files_inventory.parquet` inventories review files
from other known worker/job runs, using file SHA256, schema fingerprint and footer
row counts only. Those rows never enter the observations/current transformation.
The previously operator-discarded run remains excluded by the existing discovery
policy. The audited older input has 69 files / 1,573 rows across seven runs.

## Artifacts and grains

| Artifact | Grain / primary key |
|---|---|
| `review_identity_bridge.parquet` | One capture directory × crawler store ID, scoped by source run/job. PK `bridge_id`; composite uniqueness `(capture_ref, crawler_store_id)`. Includes unsuccessful/unbound captures. |
| `silver_review_observations.parquet` | One actual persisted source event. PK `observation_id`. Duplicate physical copies of the same source event collapse with all file references retained. |
| `silver_reviews_current.parquet` | One valid `(source, source_review_id)` with an unambiguous Google identity. PK that logical key; stable `logical_review_id`. |
| `review_collection_inventory.parquet` | One Google typed identity × recorded capture attempt, plus explicitly marked no-attempt coverage entries. PK `inventory_id`. |
| `excluded_review_files_inventory.parquet` | One excluded legacy review file; never an admitted review observation. |
| `review_dq_summary.json` | Counts, DQ, input runs, readiness and comparison to the prior audit baseline. |

`manifest.json` records complete Arrow schemas, input/code hashes, runtime versions,
file checksums and logical table hashes. `summary.json` repeats the DQ summary for
compatibility with the existing publication framework.

### Identity bridge

Columns include `bridge_id`, `source_run`, `job_ref`, `capture_ref`,
`crawler_store_id`, `google_id_kind`, `google_identity_value`, `binding_status`,
`evidence_json`, `dq_flags`, `rule_version`.

The bridge compares job input store ID to capture metadata store ID. Identity is
extracted using the existing `google_silver.identity()` validator from the actual
place-details snapshot and confirmed resolved-page URL. A selected/ranked search
candidate is not proof that the entity was opened. Conflicting metadata produces
an unbound/ambiguous bridge; it is never resolved by business name.

An event's `run_id` must select one capture **inside its job**. It is not a globally
unique capture key. The typed identity must exist in the pinned Places Silver.
Neither `data_id` nor collection eligibility is promoted to official Place ID or
verified Overture identity.

### Review observations

Core columns: `observation_id`, `source`, `source_review_id`, typed Google identity,
`rating`, `review_text`, `published_at`, `last_modified_at`, `observed_at`,
`timestamp_status`, `timestamp_source`, `content_hash`, `content_hash_computed`,
`event_type_original`, `reviewer_name`, `review_date_raw`, `owner_response`,
`review_url`, `image_urls`, `current_eligible`, `dq_flags`, `rule_version`.

Traceability columns: `source_event_id`, `observation_id_basis`, original run ID,
crawler store ID, bridge ID, source run/job/capture refs, file provenance
(file path, SHA256, row index, footer event ID), original timestamp values/status,
identity candidates and provenance JSON. Invalid review ID/rating source values
remain available in provenance. Review text is not duplicated into provenance.

ID policy: use the existing footer event ID; if absent, use file SHA256 + row index.
Malformed footer membership is a hard failure. Same event ID with different
payloads is also a hard failure. Stable source review IDs must be nonempty strings
without whitespace/control characters. Missing/invalid IDs remain in observations
but are quarantined from current; text/author similarity is never a dedup key.

### Current review policy

Current rows retain Google binding, `current_observation_id`, selected review fields,
`first_seen_at`, `last_observed_at`, observation/hash counts, provenance and DQ.
`first_seen_at` means earliest persisted observation available, not necessarily
the first time a crawler encountered the review.

Eligible current candidates require a valid review ID, source, content hash and
identity bridge. Choose a valid observed UTC timestamp before missing/invalid
timestamps, then latest observed time, then content hash and observation ID as
stable tie-breakers. Selection takes the **whole observation**: no old text is
backfilled into a newer NULL. Equal-time content disagreement is explicitly flagged.

All observations of a logical review with different Google identities are flagged
`CROSS_IDENTITY_REVIEW_CONFLICT`, quarantined and excluded from current. This does
not reassign reviews or merge stores. Quarantine is represented by observations
with `current_eligible=false`, not a separate service.

Multiple content hashes produce `CONTENT_VARIATION_NOT_VERIFIED_EDIT`. Changes
may be extraction variation or actual source changes; original `INSERT`/`UPDATE`
labels remain unchanged. A later missing review is not a DELETE. Rating-only/NULL
text reviews are retained. Relative date labels are retained without guessing
exact timestamps. Untrusted/invalid timestamp pairs normalize to NULL with flags.

### Collection inventory

Includes typed identity, capture/bridge refs, advertised and collected counts,
persisted event count, original crawler status/coverage, scroll limits/count,
idle limit, parse errors, requested sort, sort confirmation evidence, business
status, collection status and provenance. Capture metadata supplies the advertised
count; current Places count is used only on explicitly marked no-attempt entries.

No-attempt entries use `inventory_entry_kind=NO_RECORDED_COLLECTION_ATTEMPT` and
`collection_attempt_id=NULL`; they are not invented crawl attempts. Their zero
collected count means zero persisted records in scope, not zero Google reviews.
This permits complete accounting of all 460 Places identities.

The crawler's `COMPLETE`/`FULL` is preserved as original evidence and never proves
complete Google review coverage. Requested `newest` is not confirmation that the
UI sort succeeded. `is_complete_google_review_coverage` is false throughout V1.

## Quality and readiness

Hard failures block pointer activation: missing/changed pinned inputs, invalid
required schema, invalid event footer, conflicting payload for one event ID,
unflushed outbox/nonempty WAL, event with no capture in its job, uniqueness/FK/
coverage failures, or changed source file membership during publication.

Quarantine includes unresolved/contradictory identity, event/store mismatch,
invalid stable review ID/source, content hash mismatch/missing hash and cross-
Google logical-review conflicts. It preserves observations but excludes current.
Optional text/rating/timestamp gaps are DQ warnings, not automatic row deletion.

`silver_readiness=READY` means source-level review input is usable with these DQ
and coverage semantics. It does not approve canonical store linkage, production
Gold, complete review coverage, or a demographic/recommendation model. Quarantined
observations or current reviews lacking any trustworthy observation time result
in `NOT_READY`. Immutable releases and atomic pointer replacement reuse the
existing publication framework; no source release is rewritten.

## PowerShell

```powershell
Set-Location 'D:\Projects\integration-recomentdation-fnb-data-platform-20261028-pipeline'
$py = 'D:\Projects\integration-recomentdation\output\ci01-2ac84b90\venv\Scripts\python.exe'

& $py -B scripts/google_reviews_silver.py `
  --google-silver-root output/google-silver-m1 `
  --output-root output/google-reviews-silver-v1

$env:PYTHONPATH = 'src;.'
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
& $py -B -m pytest tests/test_google_reviews_silver.py `
  tests/test_google_reliability.py tests/test_google_m2_limits.py `
  -q --tb=short -p no:cacheprovider --basetemp output/rvs-check
```

Use a short pytest temporary path on Windows to avoid legacy MAX_PATH failures
inside the crawler's partitioned Parquet fixture paths. No new dependency or live
Google request is required by this CLI.
