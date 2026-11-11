# Human Decision Import V1

This is an offline decision ledger and effective source-link view. It does not
change review CSVs, ER V3, Silver, Category Quality V1 or Canonical Gold. It does
not allocate `store_id`, merge Overture IDs, crawl, or use AI labels as truth.

## Inputs and validation

`scripts/import_human_decisions.py` takes the existing identity/category CSVs,
active ER V3 and Category Quality V1 roots, and a separate decision output root.
It regenerates trusted review evidence in memory using the existing exporter;
no additional review CSV is exported.

CSV files may contain all rows or a subset. Required headers remain the same.
For every submitted row, including blank-label rows, the importer checks the
review ID, grain, composite source key, releases, evidence hashes and **every
non-annotation column** against the regenerated trusted row. Invalid, stale,
duplicate or modified evidence rejects the entire import without activating a
release. Numeric formatting/JSON whitespace changes in immutable cells are also
rejected: only human annotation columns should be edited.

AI/audit columns are allowed as ignored context (`ai_*` and the known audit
display columns). They never supply a label, reviewer, confidence threshold or
automatic acceptance. The existing AI triage file can be checked with dry-run,
but 35 AI MATCH suggestions do not create human MATCH decisions.

Identity annotations require `human_label` (`MATCH`, `NON_MATCH`, `UNSURE`),
`reviewer`, and `reviewer_note`. Category annotations require
`reviewed_fnb_status` (`FNB`, `NON_FNB`, `UNSURE`), `reviewer`, and `reviewer_note`;
`reviewed_category` is optional and uses the Category Quality V1 groups. Unknown
subcategory can remain blank/`unknown`. Contradictory status/category pairs fail
validation (for example FNB + non_fnb).

Whitespace-only human fields count as empty. Fully blank rows produce no event.
Incomplete annotations produce a warning and no event. An existing decision is
retained; incomplete rows do not silently revoke it. Invalid enums fail rather
than being guessed. Reviewer/note are explicit attestations; this local CLI does
not authenticate or prove who physically entered them.

## History and revisions

`decision_history.parquet` has one row per decision revision, keyed by
`decision_id`. A chain is identified by `(decision_type, review_id)`:

- `decision_type`: identity or category; `review_grain` and the three source keys.
- `revision`, `parent_decision_id`, `decision_id` (content-derived).
- Identity label or category status/subcategory; reviewer and reviewer note.
- Evidence hash, ER/category/Google/Overture releases and review/rule versions.
- `input_csv_path`, `input_csv_sha256`, `imported_at_utc`, decision rule version.

Each published release retains the complete history. Prior releases and old
events remain unchanged. Reapplying the same normalized annotation/evidence
against the latest state is a no-op. Changing label, reviewer, note or reviewed
evidence appends a revision, including MATCH → NON_MATCH → MATCH. The latest
explicit complete submission wins for that row; reapplying an older filled CSV
after a later revision is an explicit new revision, not a rollback of history.
Blank rows never erase a decision. Use an explicit UNSURE revision to return a
previously accepted/rejected link to PENDING.

`imported_at_utc` is the actual import time, not an invented human review time.
CSV byte hashes record what was submitted; the CSV itself may later be edited
by its owner. Old human labels/notes remain in the immutable ledger even then.

## Effective identity mapping

`effective_identity_mapping.parquet` retains **every ER composite link**,
including links outside the human review subset. Grain/key:
`(overture_id, google_id_kind, google_identity_value)`.

Columns include original names/addresses, ER classification and `ER_PENDING`,
`proposal_status=PROVISIONAL`, `effective_status`, current human label, reviewer,
note, decision ID/revision, review ID, evidence hash/applicability, source release
IDs, observation reference, group flags, conflict codes/evidence, reason and
import rule version.

| Current complete human decision | Effective status |
|---|---|
| MATCH, with resolved competing-link groups | ACCEPTED |
| NON_MATCH | REJECTED |
| UNSURE | PENDING |
| No human decision, incomplete annotation or stale decision | PENDING |

Group checks use **all ER links**, not just submitted rows. A MATCH is blocked
when the same Google identity has other Overture links, or its Overture ID has
other Google candidates, unless all competing links have current human
NON_MATCH decisions. Multiple human MATCHes in either group stay PENDING with
specific group conflict evidence. Provisional ER/AI NON_MATCH does not resolve a
group. This conservative V1 does not invent an ER-B decision or merge shared IDs.

Category conflict does not by itself reject a human identity MATCH: the same
business can have wrong source taxonomy. Source evidence remains unchanged.
An ACCEPTED source link is not yet a canonical store or a Gold publication.

## Independent category decisions

`effective_category_decisions.parquet`: one row per current category review row
(linked pair or Overture source-only), keyed by `review_id`. Includes grain/key,
human F&B status/subcategory, reviewer/note, latest decision ID/revision,
evidence hash, `decision_status`, `applicability_status`, original source category
evidence, release IDs and rule version.

- Complete category decision is recorded independently of identity.
- Unaccepted link: `WAITING_ACCEPTED_IDENTITY`.
- Rejected link: `IDENTITY_REJECTED_DO_NOT_TRANSFER`.
- Accepted link: `IDENTITY_ACCEPTED_CATEGORY_RECORDED`, for a later consumer.
- Source-only row: `SOURCE_ONLY_DECISION_NO_CANONICAL_MAPPING`.
- Missing/stale/UNSURE category: pending; no canonical category is selected.

These are decision/application states, not modifications to the source taxonomy
or the already-published Category Quality V1 release.

`unlinked_overture_inventory.parquet` retains baseline Overture candidates with
no Google link: ID, original name/address and `NO_GOOGLE_CANDIDATE`. On current
real inputs this preserves the 251 IDs independently of the 476 link rows.

## Publication and recovery

`--dry-run` validates and reports proposed changes in memory with **zero writes**.
It also reports the resulting effective counts, so an existing ledger may have
accepted mappings even if the current submitted file has no new labels.

`--apply` writes complete Parquet artifacts, summary and checksummed manifest
under `releases/<release_id>/`, then atomically replaces `current.json`. It
reuses existing canonical hashing, release verification and pointer utilities.
A concurrent writer is rejected by an OS file lock. OS locks are automatically
released after process death; an existing `.publish.lock` file is harmless and
must not be manually removed while another writer runs.

Crash before pointer replacement: previous active release/history remains
authoritative. A complete orphan release or inactive staging directory is never
read as active. Retry starts from the active history and does not duplicate
events. Crash after pointer replacement: the complete release is authoritative;
retry is idempotent. Source CSVs/pinned artifacts/code are rechecked before
activation. No database/service or separate orchestration framework is introduced.

## PowerShell

```powershell
Set-Location 'D:\Projects\integration-recomentdation-fnb-data-platform-20261028-pipeline'
$python = 'D:\Projects\integration-recomentdation\output\ci01-2ac84b90\venv\Scripts\python.exe'
$reviews = 'output/human-review/er-category-v1/a8accd4a40d4c87b8c6df71a'
$importArgs = @(
  '-B', 'scripts/import_human_decisions.py',
  '--er-root', 'output/er-m2-v3-experimental',
  '--category-root', 'output/category-quality-v1',
  '--identity-csv', "$reviews/identity_review.csv",
  '--category-csv', "$reviews/category_review.csv",
  '--output-root', 'output/human-decisions-v1'
)
& $python @importArgs --dry-run
# After inspecting the result, explicitly apply the completed human annotations:
& $python @importArgs --apply
```

The production CSVs are dry-run only during implementation. Human decisions in
tests are synthetic fixtures, never fabricated labels for real businesses.
