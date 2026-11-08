# M2 Overture ↔ Google ER V1 — experimental proposals

Rule version: `ov-google-er-proposal-v1`. Inputs: immutable M1.5 release
`e2b846a7ec106d718763fee2`, with its pinned M1/Overture inputs and raw hashes.
No independent verified ER decisions were present; the runner refuses to overwrite
such decisions if encountered. All 476 output links retain `er_status=ER_PENDING`.
`classification` contains proposals, never confirmed MATCH/NON_MATCH.

## Signals and thresholds

Name similarity is the arithmetic mean of both argument orders of Python
SequenceMatcher (`autojunk=False`) on the shared M1.5 comparison names.
No brand or branch tokens are removed. Distance uses valid WGS84 latitude/longitude
with haversine and mean Earth radius 6,371,008.8 m; this is a spherical distance,
not a survey-grade ellipsoidal measurement. Missing/invalid coordinates yield NULL.

Observed before selecting thresholds: 442 links have name and distance evidence;
34 lack Google metadata. Name bins: <0.4: 11, [0.4,0.6): 46, [0.6,0.8): 61,
[0.8,0.9): 31, >=0.9: 293. Distance bins: <=50m: 388, (50,100]: 14,
(100,200]: 12, (200,500]: 11, >500m: 17.

| Statistic | Name similarity | Distance (m) |
|---|---:|---:|
| P10 | 0.5424 | 0.3944 |
| P25 | 0.7692 | 0.6897 |
| Median | 1.0000 | 4.1069 |
| P75 | 1.0000 | 10.0361 |
| P90 | 1.0000 | 83.3117 |
| P95 | 1.0000 | 320.2785 |
| Maximum | 1.0000 | 16,758.4596 |

Rules, in order:

1. Missing name similarity or valid distance → INSUFFICIENT_DATA.
2. Shared Google identity, multiple candidates for an Overture ID, or identity-field
   metadata conflicts → REVIEW when primary evidence is available.
3. Name <0.60 AND distance >500m AND clear parsed address difference →
   NON_MATCH_CANDIDATE.
4. Name >=0.90 AND distance <=100m AND no clear address difference → MATCH_CANDIDATE.
5. Everything else → REVIEW.

100m surrounds the dense distance core (P90≈83m); 500m isolates its long tail.
The exact-name-heavy distribution supports a conservative 0.90 positive proposal
band. The <0.60 negative band includes low-similarity examples, but inspection
also found genuine-looking extended names at very low scores, so low similarity
alone never rejects a link. Thresholds live together in DEFAULTS and are embedded
in every evidence object/manifest. These are exploratory operating points, not
supervised calibration; there are no evaluation labels or precision/recall claims.

Street/house literal differences count as clear address evidence only when BOTH
addresses are PARSED. PARTIAL/AMBIGUOUS/MISSING never establish a clear conflict.
Different literals still require review; aliases or source errors remain possible.
Historical M1 STREET_CONFLICT/HOUSE_CONFLICT flags remain audit evidence only.

## Actual run and anomalies

| Classification | Links |
|---|---:|
| MATCH_CANDIDATE | 264 |
| NON_MATCH_CANDIDATE | 0 |
| REVIEW | 178 |
| INSUFFICIENT_DATA | 34 |
| Total | 476 |

Review queue: **279 flag rows for 212 links** (REVIEW, INSUFFICIENT_DATA and any
NON_MATCH_CANDIDATE). Match proposals remain fully explained in Parquet.
32 links share 16 Google IDs; four links involve two Overture IDs with multiple
Google candidates. All are retained, without automatic match or dedup.
The original **251 zero-candidate IDs** and their inventory fields are copied
unchanged into the summary; they do not become fabricated Google links.

Real anomalies, all REVIEW:

- `Quan Oc So 7` ↔ `Quán Ốc Số 7`: similarity 1.0 but 16.758 km apart.
- `Trà sữa Te Amo` ↔ `Trà sữa Teamo`: similarity 0.963, 4.063 km apart;
  `252 Đ. Khánh Hội` versus `1011 Phạm Thế Hiển...`, both parsed with differences.
- `Bánh Pizza` ↔ `Pizza Hut Nguyễn Tri Phương`: similarity 0.270, 3.674 km apart;
  Overture address is PARTIAL, so no certain address conflict is asserted.
- `133 Cafe` ↔ `Coffee & Tea 133`: similarity 0.25, 8.47m apart and same street/house;
  left for review rather than rejected from the low name score.
- `Tanabata HCM` ↔ `Tanabata Izakaya Bar Hồ Chí Minh`: similarity 0.545,
  3.505 km apart but same parsed street/house; possible coordinate error is unresolved.

Zero NON_MATCH_CANDIDATE is intentional under the conservative conjunction:
low-name/far pairs lack fully parsed address contradiction or have shared identity;
clear distant branch contradictions have higher name scores and stay REVIEW.
No threshold was adjusted to manufacture negative proposals.

## Explainability and files

Implementation: `src/fnb_platform/entity_resolution.py`, CLI
`scripts/entity_resolution.py`, tests `tests/test_entity_resolution.py`.
The contract `docs/M2_ER_EXPLAINABILITY_CONTRACT.md` is retained. Its final decision
vocabulary is represented by ER_PENDING; the four task-requested experimental
classes are in the separate classification column.

Active output: `output/er-m2-v1/current.json`, release
`c5b98603320a61a5bffca60f`:

- `er_results.parquet`: one row per original composite link, all 476 keys preserved.
- `review_queue.csv`: one row per current flag for links needing verification;
  UTF-8 BOM, names/addresses first, JSON/CSV round-trip verified.
- `er_summary.json`: counts, quantiles, anomalies, thresholds and zero-candidate inventory.
- `manifest.json`: input/code hashes, rule version and artifact hashes.

Every proposal and current flag has a concrete reason, actual values,
evidence_json, observation_ref, rule_version and recommended_action. Flag kind
distinguishes DQ from ER evidence. Evidence includes name/address field refs,
raw and normalized values, coordinate field provenance, fallback fields,
component parsing, related candidates, historical M1 flags and DQ details.
Raw historical flags are not relabeled as new current flags or conclusions.
Field-specific refs preserve differences in observation times/fallback origins.

58 focused/M1.5/M1 tests passed, including symmetry, missing data, address ambiguity,
shared identities, wrong branch, CSV escaping, input-order invariance, failed
publication visibility, input hash mismatch and refusal to overwrite decisions.
Real validation verified all 476 keys, 251 inventory IDs, complete flags, CSV
round-trip, identical reversed-input outputs and reruns, and unchanged inputs.
Validation/publication sequence took 8.451s locally. Checks:
`output/er-m2-v1-validation/validation_checks.json`.

## Rerun from project root (PowerShell)

```powershell
& 'D:/Projects/integration-recomentdation/output/ci01-2ac84b90/venv/Scripts/python.exe' -B -m scripts.entity_resolution `
  --comparison-root output/comparison-m15 `
  --output-root output/er-m2-v1
```

The versioned publisher validates pinned hashes before reading evidence and
again before publishing staged artifacts, then atomically replaces current.json.
It never writes source datasets, raw/checkpoints or reviews. No ER-B, Foody,
Gold, live crawling or automatic merging is performed.
