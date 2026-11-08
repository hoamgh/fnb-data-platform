# ER V3 experimental — audit all V2 REVIEW links

Rule: `ov-google-er-proposal-v3-experimental`. Same 476 M1.5 relationships and
pinned source observations; all 109 V2 REVIEW links were inspected. Published
Silver, M1.5, V1/V2, raw data, reviews and the existing 56-link human sample remain
unchanged. No human label was inferred from V2/V3 classifications.

## Root causes and changes

V2 REVIEW reasons included 57 links with insufficient name compatibility,
58 needing stronger address support, 34 outside the 100m positive band, 18 with
parsed address differences, seven name/branch warnings, four uncertain address
differences and one multiple-candidate link. Counts overlap across the 109 links.

1. Name representation: separate camel case and attached letters/numbers in an
   additional comparison view. Preserve raw names, M1.5 normalized names and the
   original symmetric full-string score. Record token, core and description-head
   evidence independently. Use a small generic descriptor/translation map
   (coffee/cà phê, milk tea/trà sữa, BBQ/nướng, hotpot/lẩu), not business aliases.
   Other-script tokens remain visible as unknown translations; shared Latin/Vietnamese
   identity tokens can provide support without pretending to translate Chinese.
   One-edit, nonnumeric token alignment requires length >=4, unambiguous one-to-one
   alignment, specific address agreement and <=25m when it is the added support.
   Reuse valid original V2 name evidence rather than letting a new token split
   discard it. Numeric core identifiers, explicit branch/district markers and
   title-versus-metadata address warnings remain positive-proposal blockers.

2. Address representation: derive comparison-only surface and component features.
   Exact normalized nonmissing surfaces provide agreement even if the old parser
   is AMBIGUOUS. Separately record an exact comma-delimited identifying prefix
   with an observed administrative tail; do not call it full-string equality.
   Preserve slash paths, suffixes, ranges and explicit lot/alley labels. Recover
   clearly written alphanumeric locators (K62, R32A, 183B6) and explicit street-first
   numbers. Normalize numbered street forms such as `41`/`Số 41` in the street field.
   Missing components stay UNKNOWN. Ambiguous alley/house interpretations are not
   promoted to certain conflicts. Different houses, suffixes, paths, lots or alleys
   never become equivalent from short distance. Multiple street expressions and
   reference-only addresses retain uncertainty. Building-only or street-only
   equality is support for the observed location, not sufficient branch specificity.

3. Classification: expose SUPPORT/CONFLICT/UNKNOWN for name, address, location and
   candidate evidence. Added name support requires specific address corroboration
   and <=100m; the full-string score is not a universal gate. Retain the existing
   conservative distant-location negative rules, shared-ID flags, multiple-candidate
   caution and title/branch contradiction checks. Far/same-address observations
   remain REVIEW. All results remain ER_PENDING; no merge or review reassignment.

The spatial operating bands are unchanged from V2. Generic lexical rules are
experimental and have not been calibrated against verified identity labels.

## Actual results

| Classification | V2 | V3 experimental |
|---|---:|---:|
| MATCH_CANDIDATE | 327 | 346 |
| NON_MATCH_CANDIDATE | 6 | 6 |
| REVIEW | 109 | 90 |
| INSUFFICIENT_DATA | 34 | 34 |
| Total | 476 | 476 |

| Transition | Links |
|---|---:|
| MATCH_CANDIDATE → MATCH_CANDIDATE | 325 |
| MATCH_CANDIDATE → REVIEW | 2 |
| REVIEW → MATCH_CANDIDATE | 21 |
| REVIEW → REVIEW | 88 |
| NON_MATCH_CANDIDATE → NON_MATCH_CANDIDATE | 6 |
| INSUFFICIENT_DATA → INSUFFICIENT_DATA | 34 |

All 21 new positive proposals have specific address agreement, <=100m distance,
no detected branch warning and no multiple-candidate conflict. Three share Google
IDs; their group flags and related links remain available. Evidence categories
overlap: 12 use descriptor/bilingual core support, 16 have a compatible description
head, one uses one-edit alignment, nine had at least one non-PARSED source address.
Twenty have exact identifying-prefix equality with administrative tails; **zero**
have identical full address surfaces across the two sources. The remaining link
is supported by explicit street-first number recovery. This distinction is stored,
not hidden by stripping arbitrary trailing text.

## Real changed cases and retained safeguards

| Pair / observed evidence | Distance m | Result |
|---|---:|---|
| Pizza 777 / PIZZA777 - QUẬN 4; both 237/42/5 Hoàng Diệu | 0.80 | REVIEW → MATCH_CANDIDATE; attached-number comparison |
| OcSu20k / Quán Ốc Su 20k; both 53 Tôn Đản | 3.62 | REVIEW → MATCH_CANDIDATE; camel/number tokenization |
| Passio Coffee / Cà Phê Passio Quận 4; both 124 Khánh Hội | 40.56 | REVIEW → MATCH_CANDIDATE; bilingual descriptor/core |
| Oc Qanh / Ốc Oanh; both 534 Vĩnh Khánh | 9.07 | REVIEW → MATCH_CANDIDATE; one-edit token evidence, prioritize human check |
| Kafin Coffee / KAFIN COFFFEE - XE CÀ PHÊ PHA MÁY - QUẬN 4; both 198 Hoàng Diệu | 0.20 | REVIEW → MATCH_CANDIDATE; descriptor typo and compatible title head |
| Abies BBQ Nướng & Lẩu / ABIESBBQ NƯỚNG & LẨU; Bến Vân Đồn/G42 Đ. Số 50 at both sources | 3.49 | REVIEW → MATCH_CANDIDATE; observed address equality despite AMBIGUOUS parser |
| Beno / Beno - mì Ý bò Mỹ & Steak; both 27 Hẻm 150 Bến Vân Đồn | 0.24 | REVIEW → MATCH_CANDIDATE; literal identity address, parser ambiguity preserved |
| MUI Coffee and Tea / MUI coffee & tee; both K62 Bến Vân Đồn | 8.65 | REVIEW → MATCH_CANDIDATE; descriptor spelling and alphanumeric locator |
| Luke’s Shop / Luke’s Shop; Đường Tôn Đản 293 versus 293 Đ. Tôn Đản | 37.35 | REVIEW → MATCH_CANDIDATE; explicit street-first number |
| Nhà hàng Tam Xuyên / Ẩm thực tam xuyên 温州卤味; both 375 Vĩnh Khánh | 10.21 | REVIEW → MATCH_CANDIDATE; shared core, Chinese text retained |
| Quán An _ món trưa + Chè bình dân+ nước mát mát; 500 8 Đoàn Văn Bơ versus 500 32 Đoàn Văn Bơ | 30.83 | MATCH_CANDIDATE → REVIEW; unresolved numeric/street expression, not certain inequality |
| Mì Cay Chinno / Mì cay Chinno; 317B Hẻm 1 Đường Tôn Đản versus 303 Tôn Đản | 6.19 | MATCH_CANDIDATE → REVIEW; uncertain components block reliance on closeness |
| Heekcaa Original; 172 versus 192 Khánh Hội | 8.95 approximately | Remains REVIEW; different houses are not equivalent |
| Keopi Coffee & Snacks; 79 versus 71 Đường Số 41 | 4.3 approximately | Remains REVIEW; different houses |
| Chicken Plus Vĩnh Hội / Chicken Plus Tôn Đản; same displayed 241 Tôn Đản | 1.14 | Remains REVIEW; unresolved name/branch suffix |
| Merlion title says 89 Vĩnh Khánh, both metadata addresses say 554 Vĩnh Khánh | 4.85 | Remains REVIEW; title-versus-address warning |
| Nhà Văn Kitchen; both 41 Hồ Xuân Hương | 2,387.70 | Remains REVIEW; coordinate/address inconsistency |

Address cells show identifying observed portions; original strings, typed Google
identity, source references, times and hashes remain in the result evidence.

## Remaining REVIEW and limits

Unique links per reason, overlapping: need specific address/name corroboration 85;
distance outside match band 34; name support unknown 32; current component conflict
23; uncertain address difference nine; name/branch warning six; multiple candidates
one; reference-only address one. These explain 90 links, not a sum of independent
populations. All 34 essential-metadata gaps and 251 zero-candidate Overture IDs
remain unchanged.

An exact address can contain several businesses. Descriptor normalization, similar
personal names, one-edit brands, reused premises and coordinate/source errors can
still produce wrong proposals. Administrative-prefix matching is explicitly
weaker provenance than full-string equality and depends on observed comma/tail
structure. No unknown-script translation or universal Vietnamese address parser
is claimed. The 23 changed links should be reviewed first; no precision/recall or
verified match count is claimed without independent human labels.

## Implementation, validation and rerun

Updated `src/fnb_platform/entity_resolution.py` only to route another policy,
version the evidence and compare against V2. Added
`src/fnb_platform/entity_resolution_v3.py` and `tests/test_entity_resolution_v3.py`.
V2 policy code and published artifacts were retained.

128 focused/regression tests passed (3.70s): spacing/numbers, descriptor translation,
one-edit comparison, bilingual script retention, exact ambiguous addresses,
alphanumeric locators, lots/alleys, NULL/reference uncertainty, different doors,
numeric branch IDs sharing a district, title/metadata contradictions, far locations,
determinism and atomic publication. Real validation on all 476 links confirmed:
all 109 V2 REVIEW audited; identical keys; complete flag evidence; CSV/JSON Unicode
round-trip; deterministic rerun and reversed inputs; V2 recomputation identical to
published V2 rows; all source/V1/V2 hashes unchanged. Validation took 16.376s.

Active output: `output/er-m2-v3-experimental/current.json`, immutable release
`6d61dc52aee95b95f68c6699` containing:

- `er_results.parquet`: 476 proposals with original and added evidence.
- `review_queue.csv`: 387 flag rows for 142 unique links, including shared-ID
  positive proposals retained for group checks.
- `er_summary.json`: transitions, all 109 baseline REVIEW audit entries, changed
  links/reasons, feature support counts, thresholds and unchanged source inventory.
- `manifest.json`: input/code hashes and artifact verification metadata.

Validation checks: `output/er-m2-v3-experimental-validation/validation_checks.json`.

```powershell
& 'D:/Projects/integration-recomentdation/output/ci01-2ac84b90/venv/Scripts/python.exe' -B -m scripts.entity_resolution `
  --comparison-root output/comparison-m15 `
  --output-root output/er-m2-v3-experimental `
  --version v3 `
  --baseline-root output/er-m2-v2
```

No crawling, ML, ER-B, Foody integration, source rebuild, merge, Gold, commit or push.
