# M1.5 comparison features

This offline layer prepares name/address evidence for M2. It does not score,
match, merge, assign ER decisions, generate a review queue, or process reviews.
Published Google Silver M1, Overture Silver and raw observations remain unchanged.

## Implementation and versions

- `src/fnb_platform/comparison_features.py`: shared normalization, conservative
  Vietnamese address parsing, component evidence and versioned publication.
- `scripts/comparison_features.py`: CLI using existing project dependencies.
- `tests/test_comparison_features.py`: focused offline regression tests.
- Text: `vn-comparison-text-v1`.
- Address: `vn-address-components-v1`.
- Component evidence: `address-component-evidence-v1`.

Both sources use the same Unicode/case/accent/whitespace comparison normalizer.
It retains brand/branch tokens and punctuation. Original names, raw addresses
and existing Silver normalized values are retained in separate columns.

Address features include `house_identifier`, root `house_number`, `house_suffix`,
`house_path`, range endpoints, explicit `alley_path`, street, premise context,
unparsed parts, parse notes and `parse_status`:

- `PARSED`: identifiable house and street components without detected ambiguity.
- `PARTIAL`: some components identifiable; missing components stay NULL.
- `AMBIGUOUS`: reference-only locations or uncertain/multiple interpretations.
- `MISSING`: no source address.

`house_identifier` preserves `122`, `122a` and `122/14` as distinct values.
Explicit alley evidence is separate from a slash-delimited house path.
Lot/premise labels are not invented street names. A parsed address is not a
verified business identity.

## Outputs and coverage

Active release is selected by `output/comparison-m15/current.json`.
Release `e2b846a7ec106d718763fee2` contains:

| Artifact | Grain | Rows |
|---|---|---:|
| `comparison/overture_features.parquet` | Linked Overture ID | 474 |
| `comparison/google_features.parquet` | Google ID kind + identity value | 460 |
| `comparison/address_pair_evidence.parquet` | Original M1 Overture–Google relationship | 476 |

Only linked Overture IDs receive comparison features. Zero-candidate IDs remain
in the unchanged M1 inventory; this is not a rebuild of the 725-candidate catalog.
Google rows include 34 identities with missing metadata, preserved as missing.
Exact Arrow schemas and artifact checksums are recorded in `manifest.json`.
Counts and parser distributions are in `reports/normalization_summary.json`.

Source features retain name/address field provenance, source file hashes and
normalization versions. Pair evidence contains both actual raw/parsed addresses,
component relations, source references and rule versions.
Original M1 flags, source observation references and selection policies are
copied unchanged. Historical address flags on 41 links are explicitly
`AUDIT_ONLY_NOT_CURRENT_ER_CONCLUSIONS`. Current relations are derived from the
actual address pair independently of those flags. Literal differences are
component evidence, not NON_MATCH decisions; missing components are not conflicts.

## Verified real cases and checks

- Quán Ba Hồng: `158-160, Hoàng Diệu` versus `160 Hoàng Diệu` gives the same
  street and a range containing the literal number. Historical STREET_CONFLICT
  remains intact as historical evidence.
- Bột Chiên Hẻm 134: `Trước Cửa Nhà 134/41 Đoàn Văn Bơ` retains `134/41`,
  but marks the reference-only address AMBIGUOUS.
- Ốc Ngon Cư Xá Bến Vân Đồn: `Cư Xá Vĩnh Hội, Hẻm 183 Bến Vân Đồn`
  separates premise, explicit alley 183 and street; missing house stays missing.
- Pizza Hong Kong: `301B Đ. Tôn Đản` retains root 301, suffix b and identifier 301b.

Focused comparison tests plus existing M1 regression tests: **45 passed**.
Real-data validation confirmed all 476 relationship keys, original flags and
references are preserved; repeated publication is deterministic; published
inputs are unchanged; temporary and final artifact hashes agree.
Checks are recorded in `output/comparison-m15-validation/validation_checks.json`.

## Rerun (PowerShell, project root)

```powershell
& 'D:/Projects/integration-recomentdation/output/ci01-2ac84b90/venv/Scripts/python.exe' -B -m scripts.comparison_features `
  --overture-silver 'D:/Projects/integration-recomentdation-fnb-data-platform-20261028/output/overture-silver-m241/releases/2026-10-21.0/current_state.parquet' `
  --google-silver-root output/google-silver-m1 `
  --output-root output/comparison-m15
```

The publisher reuses M1 validation utilities, checks the pinned Overture input,
validates a staged release and atomically replaces the current pointer.
PARTIAL/AMBIGUOUS evidence remains available for M2 to handle explicitly.
