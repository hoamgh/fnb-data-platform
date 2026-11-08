# Google crawler recovery — 2026-11-05

## Confirmed execution path and history

`scripts.google_m2_batch._execute_batch → scripts.google_live_workers.run/_crawl_shard
→ full_crawl_runner.run_full_crawl/_run_crawl → GoogleMapsCrawler.crawl
→ GoogleMapsNavigator.open_store → ReviewSurface.prepare_and_classify/open_reviews`.
Imports resolve to this pipeline worktree. The warm-up navigator does not select stores.
`CaptureNavigator` is not constructed by M2. Its experimental Foody/Overture workflow
remains untouched and is not an ingestion dependency.

| Component | Historical baseline | Pre-recovery local regression | Recovery |
|---|---|---|---|
| Candidate hooks | All three base navigator revisions call `_best_exact_candidate` and `_resolved_place`; CaptureNavigator (4fc7017) overrides them | Local `open_store` directly called `rank/evaluate`, bypassing overrides | Restore both virtual calls |
| Capture fallback | `CaptureNavigator._best_exact_candidate`: normalized name ranking and .05 top-two margin, no absolute floor | Not used by M2 | Adapt pattern in existing base hook, with relevance/support safeguards; do not import experimental runner |
| Address admission | 4bae1f5 made expected address, exact house/street and uniqueness mandatory | Follow-on parser discards street Khánh Hội or treats mặt tiền as street | Preserve strict evidence helper but remove exact-house admission requirement; fix parser, uncertain nested house suffix stays unverified |
| Reviews entry | d1b092e: CSS then XPath; eb70bc5: classification and already-selected guard; 4bae1f5: Vietnamese controls, scroll/stability check | Local text-only lookup dropped XPath and did not recover stale/intercepted controls | Reuse CSS/XPath, labels, stable native click; up to three local reacquisitions within bounded wait |
| Pane | `resolve_review_pane` and scoring retained since d1b092e | Scrollable search feed could be accepted as a fallback pane | Preserve pane selection and reject feed/containers without review cards that contain search feed |
| Sort | `sort_newest`, `_sort_newest_once`, confirmation retained from eb70bc5/4bae1f5 | Substring Sort matched Assorted photo labels | Keep historical menu and newest confirmation, retain word-boundary guard |
| Persistence | Existing SQLite outbox, Parquet, checkpoints, CDP timestamps | Unresolved matching treated as DLQ error | Preserve storage; normal unresolved finishes dispatch without DLQ |

Compared Git references:
- d1b092ebe05382cf1de4144a30616a4bb286974f
- eb70bc518ebd60ea9dd0758c5cd08438472e30ba
- 4bae1f5b458aae04c5d9da0b75d6d9266c8e9d8e

Historical `batch.py` and `poc.py` remain present, content-equivalent in both
worktrees and at 4bae1f5 (ignoring CRLF). No evidence of migration losing these files.
The original-worktree navigator matches 4bae1f5; local tuning is uncommitted.

## Selection and safeguards

Search discovers all results within five scrolls/two idle observations, waits
boundedly for lazy-loaded IDs, and selects zero or one. The original virtual hooks
are active again. Existing 45/40/15 scoring remains evidence; the recovered capture
fallback requires a clear name winner (margin .05, floor .45), street or nearby-point
support and no confirmed address contradiction. Missing house numbers do not imply
a mismatch. Distinctive-name/numeric-branch checks remain to prevent unrelated
same-address matches. A house-number suffix is compatibility evidence, not identity proof.

Card extraction prefers the card title, retains the raw accessible label and strips
visited-link annotations. Missing card address stays unknown. `Khánh Hội` after a
house number is a street, not automatically discarded as a ward. Bare geographic
context is never substituted for street address.

Selected source links stay `IDENTITY_UNVERIFIED`, including fallback and direct entity
results. Detail evidence and identifier contradictions can reject the selected page;
no second selection is forced. Metadata is saved before review opening/sort.

## Offline validation

278 focused tests passed. Coverage includes multiple/lazy candidates, direct details,
missing source address, active subclass hooks, CSS/XPath Vietnamese/English entries,
already-open cards/tab, stale/intercept recovery, feed rejection, newest selection
confirmation and failure, zero reviews, metadata retained after review failure,
no-DLQ unresolved outcomes, SQLite outbox crash recovery and preserved interrupted
checkpoints. No live correctness or coverage claim follows from these tests.

Saved replay: `output/google-recovery-pilot-v1-input/saved_43_selection_comparison.parquet`.
Eight of the 43 saved misses now yield a first-stage candidate, versus three that had
previously opened a selected entity. Five additional selections are not verified ER
matches and still require live entity/review-surface validation.

## Controlled manual pilot

Prepared four unchanged real source records from validated M242 inputs:
Passio Coffee, PatisStory - Chuyện bánh, Tiệm trà MOKA, Cofee Sân Vườn Cây Mít.
The fourth has a NULL source address. No live attempt has started.

Run `output/google-recovery-pilot-v1-input/run.ps1` manually after closing Chrome
using the four dedicated profiles. It processes only four records with the existing
workers. Search is independent of reviews; review limits remain 5 scrolls, 50 reviews,
two idle scrolls. Source-access/storage/reuse restrictions still apply.

Inspect `output/google-recovery-pilot-v1/summary.json`, `dispatch.sqlite3`, each worker's
`progress.jsonl`, per-job `progress.log`, metadata snapshots, checkpoints and Parquet.
Review logs include selected control text/aria/role, CSS/XPath lookup counts, sampled
visible controls, pane scoring, sort options and selection confirmation.

Normal no-result/unresolved outcomes and NO_REVIEWS are not DLQ entries. A permanent
closure with unavailable reviews is metadata completion, not full review coverage.
Auth/challenge/rate-limit, browser, persistence and server failures are not hidden.

Resume means retaining finished work and persisted observations. Unknown RUNNING
work after interruption is preserved for explicit inspection, not automatically
replayed. This change does not reset or replay the previous interrupted campaign.
