# Active Mapping and Canonical Gold Staging V1

This release activates Auto-Accept V1 **for Gold Staging only**. Production Gold
publication is not implemented. ER V3, Silver, Category Quality V1, the policy
rules and human decision history remain unchanged.

## Activation and manifests

`scripts/gold_staging.py activate` reruns the existing Auto-Accept evaluator on
its pinned inputs and current human state. It promotes the recomputed result to
a separate versioned active mapping root. The original policy evidence hashes,
statuses, reasons and machine suggestions are retained. New activation evidence
binds them to the evaluated shadow release and `GOLD_STAGING_ONLY` scope.

- AUTO_ACCEPTED is `MACHINE_ACCEPTED`, never human verified.
- HUMAN_ACCEPTED is `HUMAN_VERIFIED_LINK` after existing group checks.
- Human NON_MATCH/UNSURE overrides are preserved by the reused policy.
- PENDING/CONFLICT rows remain present but cannot provide merged Google fields.
- No history events are appended or modified by activation.

The activation pointer is independent of the human ledger and production Gold.
Its manifest records the human root and pointer checksum, including an absent
pointer. Staging refuses stale activation if human decisions change; rerun
activation first. Input checks also run before atomic pointer replacement.

`build` resolves active mapping and Category Quality `current.json` manifests,
checks their checksums/release lineage, and reads the exact pinned Overture and
Google Silver paths. Overture is pinned transitively through the checked Category
Quality manifest; the policy's input projection does not reread Overture Silver.
No source dataset is rebuilt/copied to replace the existing Silver release.

## Catalog grain, identity and source accounting

The staging catalog has one provisional anchor per baseline Overture candidate.
Stable IDs use UUIDv5 with the URL namespace and
`fnb-platform/overture/<source_id>`. They do not depend on row order, a release,
business name or Google candidate. A new identity decision changes attachments,
not the Overture anchor ID. This does not claim ER-B or verified physical-store
uniqueness: shared/duplicate source observations remain unresolved.

| Model | Grain and purpose |
|---|---|
| `dim_store` | One baseline Overture candidate anchor; attach Google fields only through active AUTO_ACCEPTED/HUMAN_ACCEPTED links. |
| `store_source_mapping` | One source/typed-source-ID attachment: every baseline Overture ID plus accepted Google identities. |
| `link_accounting` | Every ER composite link, including PENDING/CONFLICT, with an Overture anchor reference; `merged_store_id` is NULL for unresolved/rejected links. |
| `source_accounting` | Every baseline Overture ID and every Google Silver typed identity, including Google observations not assigned to a catalog anchor. |

The 251 Overture IDs with no Google candidate remain source-only. Overture IDs
with unresolved candidates also remain source-only; multiple candidate links
must be counted by distinct Overture ID when measuring stores. Unassigned Google
observations remain in accounting and Silver, rather than being silently dropped
or automatically treated as new F&B stores in the ward. Other Overture Silver
rows outside baseline eligibility remain in Silver, outside this catalog scope.

## Field and category selection

For an accepted attachment, use non-NULL Google display name/address, otherwise
the original Overture field. Coordinates are selected as a valid lat/lon pair
from Google, otherwise the Overture pair. Source-only records use Overture.
Original source fields remain unchanged. Google field-level observation
provenance and pinned file hashes are retained in `field_provenance_json`.
Unknown business status remains UNKNOWN, never automatically closed.

Canonical category is chosen only when the two linked source category sets are
identical recognized singletons, the category relation is CONSISTENT and the
source category DQ flags are empty. AUTO_ACCEPTED yields
`PROVISIONAL_SOURCE_AGREEMENT`; human-accepted identity yields
`SOURCE_AGREEMENT_HUMAN_LINK`. Neither claims independently verified category.
Other cases retain canonical `unknown`/`PENDING`, while source category lists and
provenance remain queryable. Category conflicts cannot be published as verified.
Applying separately reviewed category overrides is a later consumer step.

## Validation sample

The target is 48 AUTO_ACCEPTED links, with seed 20261108. Strata are the observed
Overture category group × distance band (≤10m, >10–25m, >25m). These are sampling
bands, not new matching thresholds. Allocate one sample to each nonempty stratum,
then allocate the remainder proportionally to remaining stratum populations using
largest remainder. Within each stratum, use seeded SHA256 ranking of composite
keys, independent of input order. Cap the sample at the eligible population.

Each CSV row includes source names/addresses/categories, observed Google URL,
distance, policy reasons/evidence and hashes, release IDs, stratum population and
sample size, inclusion probability and weight. Human label/reviewer/note are
blank. The release contains a pristine template; reviewers edit a separate copy
under `output/human-review/auto-accept-v1-validation/<release_id>/`. Replays never
overwrite an existing review copy, so labels cannot corrupt immutable mapping
release checksums or be lost.

No precision is estimated until humans label the sample. A later estimate must
weight stratum means by population share; a simple unweighted average can be
biased by minimum stratum allocation. UNSURE must be reported separately, not
counted as a true match. Single-observation strata cannot estimate their own
variance without more labels/assumptions (fully enumerated strata have no sampling
uncertainty). This sample says nothing about recall
or unresolved/source-only records. The new validation contract must be registered
with the human importer before consuming these labels; this task does not import
them or invent human decisions.

## dbt and publication

The minimal project is `pipelines/gold_staging`, using pinned dbt-core/dbt-duckdb
and the existing DuckDB runtime (`requirements/gold-staging.txt`). No cloud,
extensions, dbt packages or service is needed. Sources use manifest-pinned
Parquet paths. The local DuckDB file, dbt manifest, run results and console log
are retained per staging release. Intermediate models are ephemeral, avoiding
views referring to renamed temporary registry paths.

Adapter/source syntax follows the official [dbt-duckdb external-file contract](https://github.com/duckdb/dbt-duckdb/blob/main/README.md#reading-and-writing-external-files);
schema assertions use the official [dbt data-test contract](https://docs.getdbt.com/reference/resource-properties/data-tests).

dbt checks canonical uniqueness, relationships, category domain/status, no
unauthorized merge, accepted-links-only attachments and source coverage. Python
also checks stable ID derivation, exact source/link accounting and Google
assignment against the accepted mapping. Missing optional fields do not drop
records. Failed builds retain diagnostics in an inactive `.stage-*` directory;
the previous active release remains readable. Complete publication uses the
existing checksum/logical-hash verifier, OS lock and atomic pointer conventions.

Replay at the same root reuses the complete verified release. Independent builds
must produce identical logical Parquet data. dbt invocation timestamps/logs and
DuckDB physical files are runtime diagnostics, not byte-deterministic data claims.

## Commands

```powershell
Set-Location 'D:\Projects\integration-recomentdation-fnb-data-platform-20261028-pipeline'
$python = 'D:\Projects\integration-recomentdation\output\ci01-2ac84b90\venv\Scripts\python.exe'

& $python -B scripts/gold_staging.py activate `
  --shadow-root output/er-auto-accept-v1-shadow `
  --human-root output/human-decisions-v1 `
  --output-root output/effective-mapping-active-v1 `
  --validation-root output/human-review/auto-accept-v1-validation `
  --sample-size 48 --sample-seed 20261108

& $python -B scripts/gold_staging.py build `
  --active-root output/effective-mapping-active-v1 `
  --category-root output/category-quality-v1 `
  --output-root output/gold-staging-v1
```

Production publication still needs sample review, a defensible quality assessment,
resolution/accounting of remaining identity/category uncertainty, and an explicit
publication decision. These staging artifacts are not a recommendation system or
verified physical-store census.
