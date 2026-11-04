# Google crawler v1 — focused reliability review (2026-11-04)

Read-only evidence: `output/google-four-e2e-20261104-185249/` (129 persisted reviews, all timestamps VERIFIED, one unresolved Highlands branch, one unintended UNKNOWN retry, weekly hours incomplete). Existing crawl artifacts were not changed. No browser, network request or live crawl was launched for this review.

## Scope and test legend

Only the Google worker entry point and its direct browser/navigator/review/policy/metadata/persistence paths were inspected. This is a risk-based scenario inventory, not exhaustive Google Maps or line/branch coverage.

- **D**: `tests/test_google_driver_startup_diagnostics.py`
- **C**: `tests/test_google_live_compat.py`
- **B**: `tests/test_google_branch_identity.py`
- **I**: `tests/test_google_place_resolution.py`
- **M**: `tests/test_google_place_details.py`
- **U**: `tests/test_google_vietnamese_review_ui.py`
- **R**: new `tests/test_google_reliability.py`. This reuses C fake Crawlee/browser fixtures and U DOM fixtures. Policy tests execute the real adapter with a small fake retrying Crawlee engine; they do not prove every behavior of the installed engine.
- Additional unchanged suites: `tests/test_google_live_queue.py`, `tests/test_google_review_source.py`.

P1 denotes correctness/data-loss/access-gate risk if the contract fails; P2 denotes bounded extraction/coverage issues. Passing fixtures are not live UI verification. “Missing” identifies remaining coverage, not planned tests falsely marked complete.

## Scenario matrix

| Severity | Scenario | Input / trigger | Expected outcome | Current implementation / result | Existing or added coverage | Missing test / decision | Evidence |
|---|---|---|---|---|---|---|---|
| P1 | Startup / occupied 9222 | Unrelated Chrome owns default port | Choose free owned debug port; do not attach to unrelated Chrome | Explicit OS-selected port under retained startup lock; prior live 4-worker startups succeeded | D.test_debug_port_avoids_existing_listener; C.test_four_worker_driver_builders_preserve_profile_isolation | Cross-process port theft between bind/release and Chrome bind remains untested | Prior startup logs + four-store smoke |
| P1 | Startup / constructor throws | Fake driver raises | Same exception; release lock, restore env, cancel dump | Verified by fake constructor failure | D.test_startup_diagnostics_preserve_browser_behavior | Native process killed while holding OS lock | Offline D |
| P1 | Startup / hung constructor | Driver never returns | 90s stack evidence; external watchdog terminates owned process tree | Dump armed; no internal Python hard cancellation; previous smoke watchdog supplied externally | D.test_startup_diagnostics_preserve_browser_behavior | Fresh forced-kill regression of Windows Job Object | Historical startup stack; offline D |
| P1 | Startup / timeout configuration fails | set_page_load_timeout raises | Quit created driver; preserve exception; release lock | Verified | D.test_startup_diagnostics_preserve_browser_behavior | Driver.quit itself fails during cleanup | Offline D |
| P1 | Workers / profiles | Four workers or duplicate profile arguments | Distinct profiles; reject sharing | Four fake builders and duplicate-profile guard tested | C profile isolation and profile argument tests | Real simultaneous profile-lock contention on Windows | Smoke: 4 profiles released, all owned jobs zero active |
| P1 | Sessions / loss | Dead session or BrowserSessionError | Retire; retry only within budget; do not label store success | Existing manager + adapter, explicit policy limits | C.test_challenge_and_dead_session_are_not_store_local; R adapter mixed cases | OS-level Chrome crash at each extraction/publication boundary | Offline C/R |
| P1 | Identity / conflicting numbers | 12 versus 112 Main Street | Reject wrong branch | Whole house/street components required | B.test_branch_address; B.test_same_brand_candidates_require_unique_branch_evidence | Additional non-Latin house-number conventions | Original defect + B |
| P1 | Identity / generic or missing address | No numbered branch address | Remain unresolved, no metadata/reviews | Fail closed; resolver forwards expected_address | B.test_resolved_entity_requires_branch_address; I resolver contract | Documented policy for legitimate unnumbered addresses | Offline B/I |
| P1 | Identity / multiple candidates | Same brand, multiple matching branch candidates | Do not choose first ambiguous candidate | Unique matching candidate required | B.test_same_brand_candidates_require_unique_branch_evidence | Candidate collection rerender during click | Offline B |
| P2 | Identity / alias | Highlands Coffee vs Highlands Coffee Ben Van Don HCM | Remain unresolved unless an approved alias contract exists | No alias relaxation; one DLQ result, zero attributed reviews | R.test_saved_smoke_reproduces_alias_and_hours_gaps_without_weakening_identity | Design decision: curated aliases / evidence threshold; no implementation guessed | worker-03, PLACE_NOT_RESOLVED, matching address but differing title |
| P1 | Navigation / read-only timeout | PAGE_LOAD_TIMEOUT | Store-local terminal failure; continue safely; no same-store replay | Local failure and worker continuation tested | C.test_store_local_failure_continues_without_browser_restart | Navigation stage exceeds whole-store wall budget | Offline C; per-store pagination deadline is not a hard whole-job bound |
| P2 | Review tab / intercepted click | Search combobox receives review-tab click | UNKNOWN, never NO_REVIEWS; respect retry/deferral budget | Classified UNKNOWN; zero-budget policy now defers without retry | R.test_intercepted_review_click_stays_unknown_not_zero_reviews; R adapter UNKNOWN | Captured DOM/screenshot needed before targeted overlay handling | worker-01 first attempt, intercepted click; old access policy retried once |
| P1 | Review access / sign-in modal | Localized authentication dialog | Stop; do not bypass by fresh browser | Terminal access types cannot be overridden by AccessPolicy after fix | U.test_vietnamese_review_state; R adapter auth/challenge/rate_limit | Other locales and iframe dialogs | Offline U/R |
| P1 | Review access / challenge or 429 | ChallengeError / RateLimitedError with nonzero budgets | Terminal gate preserved, zero automatic fresh-session retries | Fixed typed terminal-gate precedence | R.test_real_adapter_policy_does_not_bypass_stop_or_retry_budget | Raw HTTP responses not mapped into typed errors remain unverified | Reproduced offline before fix |
| P2 | Sorting / decomposed Vietnamese | mới nhất menu option | Recognize Newest, confirm selected state | NFC normalization and selected signal tests pass | U.test_sort_newest_clicks_decomposed_vietnamese_label | Independent review date-order confirmation in real DOM | Historical SORT_FAILED; smoke confirms control label only |
| P1 | Sorting / missing menu | SortError | DLQ terminal store failure; no fabricated chronological order | Existing zero surface retries and local continuation | C.test_store_local_failure_continues_without_browser_restart | Stale menu / click succeeds but confirmation fails | Offline C; U selected signals |
| P1 | Zero reviews | Explicit localized no-review UI | NO_REVIEWS; distinguish from extraction failure | Localized classification fixture coverage | U.test_vietnamese_review_state | Full runner zero-review persistence/summary fixture | Zero available reviews not inferred from empty extraction |
| P1 | DOM change / missing cards | No cards with no reliable no-review evidence | UNKNOWN/stalled, not NO_REVIEWS | Paginator stalled contract and review classification preserved | R.test_pagination_does_not_label_limit_or_missing_cards_as_zero_reviews | Virtualized/recycled cards after rerender | Offline R |
| P1 | Pagination / count or scroll limit | Maximum scroll count reached | PARTIAL_LIMIT with durable rows, no failure/replay | Worker outcome PARTIAL; terminal queue FINISHED | C.test_partial_scan_is_not_error_or_automatic_replay; R pagination limit | Full crawler advertised-count drift fixture | Smoke: two stores stopped at 5 scrolls, 60 rows each |
| P1 | Pagination / deadline | Collection clock reaches deadline | PARTIAL_TIMEOUT / preserve progress | Paginator timeout tested; deadline is collection-local | R pagination deadline; C partial timeout | Hard whole-store deadline across startup/navigation/publication requires explicit watchdog contract | Prior smoke used external per-worker watchdog |
| P1 | CDP / VERIFIED same ID | Valid microsecond timestamp pair | Enrich DOM review only for matching ID | Verified collector path, exact microsecond preservation | R.test_cdp_status_never_discards_valid_dom_review; R timestamp pairs | Full saved RPC envelope replay and concurrent capture/drain stress | 129/129 VERIFIED in retained smoke |
| P1 | CDP / absent | No network record | DOM review retained, timestamps MISSING/null | Pass | R CDP MISSING and different-ID cases | Collector cannot initialize after partial socket setup | Offline R |
| P1 | CDP / failure | Collector.enrich raises or failed capture | DOM retained; PARSE_FAILED not lost review | Pass | R CDP PARSE_FAILED and collector_exception | Socket closure between drain and extraction | Offline R |
| P1 | CDP / ambiguity | Publication later than modification, uncertain pair | AMBIGUOUS; no trusted timestamp pair | Pass | R timestamp pairs and CDP AMBIGUOUS | Multiple conflicting RPC records for same ID | Offline R |
| P1 | CDP / wrong review ID | Network record belongs to another review | Never copy timestamp to current review | Pass | R.test_cdp_never_enriches_dom_review_with_another_review_id | Cross-store duplicate network ID collision diagnostics | Offline R |
| P1 | Business status | Open / ordinary closed / temporary / permanent / contradictory labels | Do not confuse outside-hours closed with permanent closure | UNKNOWN/null preserved on uncertain/conflicting evidence | M.test_schema_v2_business_status_is_not_hours_closure; M future-closing label | Real closure variants from captured DOM | Synthetic localized fixtures; live three stores OPERATIONAL |
| P1 | Metadata / provenance | Resolved entity URL contains coordinate, data_id or Place ID | Entity point only; distinguish identifier kind; no camera point | Verified parsing with ambiguity rejection | M.test_entity_location_and_identifier_are_explicit_not_camera | Google domains and URL forms not currently supported | Smoke metadata has data_id, not a fabricated Place ID |
| P2 | Hours / weekly collapsed control | Observed hours label, actual interactive ancestor not captured | Expand only confirmed collapsed control; restore UI; otherwise incomplete | Fixture expansion passes; actual smoke yielded no verified control and no weekly rows | M.test_hours_expansion_is_bounded_and_restores_control; R saved smoke gap | Capture actual control DOM/AX before any new selector or interaction | All three resolved stores weekly_hours_complete=false |
| P2 | Hours / split overnight 24h | Explicit weekly table intervals | Preserve shifts, next-day offset, 24h and closed days | Pass; absent days never invented | M weekly interval, line-break split-shift and complete-week tests | Holiday date-specific schedules; ambiguous AM/PM remain unknown | Offline M |
| P2 | Hours / next closing | Đóng cửa vào 23:00 / 0:00 / 3:00 | Explicit local time; no invented date/timezone | Pass in offline and retained live metadata | M.test_next_changes_preserve_local_time_without_inventing_timestamp | Explicit dated next transitions across DST | Smoke three resolved stores parsed next closing |
| P1 | Metadata / missing or throwing | Optional extraction unavailable | Continue review stage; preserve null/completeness | Pass | M.test_missing_optional_snapshot_does_not_prevent_review_stage | Partial failure of one optional field rather than whole snapshot | Offline M |
| P1 | Metadata / later review failure | Review pane/sort raises after snapshot | Snapshot survives independently | Pass; identity required before v2 publication | M snapshot survival, publication identity and atomic replacement tests | Power loss after atomic rename, before metadata index update | Offline M |
| P1 | Retry / zero UNKNOWN | UNKNOWN with max_browser_retries=0 | No fresh session; deferred state persists | FIXED: budget checked instead of unconditional first retry | R policy budgets and adapter UNKNOWN=0 | Real Crawlee native error-handler timeout outside handled exceptions | Old smoke unexpectedly attempted twice; failing regression reproduced |
| P1 | Retry / positive and mixed budgets | UNKNOWN and BROWSER_ERROR with budgets 1/2 | Honor each state budget, then defer/fail | FIXED: configured budget respected; request ceiling accommodates both state budgets | R policy budget and mixed adapter cases | Longer mixed error sequences; shared total-budget semantics not introduced | Offline R 0/1/2 budgets |
| P1 | DLQ / store-local vs systemic | Sort/parse/resolution fail vs ERROR/PERSISTING/security | Store-local terminal callback/DLQ; systemic worker stop | Existing local flags/stop-scope preserved | C local-failure, hard-failure, integration-error tests; R terminal gates | Explicit product decision: UNKNOWN remains scheduler deferral rather than DLQ | Existing AccessPolicy deferred state intentionally unchanged |
| P1 | Queue / concurrent claim | Four concurrent claimers on fresh SQLite queue | One owner per item | 12 distinct claims; duplicate claims rejected | R.test_dispatch_concurrent_claims_and_crashed_owner_are_not_replayed | Windows multiprocessing termination while BEGIN IMMEDIATE held | Threaded SQLite race test; native kill not simulated |
| P1 | Queue / crash and restart | Owner dies with RUNNING item | Preserve unknown state; never auto-reclaim/replay | RUNNING remains unclaimable; wrong-owner completion rejected | R concurrent claims/restart; C terminal dispatch | Approved explicit recovery workflow for unknown RUNNING tasks | Historical watchdog outputs preserved; no artifact mutation |
| P1 | Queue / terminal outcome | Completed, partial or failed crawl | FINISHED lifecycle separate from SUCCESS/PARTIAL/ERROR | Pass; terminal outcomes not reclaimed | C terminal and partial queue cases | Duplicate finish_dispatch by same owner | Current lifecycle contract unchanged |
| P1 | SQLite / transaction failure | Outbox insert aborts | Rollback state and outbox together | Failure injection passes | R.test_sqlite_transaction_failure_does_not_leave_state_without_outbox | Disk-full and durable WAL power-loss tests | Real SQLite trigger abort; no production source change needed |
| P1 | Publication / failure before commit | Parquet writer or atomic replace raises | Keep outbox and state; no half-published target | Pass | R publication crash before_publish; R atomic replace failure | Windows sharing violation / disk-full error variants | Injected OSError with real local Parquet output |
| P1 | Publication / crash after rename | Target exists but SQLite acknowledgement not done | Replay batch idempotently; clear outbox only after validation | Pass across repository reopen; one file/row | R publication crash after_publish | Concurrent publishers writing same batch under separate repo instances | Single-owner local filesystem assumption retained |
| P1 | Publication / invalid existing batch | Matching event-ID footer but missing rows | Reject; retain outbox; never silently lose review | FIXED: validate row count before acknowledging existing target | R.test_corrupt_existing_batch_cannot_acknowledge_and_drop_outbox | Full payload checksum validation beyond existing footer/row-count contract | Failing regression showed outbox previously cleared |

## Confirmed fixes (minimal source changes)

1. `crawl_experiment/orchestration/access_policy.py:67`: UNKNOWN retries now consult the existing `max_browser_retries` budget. Default positive-budget behavior remains supported; zero no longer causes an implicit first retry. UNKNOWN and BROWSER_ERROR retain per-state counters, not a newly invented shared total budget.
2. `crawl_experiment/orchestration/crawlee_adapter.py:131`: typed AuthRequiredError, ChallengeError and RateLimitedError keep their terminal/cooldown decisions rather than entering retryable UNKNOWN/BROWSER_ERROR classification. At line 213 the Crawlee retry ceiling is `2 * browser_budget + runtime_budget` (or larger existing RetryPolicy ceilings), covering the two independently counted browser-related states; it is zero when all configured budgets are zero. The default ceiling remains unchanged at 3.
3. `crawl_experiment/storage/parquet_review_writer.py:72`: existing batch row count must equal expected event-ID count before the existing-footer idempotency path acknowledges it. A malformed batch retains its outbox events. This does not claim full content/checksum validation or power-loss durability.

No source changes to worker architecture, startup, identity, hours selectors, review DOM extraction or timestamp enrichment. No dependencies or assertion weakening. A test fixture initially omitted its Parquet writer, so the outbox trigger was never exercised; the fixture was corrected to enable the intended real outbox path, preserving its rollback assertions.

## Executed validation

Baseline: 140 passed in 1.62s across the eight pre-existing focused Google suites.
Final: **178 passed, 0 failed, 0 skipped**, 3.09s, across those suites plus R (38 added parametrized cases). Two existing environment warnings concern optional pandas accelerators numexpr/bottleneck; no upgrades were made. No failures were hidden by skips or changed pre-existing expectations.

PowerShell command:

```powershell
$googleSuites = @(Get-ChildItem tests/test_google_*.py | ForEach-Object { $_.FullName })
& C:/Users/MSI/anaconda3/python.exe -m pytest -q @googleSuites --tb=short --basetemp=output/pytest-google-qa-final-20261104-v2
```

Executed source/test `git diff --check` also passed. No repository-wide coverage percentage is asserted.

## Unresolved P2 findings and decisions

- **Highlands:** saved Google title is `Highlands Coffee Ben Van Don HCM`, source name `Highlands Coffee`; address agrees but exact-name identity rejects it. Keep rejection. Any alias approval/normalization contract requires an explicit decision and independently labeled branch fixtures.
- **Weekly hours:** snapshots contain `Hiện giờ mở cửa trong tuần`, but no full DOM/control ancestry was saved. The bounded expansion supports an existing confirmed collapsed control; the real smoke did not expose one through supported selectors. Keep `weekly_hours_complete=false`. Capture only the resolved place hours control and table DOM/AX in a separately authorized diagnostic before proposing a selector change.
- **Intercepted review click:** first Oc Loan attempt reported the search combobox intercepting the review-tab click, then UNKNOWN. No screenshot/DOM proves why the overlay intercepted it. Do not add sleeps or broad popup dismissal. The fixed zero-budget policy defers without replay; current deferral is durable scheduler state, intentionally distinct from terminal DLQ.
- **Watchdog contract:** crawler timeout begins at pagination; the CLI overall watchdog covers the run. Historical bounded smoke wrappers supplied a hard per-worker wall watchdog. A hard whole-store deadline in the standard CLI is not yet established; decide whether to standardize that contract before unattended batches.
- Real Windows Job Object forced-kill recovery, malformed RPC-envelope coverage, non-English/Vietnamese variants, full payload corruption validation, holiday calendars and power-loss durability remain unverified.

**GO for a representative bounded live validation only**, separately authorized, with explicit per-worker/overall watchdogs and zero retry budgets if requested. Include exact-name branches plus intentionally unresolved branches, treat weekly metadata completeness separately, and assert one attempt per store. This is **not crawler-v1 final sign-off or batch readiness**; aliases and watchdog/recovery policy need decisions, and actual hours-control evidence is still missing. No automatic rerun.

## Subsequent approved name-match adjustment (2026-11-04)

The later user request permits name variation with strong branch-address evidence. `navigator.py` now supports normalized name equality, spacing differences, distinctive full-name prefixes/qualifiers and high similarity (0.94), still requiring house/street agreement. This supersedes the matrix’s historical exact-name-only findings; saved smoke artifacts retain their original unresolved outcomes. The Highlands replay test is now `R.test_saved_smoke_name_variant_requires_branch_address_and_preserves_hours_gap`, with positive supported-name-variant and negative conflicting-number assertions. 215 focused tests PASS; live precision/recall after this adjustment is UNVERIFIED. No address-only or distance-only auto-match was introduced; no thresholds were changed in the separate scored canonical resolver.


## Eight-store / four-worker DOM result (2026-11-04)

Decision: retain the current crawler architecture and continue Data Platform development. This bounded run provides positive runtime evidence for browser startup, worker-level reuse, DOM review collection and persistence. It is not final crawler acceptance or evidence of unattended large-batch readiness.

Evidence: `output/google-eight-20261104-230831-609/summary.json` (operator-provided completed run).
SHA-256: `3b5fc0f94ce8ebecfa716b87f94d0c146d17fd7be3f22329735fbb3c34650c7c`.
The figures below were checked against this summary only; Parquet contents, branch identities and source-code revision were not independently verified in this documentation update. Preserve the local run artifacts; this document carries aggregate evidence without raw review records or browser profiles.

| Measure | Recorded result |
|---|---|
| Mode / bounds | DOM; 4 workers; 8 stores; 5 scrolls; 120s configured per-store budget; 600s overall limit |
| Runtime / dispatch | 106.031 seconds; 8 terminal dispatch outcomes; status FINISHED |
| Store outcomes | 3 COMPLETE/FULL; 4 PARTIAL_LIMIT/max_scrolls; 1 PLACE_RESOLUTION_FAILED/DLQ |
| Persistence | 262 SQLite records; 262 Parquet rows in 7 files; 0 pending events; no reconciliation errors |
| Rows per file/job | 9, 19, 60, 60, 60, 14, 40; unresolved job emitted no Parquet file |
| Browser lifecycle | 4 sessions created; 0 restarts; stores per session 2, 2, 1, 3 |
| Cleanup | cleanup_verified=true; all 4 worker exit codes 0; all owned jobs active_after=0 |
| Resources | Sampled peak owned-process RSS 10,649,227,264 bytes (about 9.92 GiB) |
| Acceptance | acceptance_pass=false; 3 full, 4 partial and 1 unknown coverage outcomes |

VERIFIED from the saved summary: seven jobs emitted reviews, four workers started browsers, sessions were reused, partial results were retained, and reported SQLite/Parquet counts reconcile with no pending outbox events. The four partial outcomes reached the deliberate five-scroll cap; they are not recorded browser crashes. FINISHED means dispatch finished, not that all stores succeeded. FULL is the crawler's natural-end classification, not independent proof of exhaustive reviews or correct branch identity. No throughput extrapolation is supported by this small bounded sample.

UNVERIFIED / remaining: independently validate the seven selected branches and review identity/deduplication; inspect the unresolved job; retain the existing weekly-hours and whole-store watchdog limitations above. This run used DOM and does not by itself establish the status of earlier default-mode, serialization or test-contract findings. No new pytest run, browser launch or source change was performed for this documentation update.

Next: inspect saved evidence for the unresolved job and label the seven selected Google branches; reconcile remaining review findings with the current implementation and focused offline tests before final acceptance or a larger authorized crawl.


## Pre-commit offline validation

Executed the nine `tests/test_google_*.py` suites with `C:/Users/MSI/anaconda3/python.exe -B -m pytest -q -p no:cacheprovider`, `--tb=short` and a fresh `output/pytest-google-commit-<uuid>` basetemp: **227 passed, 2 warnings in 3.18s**. The initial default-temp invocation had 177 passed and 50 fixture setup errors because Windows denied access to the existing pytest temporary directory; the fresh-basetemp rerun passed all cases. Warnings concern existing optional pandas numexpr/bottleneck versions. No dependencies were installed and no live crawler was launched. This validates the current focused contracts; it does not change the bounded live run acceptance result.
