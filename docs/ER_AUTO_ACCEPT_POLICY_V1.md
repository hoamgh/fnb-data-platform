# ER Auto-Accept Policy V1 — shadow only

The new hybrid layer evaluates immutable ER V3, Category Quality V1, Google
Silver, audited AI suggestions and the existing human ledger. It does not edit
any of them. The CLI requires `--shadow`; there is no official activation switch.
The human importer and its active mapping/history remain unchanged.

## Policy inputs and evidence

`config/er_auto_accept_policy_v1.json` versions the entry and safety conditions.
The manifest pins its checksum, code checksums, source artifact/release hashes,
the audited AI CSV and audit summary hashes, and the current human ledger pointer
when present. Replay uses the same pinned inputs. A changed human decision creates
a new shadow result; the shadow evaluator never appends to the human ledger.

AI loading requires the exact audited file checksum, all original review IDs and
composite keys, release versions and immutable exported fields to match current
trusted evidence. The file must contain machine suggestions, not human labels.
`ai_confidence`, rationale and external URLs are preserved as machine context;
confidence is not a measured probability or an acceptance signal. External URLs
are not fetched. Later human annotations belong in the separate human importer.

## Candidate entry and positive support

An ER V3 `MATCH_CANDIDATE` or an audited AI `MATCH` may enter the same auto policy.
Neither is sufficient for acceptance. Require all of:

1. Existing V3 name, address and location signals are `SUPPORT`.
2. V3 `strong_agreement` **and** `specific_agreement` are true for the address.
3. Valid observed coordinates and distance, nonempty source names and available
   essential Google metadata. Incomplete optional metadata is allowed.
4. No unresolved safety blockers described below.

The policy reuses the V3 geographic band and name/address comparison features.
It introduces no new numeric similarity/distance threshold and no new parser.
Equal full addresses can support comparison while still being too broad or
ambiguous for automatic identity acceptance. Missing/uncertain/reference-only
address evidence is a support gap, not a proven mismatch. Missing category is
optional and does not by itself block identity acceptance.

Requiring specific address corroboration was checked against real shadow
examples: Honey Moon has a building/compound address; Le Blanc has the ambiguous
`Tân Vĩnh/83 Khánh Hội` structure. Both strings agree across sources, but the
published V3 evidence lacks specific agreement. AI MATCH does not override that
gap. The same condition applies to every record, without business-name rules.

## Safety blockers and group checks

- Any shared Google identity is held from **auto** acceptance in V1. Do not infer
  ER-B, choose a winner by score or merge the Overture IDs.
- Multiple Google candidates for one Overture ID are unresolved until every
  competing link has a current human NON_MATCH. Machine/ER NON_MATCH is not a
  group resolution. A V3 candidate-conflict signal caused only by this group may
  be considered resolved by those current decisions; actual metadata conflict
  remains blocking. Human acceptance still uses the existing importer checks.
- F&B/non-F&B category conflict or internally contradictory source categories
  blocks auto acceptance. Subcategory differences alone do not confirm/reject ER.
- Current parsed address conflict, effective name/branch warning or unexplained
  candidate-identity conflict blocks auto acceptance. Historical M1 STREET/HOUSE
  flags are not used as current conclusions.
- Unresolved Google metadata conflicts, including retained conflicting values,
  block auto acceptance. This is a hold, not a claim that the link is false.
- An ER NON_MATCH proposal is not overturned automatically by AI MATCH.

Accepted shadow links must remain one-to-one across Overture IDs and typed Google
identities. A violation hard-fails publication. No `store_id` is generated.

## Human and machine precedence

| Evidence/decision | Effective shadow result |
|---|---|
| Current human MATCH, existing group checks pass | HUMAN_ACCEPTED |
| Current human MATCH, unresolved group conflict | CONFLICT |
| Current human NON_MATCH | REJECTED |
| Current human UNSURE | PENDING; auto cannot override |
| Stale human evidence | PENDING; requires refreshed review |
| No current human decision + AI UNSURE | PENDING |
| AI NON_MATCH | Machine suggestion only; PENDING/CONFLICT, never REJECTED |
| Eligible entry + positive support + no blockers | AUTO_ACCEPTED, decision_source=AUTO_POLICY |
| Blockers / insufficient support | CONFLICT / PENDING |

Valid human identity MATCH may override an auto hold such as wrong source
category, after the existing group checks. This does not verify F&B eligibility
or change category decisions. AI MATCH blocked by a conservative policy is not
automatically a false-positive AI label. AI NON_MATCH opposing positive policy
evidence is reported separately; it is never silently accepted or human-rejected.

## Output contract and publication

`effective_identity_mapping.parquet`: all current ER composite links, keyed by
`(overture_id, google_id_kind, google_identity_value)`.

- Original names/addresses, distance, similarity, unchanged ER classification and
  `ER_PENDING`, category relation and metadata status.
- `effective_status`: AUTO_ACCEPTED / HUMAN_ACCEPTED / REJECTED / PENDING / CONFLICT.
- `decision_source`, `policy_version`, reason/reason_codes, policy conflict codes,
  missing-support codes, group flags, candidate-entry and positive-evidence flags.
- Current human label/decision reference/reviewer/note/evidence applicability;
  AI label/confidence/provisional status and relation to policy evidence.
- Canonical `evidence_hash`, actual values in `evidence_json`, source provenance,
  ER/category/Google release IDs, rule version and `shadow_only=true`.

The policy evidence hash covers actual source comparisons, category values,
metadata and group decisions, full-source evidence hashes, AI suggestion, policy
configuration and provenance. It is a policy decision hash, not a replacement
for the human review evidence hash (`human_evidence_hash` remains separate).

`summary.json` separates the 346 V3 candidates, original 90 REVIEW links, AI
suggestions/statuses, human overrides and auto-safety leak checks. Primary hold
reasons count unique links; all-reason counts can overlap. `manifest.json` contains
artifact byte/logical checksums and `SHADOW_ONLY_NOT_OFFICIAL_MAPPING`.

Publication uses the existing versioned Parquet/atomic pointer convention and OS
lock. `current.json` is replaced **only in the separate shadow root**. This is
not official effective mapping activation. Retry/replay is deterministic and
idempotent; failure leaves the previous shadow pointer intact. A concurrent human
ledger change is detected and requires replay. The unlinked 251 baseline IDs are
accounted for in the summary and remain in the existing source/human inventory.

No precision/recall claim is made without independent ground-truth labels. The
initial policy is deliberately conservative, including cases that may be true
matches but whose saved address evidence is not specific enough.

## PowerShell

```powershell
Set-Location 'D:\Projects\integration-recomentdation-fnb-data-platform-20261028-pipeline'
& 'D:\Projects\integration-recomentdation\output\ci01-2ac84b90\venv\Scripts\python.exe' -B scripts/er_auto_accept_shadow.py `
  --er-root output/er-m2-v3-experimental `
  --category-root output/category-quality-v1 `
  --human-root output/human-decisions-v1 `
  --ai-csv 'C:\Users\MSI\Downloads\identity_review_AI_triage.csv' `
  --ai-audit output/human-review/ai-triage-audit-v1/1b014cf1383da737496864a8/audit_summary.json `
  --output-root output/er-auto-accept-v1-shadow `
  --shadow
```

Official activation is a separate action after reviewing the shadow results.
