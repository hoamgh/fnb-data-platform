# Candidate selection recovery (collection only)

Latest refinement: `source-neutral-core-name-ward-v5` compares the distinctive
name core after removing generic Vietnamese/English business descriptors
(including Milk Tea / Trà / Coffee / Bakery / Shop). It tolerates word order,
accents and minor spelling differences. The existing configurable 0.80 local
name threshold is applied to that name evidence; matching input/Google ward
permits collection despite conflicting Overture street or coordinate values.
Raw names and raw similarity remain available. Wrong distinctive names and
ambiguous equally plausible branches are not forced. This is a collection
decision, never a verified identity or merge. Offline replay of the saved 286
misses selects 18 provisional candidates, including Phúc Kiến Milk Tea → Trà
Phúc Kiến. No live crawl was launched for this refinement; evidence is in
`output/google-core-name-ward-v5-offline/comparison.json`.

Current policy update: `source-neutral-same-ward-v4` accepts a sufficiently
relevant distinctive business name (configurable local name threshold, 0.80)
when the Google address explicitly supplies the same input ward. Source street,
house-number and coordinate disagreements remain scored/audited evidence, but
do not veto this collection route because Overture location data can be wrong.
Ward comes from structured input or explicitly labeled source context; matching
Google comma-separated locality fields can supply the ward without its prefix.
No ward is hardcoded and proximity is never presented as confirmed ward membership.
An unknown ward remains unknown. Multiple equally plausible branches remain
unresolved under the existing winner-margin rule. Identity stays unverified.
This update supersedes the older address-conflict veto described below for
candidates with observed same-ward/name evidence. Review components are unchanged.
Offline replay of 286 saved misses provisionally selects 10 (7 via same-ward
evidence); no new live crawl was launched. Evidence:
`output/google-same-ward-v4-offline/comparison.json`.

Active path: `scripts.google_m2_batch` imports `scripts.google_live_workers`,
which calls `full_crawl_runner`, `GoogleMapsCrawler`, then the base
`GoogleMapsNavigator.open_store()`. M2 does not instantiate `CaptureNavigator`.

Historical comparison:

- `eb70bc5`: base navigator preferred exact names and address matches but could
  fall back when the address was absent. No bounded search-panel scrolling.
- `4bae1f5`: `_best_exact_candidate()` required an expected address and a unique
  exact house/street-compatible result. This is too strict for collection.
- `experiments/google_resolution/batch.py:CaptureNavigator` retained a ranked
  name fallback, but without an absolute relevance floor. Reusing that behavior
  without corroboration could select a lone unrelated result.
- Before this change, local M2 already called the historical virtual hooks and
  scrolled the feed. Its extra token filter, full-string name score, and 1 km
  veto still lost plausible candidates. Its fallback could accept generic
  shared words such as “Nướng”. These are local policy regressions; the files
  were not missing from the worktree migration.

This change retains the existing ranking, hooks and bounded feed discovery.
Name evidence now retains distinctive tokens when location/cuisine suffixes
are added, tolerates spacing and modest spelling variations, and excludes
generic food words as sole corroboration. Original names/addresses remain intact.
Weights remain 45/40/15 and the existing combined threshold remains 0.65.
Street/address and name drive eligibility; matching house numbers strengthen
it without making available house numbers mandatory. Known conflicting house
numbers, numbered streets and clearly incompatible streets remain negative
evidence. Strong distinctive names can be collected when addresses are unknown;
that is explicitly unverified and can produce wrong-branch candidates.

The former blanket 1 km veto is removed. A configurable 20 km clear-location
contradiction guard remains; lesser distances affect ranking and are not proof
of identity. It is a collection heuristic, not a calibrated ER boundary.

Structured card headings are preferred to full card text. Visible numbered
address segments are recovered when `data-address` is missing; rating, review
count and hours lines are excluded. Unavailable addresses stay NULL. Search
retains the existing maximum five scrolls and two idle-scroll stopping rule.
All discovered unique candidates are ranked. The historical winner-margin
check remains; no clear winner means zero selections. If a plausible candidate's
detail page is unsuitable, the existing configurable inspection budget (three
opens by default) allows trying the next candidate. At most one is returned.

Diagnostics retain candidate rankings, scores, unknown signals and rejection
reasons, plus detail attempts and `search_elapsed_seconds` (search plus detail
selection, excluding review collection). Final identity remains
`IDENTITY_UNVERIFIED`. Closed status never excludes a candidate.

Offline evidence is in `output/google-candidate-recall-v2-evaluation/report.md`
and its JSON files. The first 20 historical misses have 103 saved candidates:
zero historical final selections versus six provisional selections on replay.
These are not six successful new crawls. Saved card addresses are incomplete;
detail checks can still reject selections. In a separate, newer 20-miss sample,
all 99 candidates still yield zero selections because of contradictory or
ambiguous evidence. Live recall improvement has not yet been demonstrated.

Potential false positives include the distant name-only matches for
“Cơ Sở Sản Xuất Bán Mì Vân Đồn” and “Cà Phê Hương Việt”. The suspicious
“Quán Nhóc Nướng” → “Quán Cá Biển Nướng” pair is rejected. Baseline pure search
timings are unavailable, and offline scoring time is not live search time.

A fresh, manual 20-POI pilot with the original four dedicated profiles is ready:
`output/google-candidate-recall-v2-pilot-input/run.ps1`. It uses existing
SQLite/Parquet checkpointing and review limits (five scrolls, 50 reviews, two
idle scrolls). No review tab, sort, extraction or persistence implementation was
changed. The pilot has not been launched.
