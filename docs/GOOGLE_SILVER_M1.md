# M1 — Google Maps Silver staging and technical dedup

Status: implemented offline; no crawler, review, Gold or ER implementation changed. Policy: `google-silver-technical-v1`.

Mandatory contract for the future M2 ER implementation:
[explainability, evidence, traceability and review queue](M2_ER_EXPLAINABILITY_CONTRACT.md).
M1 DQ fields are inputs to that contract, not yet complete M2 explanations.

## Entry points

- `src/fnb_platform/google_silver.py`: input inspection, normalization, technical identity grouping, candidate links, DQ, validation and atomic publication.
- `scripts/google_silver.py`: CLI wrapper; no environment initialization or installation.
- `tests/test_google_silver.py`: focused offline regression tests.
- Reuses `_canonical` / `_digest` from `overture_incremental.py` and the existing versioned-release/atomic-pointer publication pattern. The strict Google typed-ID parser follows the existing resolver URL protocol without importing Selenium or invoking its matching logic.
- Existing Overture Silver is read for source-ID membership and fingerprinting; it is not copied/rebuilt.

## Actual input scope

| Crawl root | Queue records | Result observations | Metadata files | Status |
|---|---:|---:|---:|---|
| `D:\Projects\integration-recomentdation-fnb-data-platform-20261028\output\google-eight-20261104-230831-609` | 8 | 8 | 7 | FINISHED |
| `D:\Projects\integration-recomentdation-fnb-data-platform-20261028-pipeline\output\google-candidate-recall-v2-pilot` | 20 | 20 | 2 | FINISHED |
| `D:\Projects\integration-recomentdation-fnb-data-platform-20261028-pipeline\output\google-m2-725-v5\batch-001` | 4 | 4 | 2 | FINISHED |
| `D:\Projects\integration-recomentdation-fnb-data-platform-20261028-pipeline\output\google-m2-dlq-core-v5` | 1 | 1 | 1 | FINISHED |
| `D:\Projects\integration-recomentdation-fnb-data-platform-20261028-pipeline\output\google-m2-dlq-reviewfix-v2` | 355 | 46 | 3 | STOPPED |
| `D:\Projects\integration-recomentdation-fnb-data-platform-20261028-pipeline\output\google-m2-dlq530-moderate-v1` | 530 | 530 | 186 | FINISHED |
| `D:\Projects\integration-recomentdation-fnb-data-platform-20261028-pipeline\output\google-m2-full-v5` | 721 | 721 | 191 | FINISHED |
| `D:\Projects\integration-recomentdation-fnb-data-platform-20261028-pipeline\output\google-m2-remaining-core-v5-four` | 277 | 277 | 12 | FINISHED |
| `D:\Projects\integration-recomentdation-fnb-data-platform-20261028-pipeline\output\google-m2-unresolved286-sameward-v4` | 286 | 163 | 6 | STOPPED |
| `D:\Projects\integration-recomentdation-fnb-data-platform-20261028-pipeline\output\google-m2-unresolved342-recall-v2` | 342 | 342 | 36 | FINISHED |
| `D:\Projects\integration-recomentdation-fnb-data-platform-20261028-pipeline\output\google-recovery-candidate25-v1` | 25 | 25 | 3 | FINISHED |
| `D:\Projects\integration-recomentdation-fnb-data-platform-20261028-pipeline\output\google-recovery-pilot-v1` | 4 | 4 | 3 | FINISHED |

The 11 pipeline queues plus the explicitly supplied original-worktree 8-POI run cover 725 Overture IDs. Input discovery only checks known dispatch layouts; it does not traverse browser profiles or review lakes. The original worktree is read-only.

Excluded by the previous operator discard decision:
- `D:\Projects\integration-recomentdation-fnb-data-platform-20261028-pipeline\output\google-m2-discarded-pilot-20261105-112744\batch-001`

All supplied roots must have a final FINISHED/STOPPED summary. RUNNING dispatch rows in stopped roots are retained as interrupted/unknown coverage. Metadata files are discovered independently of result pointers, so a saved snapshot can be staged even when its dispatch write was interrupted; association must come from the saved job input or dispatch reference. Only one checkpoint per root is read to inventory the actual checkpoint shape; no checkpoint is modified.

Input JSON, metadata, dispatch DB and existing Silver are fingerprinted. Metadata/source-association contradictions and invalid IDs go to DQ rather than being silently assigned. Rows merely present in candidate_rankings do not become links. Actual selected, opened/rejected and resolved-page candidates are distinguished and preserved as unverified relationships.

## Results

| Metric | Count |
|---|---:|
| Raw metadata snapshots | 452 |
| Valid typed metadata observations | 452 |
| Overture source IDs covered | 725 |
| Google IDs with metadata | 426 |
| Google current rows, including explicit missing-metadata identities | 460 |
| Repeated metadata observations reduced by typed identity | 26 |
| Exactly repeated normalized observation records | 14 |
| Source candidate relationships | 476 |
| Google IDs shared by multiple Overture IDs | 16 |
| Overture IDs with multiple Google candidates | 2 |
| Google IDs with conflicting non-null stable normalized fields | 0 |
| Google IDs whose observation records changed, including missing/display/volatile changes | 11 |
| Missing metadata Google IDs | 34 |
| Potentially suspicious links, overlapping flags | 77 |
| Zero valid selected/opened relationship inventory | 251 |

474 source IDs have links; 251 remain explicitly inventoried. These are source/identity counts, not verified businesses. A Google ID shared by two sources does not merge those sources.

## Normalization and current-value policy

1. Google key is `(google_id_kind, google_identity_value)`. Data-ID hex is lowercased; Place-ID case is preserved. Explicit IDs and typed entity URL IDs must agree; name/distance never deduplicate identities. A Google data_id is not an official Place ID.
2. Keep original display name/address/phone/category from valid field observations. Normalized comparison text casefolds, removes Vietnamese accents and punctuation/icons. Phone cleaning removes formatting and converts a 00 prefix to +; no country code is invented.
3. Validate coordinate pairs together, latitude/longitude ranges and finite numbers; validate rating 1–5 and nonnegative integral review_count. Invalid values become NULL with DQ, never clipped.
4. Observation order is: valid timezone-aware timestamp before missing/invalid time, then latest UTC timestamp, then count of usable stable fields, then stable hash of metadata path/content. No filesystem mtime is used for choosing business facts.
5. Per stable field, use the newest valid non-null value; if the newest snapshot is incomplete, retain the older valid field and its exact observation/hash/run/path in field_provenance_json. Fallback fields are explicit. Coordinates remain a coherent pair. Raw snapshots stay unchanged.
6. UNKNOWN business status counts as unknown for fallback. latest_business_status_raw preserves the current snapshot status even when an older known status is retained. Such a retained status is best-known historical evidence, not a new verification.
7. Volatile open_now and next-change labels never fall back to older visits. An open_now=True conflict with retained closed status becomes NULL with an explicit conflict flag. Next-change labels are suppressed for retained permanent/temporary closure. This is the documented exception to stable-field NULL retention.
8. Reuse already extracted weekly intervals, validate ISO weekdays/time ranges/offsets, sort days and intervals, and omit localized labels from normalized JSON. Unknown days remain unknown, not closed. Raw display hours remain separately available. If only unstructured hours/status labels exist, retain raw display and flag HOURS_DISPLAY_WITHOUT_NORMALIZED_WEEK rather than invent weekly intervals.
9. Distinct non-null normalized stable values constitute conflicts; missing/UNKNOWN values, localized display variants and time-sensitive open_now changes alone do not. Stable-field conflicts retain all value/observation-ID evidence.
10. Valid selected identities without metadata get explicit MISSING_METADATA rows, with NULL business fields and DQ; they are not successful store snapshots.

## Source-link and ER policy

- Grain: `(overture_id, google_id_kind, google_identity_value)`. Every valid distinct selected/opened relationship from supplied inputs survives.
- All rows are ER_PENDING. No independently verified ER decisions were supplied. Crawler VERIFIED/RESOLVED/IDENTITY_UNVERIFIED labels are preserved as original_crawler_identity_status, never promoted to ER.
- Most recent evidence uses parsed UTC timestamp plus deterministic evidence/reference tie-breakers. selection_evidence_json and opened_evidence_json retain the most recent selection scores and post-open evidence independently.
- source_observation_refs and selection_policies preserve references to prior evidence in the unchanged raw inputs; observation_count counts evidence records, not distinct crawls.
- Shared Google IDs, multiple Google candidates for one source, missing metadata, opened-but-unconfirmed pages and recorded street/house/identifier contradictions are flagged for later ER. No additional fuzzy matching is introduced.
- Zero-candidate inventory is in the DQ summary and details CSV, with explicit ZERO_SELECTED_OBSERVED, SELECTED_ID_INVALID, INPUT_ONLY_NOT_ATTEMPTED or INTERRUPTED_UNKNOWN semantics. Interrupted rows are not assumed to prove absence of a selected candidate.

## Output schema

Google places is one current typed Google identity, not one confirmed physical store. Source links are candidate relationships, not an accepted identity map. Original Overture fields remain in the existing Overture Silver and are joined by source_id.

### silver/google_places.parquet

| Column | Parquet type |
|---|---|
| `google_id_kind` | `string` |
| `google_identity_value` | `string` |
| `metadata_status` | `string` |
| `name_display` | `string` |
| `name_normalized` | `string` |
| `address_display` | `string` |
| `address_normalized` | `string` |
| `phone_display` | `string` |
| `phone_normalized` | `string` |
| `category_display` | `string` |
| `category_normalized` | `string` |
| `business_status` | `string` |
| `latitude` | `double` |
| `longitude` | `double` |
| `rating` | `double` |
| `review_count` | `int64` |
| `open_now` | `bool` |
| `website` | `string` |
| `menu_url` | `string` |
| `price_range` | `string` |
| `plus_code` | `string` |
| `opening_hours_json` | `string` |
| `opening_hours_display_json` | `string` |
| `next_opening_time_json` | `string` |
| `next_closing_time_json` | `string` |
| `source_url` | `string` |
| `current_observation_id` | `string` |
| `observed_at` | `string` |
| `source_run` | `string` |
| `metadata_path` | `string` |
| `metadata_sha256` | `string` |
| `latest_business_status_raw` | `string` |
| `field_provenance_json` | `string` |
| `conflicting_values_json` | `string` |
| `observation_count` | `int64` |
| `distinct_metadata_count` | `int64` |
| `source_overture_count` | `int64` |
| `has_metadata_conflict` | `bool` |
| `conflict_fields` | `list<item: string>` |
| `fallback_fields` | `list<item: string>` |
| `missing_fields` | `list<item: string>` |
| `dq_flags` | `list<item: string>` |

### silver/source_links.parquet

| Column | Parquet type |
|---|---|
| `overture_id` | `string` |
| `google_id_kind` | `string` |
| `google_identity_value` | `string` |
| `er_status` | `string` |
| `metadata_status` | `string` |
| `evidence_kind` | `string` |
| `observed_at` | `string` |
| `source_run` | `string` |
| `source_observation_ref` | `string` |
| `selection_policy` | `string` |
| `original_crawler_identity_status` | `string` |
| `collection_status` | `string` |
| `candidate_evidence_json` | `string` |
| `selection_evidence_json` | `string` |
| `opened_evidence_json` | `string` |
| `observation_count` | `int64` |
| `overture_google_candidate_count` | `int64` |
| `google_overture_source_count` | `int64` |
| `overture_has_conflicting_candidates` | `bool` |
| `google_shared_by_overture_ids` | `bool` |
| `source_observation_refs` | `list<item: string>` |
| `selection_policies` | `list<item: string>` |
| `dq_flags` | `list<item: string>` |

## Reports and validation

- `reports/google_silver_dq_summary.json`: counts, input runs, raw missing-field counts, conflicts, candidate collisions, zero-candidate ID inventory and logical hashes.
- `reports/google_silver_dq_details.csv`: issue_code, overture_id, Google key, raw observation reference, field and detail_json. Reasons can overlap; counts are not additive across issue categories.
- `manifest.json`: input/code hashes, artifact checksums, schemas, primary keys and logical hashes.
- `output/google-silver-m1-validation-all-runs/validation_checks.json`: independent checks against the actual data.

**41 tests passed** across Google Silver and the existing Overture eligibility tests. Verified place/link primary keys, exact preservation of all valid observed relationships, source coverage, FK integrity, coordinate/rating ranges, stable tie order, NULL fallback and field provenance, independent-ID grouping, invalid identities, opened/rejected candidates, interrupted metadata, opening-hours normalization, idempotent reruns, input immutability and publication failure paths.

Actual-data validation: 460 unique place keys, 476 unique link keys, 474 linked source IDs plus 251 explicitly inventoried source IDs, zero orphan FKs and zero invalid numeric output rows. Reversed-root rerun produced identical logical outputs and pointer bytes. Validation and final publication have matching logical hashes. Existing Overture Silver SHA-256 is unchanged. Size/mtime checks on 2537 review Parquet/SQLite files are unchanged; the transformation never reads review text, writes reviews or reassigns their store IDs.

## Publication and exact rerun command

The whole tested bundle is written under a hidden stage directory, verified, then renamed to an immutable release. Only after that does os.replace switch current.json. Readers resolve current.json once and verify manifest_sha256. Failed publication leaves the previous pointer intact; incomplete stages are not active datasets.

Current release: `D:\Projects\integration-recomentdation-fnb-data-platform-20261028-pipeline\output\google-silver-m1\releases\bc742a4d46f7e312cd248550`

```powershell
Set-Location 'D:\Projects\integration-recomentdation-fnb-data-platform-20261028-pipeline'
$python = 'D:\Projects\integration-recomentdation\output\ci01-2ac84b90\venv\Scripts\python.exe'
& $python -B -m scripts.google_silver `
  --discover-root 'output' `
  --crawl-root 'D:/Projects/integration-recomentdation-fnb-data-platform-20261028/output/google-eight-20261104-230831-609' `
  --overture-silver 'D:/Projects/integration-recomentdation-fnb-data-platform-20261028/output/overture-silver-m241/releases/2026-10-21.0/current_state.parquet' `
  --output-root 'output/google-silver-m1'
```

Repeat the identical command for an idempotent rerun; it validates the existing immutable release. A new input/code signature creates a new release rather than overwriting one. Do not manually modify published Parquet or manifest files.

Inspect the active paths:

```powershell
$active = Get-Content 'output/google-silver-m1/current.json' -Raw | ConvertFrom-Json
$releasePath = "output/google-silver-m1/releases/$($active.release_id)"
Get-ChildItem "$releasePath/silver"
Get-ChildItem "$releasePath/reports"
```

Run focused tests:

```powershell
& $python -B -m pytest tests/test_google_silver.py tests/test_overture_eligibility.py -q --basetemp output/pytest-google-silver-m1-rerun
```

## Remaining risks / deferred work

- 34 identities have no usable metadata snapshot; they remain explicit missing-metadata rows, not verified stores.
- 251 source IDs have no valid selected/opened link in supplied evidence, including 6 interrupted/unknown IDs. This is not business closure or proof that no Google business exists.
- 16 shared Google identities and 2 sources with multiple Google identities require downstream ER; 77 links carry overlapping suspicious-evidence flags.
- 11 Google IDs have non-identical observation records; no incompatible non-null normalized stable fields were found under the documented conflict definition. This does not prove raw observations or operating facts are complete.
- Historical false-positive candidate selections remain ER_PENDING; this milestone intentionally does not correct branch matching or business dedup.
- Opening hours, phone, category, rating and review coverage can be missing. Missing collected reviews never imply zero reviews on Google.
- No review dedup/transformation, canonical catalog, Gold, recommendation, Foody or new crawler execution is part of this milestone.
