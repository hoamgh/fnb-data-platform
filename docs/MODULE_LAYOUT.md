# Module layout and debugging

Canonical implementations live under `src/fnb_platform/`:

```text
fnb_platform/
  overture/       acquisition, snapshot, catalog, comparison runner/publication,
                  silver, eligibility, eligibility_policy
  google/         places_silver, reviews_silver, address_enrichment
  integration/    comparison_features, entity_resolution (V1),
                  entity_resolution_v2, entity_resolution_v3, category_quality,
                  er_category_review_export, human_decisions, er_auto_accept
  gold/           staging, area_enrichment
  reference/      area_demographics
  publication.py  shared publication primitives
```

`crawl_experiment/` owns Google acquisition, browser sessions and review persistence.
Live workers remain in `scripts/`; historical runners remain under `experiments/`.
`pipelines/gold_staging/` and `pipelines/area_enrichment/` retain their dbt projects.
There is no production Gold deployment, Airflow DAG or Spark pipeline in this refactor.

## Entrypoints

Existing `scripts/*.py` commands and historical Overture CLIs remain supported.
The old flat `fnb_platform` module paths alias the canonical module object, including
private APIs, exceptions and globals used by monkeypatches. They contain no second
pipeline implementation. New module CLIs support `python -B -m` with these names:

| Stage | Module |
|---|---|
| Overture acquisition | `fnb_platform.overture.acquisition` |
| Overture Silver | `fnb_platform.overture.silver` |
| Overture eligibility | `fnb_platform.overture.eligibility` |
| Places Silver | `fnb_platform.google.places_silver` |
| Reviews Silver | `fnb_platform.google.reviews_silver` |
| Address enrichment | `fnb_platform.google.address_enrichment` |
| Comparison | `fnb_platform.integration.comparison_features` |
| ER V1/V2/V3 publisher | `fnb_platform.integration.entity_resolution` |
| Category quality | `fnb_platform.integration.category_quality` |
| Review export | `fnb_platform.integration.er_category_review_export` |
| Human decisions | `fnb_platform.integration.human_decisions` |
| Auto-accept | `fnb_platform.integration.er_auto_accept` |
| Gold staging | `fnb_platform.gold.staging` |
| Area enrichment | `fnb_platform.gold.area_enrichment` |
| Area demographics | `fnb_platform.reference.area_demographics` |

Use `--help` for unchanged arguments. ER V2/V3 are policy modules selected by the
existing ER publisher, not separate publisher CLIs.

## Debugging

1. Start with the CLI module above; follow `pin_inputs` / admission before transforms.
2. Read the selected immutable manifest and its pinned lineage; do not guess latest inputs.
3. Check canonical `__file__` when tracing imports. Historical names resolve to that same object.
4. Follow validation, writer, final input guards, then pointer activation. Fault stages and
   retained failed staging remain unchanged; do not repair historical artifacts in place.
5. For attachment, inspect Silver current eligibility, accepted typed mapping, then
   `review_attachment` / `unmatched_review_accounting` and dbt conservation tests.
6. Use the explicit offline/dbt profiles in `scripts/run_offline_tests.py`; fixtures write
   to temporary outputs. Multi-minute profiles must be launched manually per instructions.

## Compatibility and fingerprints

New publications fingerprint the actual canonical implementation. Code signatures and
release IDs may differ from pre-refactor runs. Existing manifests and current pointers
are not rewritten; historical verification retains its existing contracts. Resuming a
campaign pinned to older source bytes requires its original code checkpoint, not a
fabricated old signature. Run new campaigns in a fresh output root.

`overture_incremental.py` intentionally stays flat: its canonical serializers are used
across Google/Integration and directly fingerprinted. `publication.py`, models,
categories and OSM retain their existing locations. Compatibility aliases are retained
for callers; cross-domain imports may still pass through them. Package entrypoints and
dbt paths are stable boundaries for the next demo orchestration milestone.
