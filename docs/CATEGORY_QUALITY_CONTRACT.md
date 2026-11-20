# Category Quality V1 — Silver features and Pre-Gold audit

This is a derived quality release, not Canonical Gold. The CLI does not read
human labels, create identity decisions, change ER V3, or rewrite source Silver.

## Reuse and input contracts

- `categories.py`: reuse the explicit `PRODUCT_CATEGORY_GROUPS` entries. Its
  legacy `other_food` fallback is deliberately not used by this pipeline.
- `eligibility.py`: reuse the published, checksum-validated M242 eligibility
  release, including hierarchy/alternates absent from Overture current-state
  Silver. Join/validate the exact source IDs and shared category/display values
  against the authoritative Silver file pinned by Google M1. Do not rebuild it.
- `google_silver.py`: reuse Unicode/accent/case comparison normalization, typed
  Google identity validation, canonical JSON, release verification and atomic
  pointer publication conventions. Original category strings remain unchanged.
- ER V3: use every existing composite relationship; require the same Google and
  Overture input hashes as V3. Classification and `ER_PENDING` remain unchanged.
- `config/category_taxonomy_v1.json`: versioned exact aliases, additions to the
  existing product mapping, explicit non-F&B hierarchy roots, and audit-only
  name terms. Its checksum, code checksums and all input checksums are pinned in
  the release manifest. There is no substring/fuzzy category assignment.

## Grains and schemas

`category_normalization.parquet`: one record per Overture source ID in the
pinned release, or per Google `(google_id_kind, google_identity_value)`.

- Source keys, original name/address, `raw_categories_json` and provenance.
- `normalized_categories` (list), `unknown_values`, `category_evidence_json`.
- `candidate_classification`: `FNB_CANDIDATE`, `NON_FNB_CANDIDATE`, or
  `UNKNOWN_CATEGORY`; these describe source evidence, not verified businesses.
- `baseline_fnb_candidate`, ward inclusion, unchanged eligibility reason/policy.
- `taxonomy_path_valid`, `broad_fnb_evidence`, DQ codes/details, mapping/rule version.
- Each DQ detail has code, severity, concrete reason, actual values, source
  observation reference, rule version and recommended inspection action.

`category_audit.parquet`: one row per ER composite relationship, plus one
source-only row for each source POI with no relationship. Singleton keys contain
NULL for the absent source; they are not failed or confirmed ER links.

- Both source keys, original names/addresses, raw and mapped category lists.
- `category_relation`: `CONSISTENT`, `SUBCATEGORY_DIFFERENCE`, `SOURCE_ONLY`,
  `CATEGORY_CONFLICT`, or `UNKNOWN`.
- `comparison_status`: that relation, overridden by `NEEDS_IDENTITY_RECHECK`
  for unaccepted shared/multiple candidates, F&B/non-F&B conflicts, or an ER
  `NON_MATCH_CANDIDATE`. The underlying relation remains visible.
- Concrete reason/codes, evidence, provenance, ER classification/status,
  rule/mapping versions, baseline and ward flags, shared/multiple flags.
- Future Gold category contract fields, described below. In the current real
  run all rows are `PROVISIONAL`, `store_id=NULL`, category `unknown`/`PENDING`.

`category_conflicts_for_review.csv` is an inspection export, not a human-label
import/workflow. It contains cross-source conflicts, identity-recheck cases,
subcategory differences, taxonomy anomalies and excluded-taxonomy name hints.
It preserves source IDs and raw values and uses UTF-8 BOM for Excel.

`category_dq_summary.json` separates source DQ, baseline eligibility, cross-source
relations and identity-recheck counts. `manifest.json` includes artifact byte
checksums and Parquet logical checksums. `current.json` activates only a fully
validated release. Failed publication retains the previous active pointer.

## Source normalization and missing information

Allowed groups: `restaurant`, `cafe_coffee`, `beverage`, `bakery_dessert`,
`fast_food`, `bar`, `other_food`, `non_fnb`, `unknown`.

Overture primary and explicit alternates contribute distinct mapped categories.
Use the deepest recognized hierarchy label/basic category as fallback when the
primary is unmapped; do not count all ancestors as additional subcategories.
A generic food ancestor does not turn an unrecognized leaf into `other_food`.
`other_food` requires an explicit known mapping such as `food_and_drink` or
`food_truck_stand`. Recognized non-food hierarchy roots describe source taxonomy;
they do not prove the source classified the physical business correctly.

The raw food hierarchy can support broad F&B candidacy while the particular
subcategory remains unknown. Conversely, an unmapped non-food leaf can retain
`non_fnb` from its explicit broad root. `unknown_values` and warning counts make
this incomplete leaf mapping visible; they are not identical to POI counts whose
entire normalized category is `unknown`.

Google display labels take precedence over the stored normalized copy because
the latter is a representation of the same field, not independent evidence.
Use the normalized copy only if display is absent. A disagreement is a warning.
Support explicitly supplied category lists; do not split opaque category strings
on punctuation. Preserve category field provenance, fallback observation/run and
historical metadata conflicts. Historical values are not current category labels.

Missing, malformed and unrecognized values have separate warnings. NULL never
creates a category conflict. Ambiguous labels such as food manufacturing,
generic wholesale or `Phòng trà` remain unknown until scope/evidence is clarified.
No name-based category inference is performed. Food-related name tokens only
identify possible taxonomy mislabels for inspection; they can be false alarms.

## Coverage and baseline preservation

Normalize all 6,199 Overture rows, not only the 725 selected rows, and all current
Google identities. Preserve the original 557 + 168 reconciliation unchanged.
Broad F&B diagnostics over the full extraction include the surrounding buffer;
they are not an expansion of the 725 ward candidate catalog.

Unlinked source rows remain in the audit. The original 251 baseline Overture IDs
without Google candidates remain source-only, and no Google category is inferred
for them. Unlinked taxonomy exclusions cannot be cross-validated from Google.
The name-hint inspection is a bounded diagnostic, not an exhaustive false-negative
evaluation or a reason to add/remove candidates.

Category membership counts are non-additive across groups for multi-category
POIs. ER-link counts can also repeat a Google identity or Overture ID; use source
normalization keys for source POI counts. None of these counts is a verified
physical-store count or a claim of complete category/geographic source coverage.

## Accepted identity dependency and future Gold field selection

The in-memory `accepted_mappings` interface is tested using fixtures only. Each
record needs the composite source link, an existing `store_id`, explicit
`identity_status=MATCH`, independent `decision_ref`, and `mapping_version`.
Crawler labels, ER proposal classifications and category agreement do not satisfy
this contract. There is deliberately no CLI option to import decisions.

HARD FAIL for unknown relationships, repeated accepted links, one Overture ID
assigned to different stores, or one Google identity assigned to different
stores. Shared IDs/multiple candidates remain evidence, not implicit merge rules.

Gold category contract (present as preparation fields; no Gold table published):

| Field | Selection policy |
|---|---|
| `store_id` | Only supplied by an independently accepted canonical mapping. Never generated here. |
| `canonical_category` | Select only one identical, recognized category from both sources, with an accepted identity and no source DQ warning. Otherwise `unknown`. |
| `canonical_category_group` | Same coarse group as canonical category in V1; raw taxonomy retains finer source detail. |
| `category_validation_status` | `SOURCE_AGREEMENT_ACCEPTED_IDENTITY` for that selection; otherwise `PENDING`. This does not assert independent human verification of the category. |
| `category_source` | `overture+google` only when selected; otherwise NULL. No blanket source priority. |
| `category_rule_version` | The category quality rule version. |
| `category_provenance` | Both source references/observations and accepted mapping reference when selected; otherwise NULL. All raw audit evidence is retained separately. |

Source-only categories, differing F&B subcategories, multiple categories, unknown
labels and unresolved conflicts do not receive an arbitrary canonical selection.
Conflicting category sets across accepted links of the same store block selection
for that store. Accepted identity can resolve identity uncertainty, not a category
conflict. The current CLI never produces accepted/verified store mappings.

## Check ownership and severity

| Layer | HARD FAIL | WARNING / retained uncertainty |
|---|---|---|
| Silver features | Missing required schema/key columns; empty/duplicate source keys; invalid Google typed identity; input/release checksum or lineage mismatch; lost source records; missing file-level provenance | Missing/invalid/unmapped optional category; invalid taxonomy path; historical metadata conflict; possible mislabel; display/normalized disagreement |
| Pre-Gold | Lost/duplicate ER composite link; dangling source reference; unexpected ER contract; inconsistent accepted mapping fixture | Cross-source category conflict; different F&B subcategory; shared ID/multiple candidates; missing source category |
| Gold contract preparation | Invalid category/group; selection without accepted identity/provenance; unknown presented as selected; conflict presented as supported | `unknown`/`PENDING` while a decision/category is unresolved |

Canonical Gold still depends on actual accepted identity/canonical mappings and
resolution of category conflicts. Business scope for ambiguous food-sector labels
and dictionary gaps needs evidence/approval before changing their mapping. No
Human Validation, ER-B, crawler, review processing or Gold implementation is part
of this release.
