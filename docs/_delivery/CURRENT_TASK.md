- GOOGLE DOM FOLLOW-UP 2026-11-04: completed run `output/google-eight-20261104-230831-609/summary.json` supersedes the earlier smoke-ready checkpoint below. VERIFIED from summary: 4-worker startup/reuse/cleanup and 262 persisted review rows across 7 files; 3 full, 4 deliberately scroll-limited, 1 unresolved; acceptance_pass=false. No final identity/coverage sign-off or new tests in this documentation update. NEXT: inspect unresolved job, validate seven selected branches, then reconcile current code/offline tests with remaining findings. Details: [Google crawler QA](GOOGLE_CRAWLER_V1_QA.md#eight-store--four-worker-dom-result-2026-11-04). PRE-COMMIT VERIFIED: 227 focused Google tests PASS (2 existing optional dependency warnings); fresh basetemp avoids default-temp access error.
# Current task — M1.1 Offline OSM Source Adapter

State: IMPLEMENTED_AND_LOCALLY_VERIFIED; local seven-file commit approved.
Prerequisite: user confirms hosted CI green on d7277343 (31 tests, fresh Python 3.11 install).

## VERIFIED scope
- src/fnb_platform/osm.py and osm_fnb_mapping.v1.json: in-memory normalization,
  source-specific identities, raw observation/tag/provenance retention, F&B mapping.
- Node lat/lon; way/relation GeoJSON Point/Polygon/MultiPolygon or supplied center.
  Valid polygons use representative_point; derived/source centers flagged approximate.
- Missing/invalid geometry rejected with diagnostics; no invalid-geometry fallback.
- Highest source version then canonical JSON dedup; distinct source identities kept.
- Existing ward validation/covers and catalog dataclasses reused, schema unchanged.
- tests/test_osm.py: 44 synthetic cases; 75 total tests passed, 2 existing warnings,
  0.88s on local Python 3.12.7. Exact command in STATUS.md.
- pyproject.toml packages mapping JSON and includes OSM tests; offline runner includes them.
- All 16 protected crawler/script files retain their pre-task SHA-256 digest; excluded from this commit.

## Limits
No live data, input geometry assembly, publication or completeness claims.
M1.1 hosted execution/fresh package build remain unverified.

## Next action
Review M1.1 and approve M1.2 bounded real OSM acquisition/conversion with versioned
manifest, provenance/rights, geometry assembly, input checksum and profiling.
Do not begin M1.2–M1.4 or push. Only the seven M1.1 paths are approved for this local commit.
