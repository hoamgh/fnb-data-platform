# MVP spatial pre-dedup

Run from the pipeline worktree (offline; original inputs remain read-only):

```powershell
python -B experiments/overture_gold_contract/spatial_prededup.py --input-root D:/Projects/integration-recomentdation-fnb-data-platform-20261028 --output-dir output/spatial-prededup-v1 --radius-m 50
python -m pytest tests/test_spatial_prededup.py tests/test_overture_eligibility.py -q
```

Use a fresh output directory for each run; existing outputs are never overwritten.
`summary.json` is the completion marker, not a Gold publication/pointer. It records
input/code/output hashes, radius, counts and measured runtime.

Reuses M1 input checksum/schema/replay validation and M242 eligibility to reproduce
557 original + 168 hierarchy additions. `candidates.parquet` preserves every original
candidate column and appends DQ/inferred geography. `candidate_pairs.parquet` contains
unique ordered source-ID pairs within the inclusive radius, including non-likely pairs.
No source POI is merged or deleted. Missing/duplicate IDs or invalid coordinates are
retained with DQ flags but not spatially paired (upstream authoritative-input validation
may reject such corruption before selection).

Shapely STRtree (existing dependency) blocks via spherical bounding envelopes; only
neighbors receive haversine distance and string comparisons. No N² matrix is created;
dense neighborhoods can still produce quadratic-sized output. Distances are spherical,
not ellipsoidal survey measurements. Parquet uses existing PyArrow; DuckDB is not required.

Names/addresses reuse Silver NFKC, casefold and whitespace normalization, then Python
SequenceMatcher (0–1; not a match probability). Names use the arithmetic mean of
forward and reverse scores (v2), guaranteeing symmetry regardless of ID order.
Address scoring retains the original directional v1 calculation.
Missing strings yield null similarity. A provisional likely flag requires name >=0.90,
address >=0.85 and no conflicting leading house numbers. These are review heuristics,
not calibrated identity decisions; identical brand/address text can still be ambiguous.

Geography is inferred only for polygon-covered points using the already pinned
Khánh Hội boundary and its hash. Original ward/city and other source columns remain
unchanged. The source address schema has locality/region rather than explicit ward/city;
old district localities are not relabeled as ward or city. These inferred labels describe
current geographic containment, not verified postal addresses. The boundary's OSM
provenance does not introduce OSM POIs into the integration flow.

## Future improvements — not implemented

- H3/grid-based blocking.
- Multi-pass blocking with names and addresses.
- Inaccurate/missing-coordinate handling beyond DQ flags.
- Google Maps-assisted verification, subject to access/storage/reuse restrictions;
  shared crawler-selected data_id alone must never trigger a merge.
- Incremental deduplication.
- Blocking recall evaluation and performance benchmark at 70k POIs.

No live crawling, Google-assisted verification, Gold publication or recommendation.
