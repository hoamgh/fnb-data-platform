# M2 ER V2 — offline experimental comparison policy

Rule version: `ov-google-er-proposal-v2`. Same 476 M1.5 relationship keys as V1.
Every result remains ER_PENDING; classifications are proposals only.

## Implementation and comparison policy

- Updated `src/fnb_platform/entity_resolution.py`: reuse the existing build,
  evidence/flag contract, versioned publisher and CLI, with explicit V1/V2 selection.
- Added `src/fnb_platform/entity_resolution_v2.py`: comparison/classification policy.
- Added `tests/test_entity_resolution_v2.py`: regression and publication checks.
- V1 output is immutable and pinned as baseline; V2 refuses to publish over it.

Names retain original/shared normalized values and full symmetric similarity.
Additional evidence: token Dice, whole-token containment, contiguous containment,
distinctive shared tokens, numeric tokens and explicit branch/district warnings.
Common descriptor tokens only limit what qualifies as a distinctive anchor; they
are never deleted from names or branch evidence. No restaurant-specific rules.
Two-token containment or Dice>=0.85 can support extended/reordered names with
strong parsed address agreement. Single-token anchors such as Sumi require at
least four characters, contiguous containment, strong address agreement and <=25m.
Exact/high full-string evidence remains available; it is not a mandatory gate
for every positive proposal. Numeric-name differences, explicit branch/district
differences and a house/street address in a name disagreeing with its own metadata
block positive proposals. Such warnings alone never establish NON_MATCH.

Strong address agreement means both addresses PARSED, same street and same house,
same range, or a range containing the literal house. Merely overlapping ranges,
same street without house, or shared premises are not strong branch evidence.
PARTIAL/AMBIGUOUS components are retained; tentative differences block automatic
positive proposals without being treated as certain address conflicts.

Rules in precedence order:

1. Missing name/distance evidence → INSUFFICIENT_DATA.
2. Identity metadata conflicts → REVIEW.
3. Fully parsed street AND house differences with distance >200m; or fully parsed
   house difference with distance >500m; or reliable observed street differences
   corroborated by distance >2000m → NON_MATCH_CANDIDATE, irrespective of name score.
   The last route permits PARSED/PARTIAL with only HOUSE_NOT_IDENTIFIED notes;
   it never uses AMBIGUOUS/reference-only addresses. PARTIAL alone is insufficient.
4. Name support, distance <=100m, no branch/address warnings, no multiple candidates,
   and strong address support when required → MATCH_CANDIDATE.
5. Otherwise REVIEW, with explicit evidence gap/contradiction reason codes.

Shared Google ID is a DQ flag/group audit, not a classification gate. Each link
is evaluated independently; a positive shared-ID proposal requires strong address
agreement. Multiple Google candidates for one Overture ID still require caution
for positive proposals, but do not prevent an independently supported negative.
No Overture records or reviews are merged/reassigned.

## Operating points and evidence limits

V1 measured 442 usable distances: median 4.11m, P90 83.31m, P95 320.28m;
388 <=50m, 14 in (50,100], 12 in (100,200], 11 in (200,500], 17 >500m.
The 100m core is retained. 200m is twice that positive band and requires BOTH
house/street differences (e.g. ABC Bakery at 320m); house-only negatives retain
the more conservative 500m tail boundary. PARTIAL street-only corroboration uses
2000m, substantially beyond the core, covering the observed 16.758km wrong-location
case without rejecting far/same-address records such as Nhà Văn Kitchen.
Dice 0.85 requires high token overlap (e.g. six shared tokens across sets of
sizes six/seven); exact containment handles long descriptions independently.
The single-token 25m band is tighter than the general 100m band.

These are explainable experimental operating points supported by inspection and
boundary tests, not statistically calibrated thresholds. No ground truth exists;
precision/recall are not measured. Future labels may justify different values.

## Actual results

| Class | V1 | V2 |
|---|---:|---:|
| MATCH_CANDIDATE | 264 | 327 |
| NON_MATCH_CANDIDATE | 0 | 6 |
| REVIEW | 178 | 109 |
| INSUFFICIENT_DATA | 34 | 34 |
| Total | 476 | 476 |

| Transition | Links |
|---|---:|
| MATCH_CANDIDATE → MATCH_CANDIDATE | 259 |
| MATCH_CANDIDATE → REVIEW | 5 |
| REVIEW → MATCH_CANDIDATE | 68 |
| REVIEW → NON_MATCH_CANDIDATE | 6 |
| REVIEW → REVIEW | 104 |
| INSUFFICIENT_DATA → INSUFFICIENT_DATA | 34 |

79 links changed. All 68 newly positive proposals have strong parsed address
agreement, <=100m distance, no detected branch warnings and no multiple-candidate
conflict. Nine have shared Google IDs; group evidence/flags remain present.
This is automated evidence consistency inspection, not verified correctness.

Remaining REVIEW reasons, **unique links per reason; overlapping counts**:

| Reason | Links |
|---|---:|
| Need stronger parsed address support | 58 |
| Name insufficiently distinctive/compatible | 57 |
| Distance outside positive band | 34 |
| Fully parsed address difference | 18 |
| Name/branch warning | 7 |
| Uncertain address component difference | 4 |
| Multiple Google candidates | 1 |

Review queue contains 390 flag rows for 158 unique links: 109 REVIEW,
34 INSUFFICIENT_DATA, six negative proposals and nine shared-ID positive proposals
retained for group consistency checks. More explicit flags mean queue rows need
not decline in proportion to REVIEW links. All historical flags stay audit-only.

## Eighteen real examples

M = MATCH_CANDIDATE, N = NON_MATCH_CANDIDATE, R = REVIEW.
Address cells show actual identifying address portions; full unchanged display
values, Google identity and field provenance are in Parquet/CSV.

| Overture name → Google name | Overture → Google address | Distance m | V2 / evidence |
|---|---|---:|---|
| Ốc Tú → ỐC TÚ - Đoàn Văn Bơ | 232/75 Đoàn Văn Bơ → 232/75 Đoàn Văn Bơ | 0.67 | M; containment, exact parsed address |
| Chấn Phong Mì Gia → CHẤN PHONG MÌ GIA( Mì Xá Xíu- Hoành Thánh- Sủi Cảo) | 156 Tôn Đản → 156 Tôn Đản | 0.40 | M; full score only 0.50 |
| GoGi House → GoGi House Đường số 41 Quận 4 | 69 Đ. Số 41 → 69 Đ. Số 41 | 4.39 | M; branch address agrees |
| Trà Sữa Mr Tea → TRÀ SỮA MR TEA \| TRÀ SỮA NHÀ LÀM \| TRÀ SỮA NGON QUẬN 4 | 34 Nguyễn Hữu Hào → 34 Nguyễn Hữu Hào | 7.18 | M; containment |
| ABC Bakery → ABC BAKERY QUẬN 4 | 187 Khánh Hội → 396 Hoàng Diệu | 320.28 | N; parsed house/street differences + distance |
| ABC Bakery Quận 4 → ABC BAKERY QUẬN 4 | 396 Hoàng Diệu → 396 Hoàng Diệu | 2.28 | M; shared ID retained as flag |
| Quan Oc So 7 → Quán Ốc Số 7 | 232 Vĩnh Khánh → Đ. Vĩnh Lộc, Tân Vĩnh Lộc | 16,758.46 | N; reliable different street + very large distance |
| Trà sữa Te Amo → Trà sữa Teamo | 252 Đ. Khánh Hội → 1011 Phạm Thế Hiển | 4,063.03 | N; near-identical name does not override location |
| Highlands Coffee → Highlands Coffee Ben Van Don HCM | 182 Bến Vân Đồn → 182 Bến Vân Đồn | 1.20 | M; strong address, not brand alone |
| Bánh Canh Cua Ngọc Trinh → Bánh Canh Cua Ngọc Trinh Chi Nhánh 2 | 4 Tân Vĩnh → 4 Tân Vĩnh | 4.16 | M; added branch label retained |
| Merlion cháo ech Singapore → Cháo Ếch Singapore Merlion 89 Vĩnh Khánh | 554 Đ. Vĩnh Khánh → 554 Đ. Vĩnh Khánh | 4.85 | R; Google title says 89 but metadata says 554 |
| Cà phê Hạnh Nhi → Cà phê Hạnh | 361 Đ. Vĩnh Khánh → 361 Đ. Vĩnh Khánh | 0.46 | M; risk: shortened personal/brand name, prioritize human labels |
| Hồng Trà Ngô Gia → Hồng Trà Ngô Gia H103 | 141 Tôn Đản → 141 Tôn Đản | 0.95 | M; H103 preserved |
| Sumi → Sumi - Ăn Vặt Cá Viên | 148/12/25 Tôn Đản → 148/12/25 Tôn Đản | 3.58 | M; single-token anchor under tight distance/address rule |
| 133 Cafe → Coffee & Tea 133 | 133 Tôn Đản → 133 Tôn Đản | 8.47 | R; shared numeric token alone insufficient |
| Chicken Plus Vĩnh Hội → Chicken Plus Tôn Đản | 241 Tôn Đản → 241 Tôn Đản | 1.14 | R; different suffixes, insufficient name compatibility |
| Nhà Văn Kitchen → NHÀ VĂN KITCHEN RESTAURANT | 41 Hồ Xuân Hương → 41 Hồ Xuân Hương | 2,387.70 | R; coordinate/address disagreement, not automatic negative |
| Bột Chiên Hẻm 134 → Bột Chiên Hải Vân | Trước Cửa Nhà 134/41 Đoàn Văn Bơ → 134/121 Đoàn Văn Bơ | 36.79 | R; AMBIGUOUS reference address and shared candidate |

## Validation, artifacts and rerun

91 tests passed across V2, V1, M1.5 and M1. Tests cover supplied real name cases,
same-brand wrong-house/branch counterexamples, shared-ID independent decisions,
missing metadata, ambiguous addresses, 25/200/500/2000m boundaries, CSV Unicode/JSON,
historical-detail ordering, atomic publication, reruns and protection of V1.
Actual data validation confirmed exact 476 keys, reversed-input invariance,
deterministic rerun, complete flag evidence, CSV round-trip and unchanged input/V1
hashes. 251 zero-candidate Overture IDs remain unchanged in summary inventory.

Active V2: `output/er-m2-v2/current.json`, release `95def9bca6f741e63cd77134`.
Artifacts: `er_results.parquet`, `review_queue.csv`, `er_summary.json`, `manifest.json`.
Summary includes all transitions, changed links, new-match audit, group outcomes,
reason counts, thresholds and input/code hashes via the manifest.
Checks: `output/er-m2-v2-validation/validation_checks.json`.
Local validation plus repeated publication completed in 13.396 seconds.

```powershell
& 'D:/Projects/integration-recomentdation/output/ci01-2ac84b90/venv/Scripts/python.exe' -B -m scripts.entity_resolution `
  --comparison-root output/comparison-m15 `
  --output-root output/er-m2-v2 `
  --version v2 `
  --baseline-root output/er-m2-v1
```

## Remaining limitations and labeling

Containment cannot prove that a shortened brand, co-located businesses, a renamed
store or reused premises represent the same entity. Token matching has no alias
dictionary or semantic understanding. Numeric titles may be slogans: e.g. Gà Ta
Ngon Số 1 Vĩnh Hội receives a conservative warning against house 120; this is a
review prompt, not a confirmed conflict. Parser/coordinate/source errors and stale
observations remain unresolved. F&B eligibility is independent: VWS and Hút Chân
Không candidates can have consistent identity evidence without being valid F&B.
V2 does not change eligibility or source classification.

Recommend an initial **200-link** labeled audit: all 79 changed links, 40 unchanged
positive proposals, 60 unchanged REVIEW links, 21 missing-metadata links. Within
those strata prioritize all shared/multiple-candidate cases, chains, short anchors,
numeric/branch warnings, ranges and distance boundaries. Record MATCH/NON_MATCH/
UNRESOLVED with observation refs; insufficient evidence must remain unresolved.
For this small corpus, labeling all 476 is preferable before operational acceptance.
No precision/recall or accuracy claim is made from the automated audit.
