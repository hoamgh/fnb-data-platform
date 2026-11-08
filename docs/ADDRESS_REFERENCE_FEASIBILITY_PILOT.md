# Reference-first address normalization — bounded feasibility pilot

**Decision: NO-GO for replacing the current parser or automatically publishing
canonical addresses.** The reference-first direction is promising as a lexical
candidate/evidence layer, but this pilot exposes specific blockers before adoption.
No ER, Silver or Gold artifacts were changed. No POI crawling or geocoding occurred.

## Scope and evaluation basis

Exactly **50 distinct actual source addresses**: 45 Overture and five Google,
selected from pinned M1.5 feature files. The sample deliberately emphasizes hard
locators, ranges, alleys, lots, numbered streets, compound addresses and one
out-of-area control. It is not a representative accuracy sample of the catalog.

`expected_samples.json` contains independent agent-written structural targets
from reading these source literals, not outputs copied from either parser. Six
rows lack one unambiguous parent street target; 44 have a clear lexical target.
Targets do not establish physical door numbers, verified business locations,
official street identity or human-reviewed ground truth. Coverage below means
agreement with these structural targets, not real-world precision/recall.

## Audit of existing references and bounded collection

| Asset | Confirmed availability and limits |
|---|---|
| `config/admin_areas/phuong_khanh_hoi.geojson` + metadata | Valid stored Polygon, EPSG:4326; working ID `vn-hcm-ward-27265`, metadata valid_from 2025-07-01, retrieved 2026-10-01. Metadata source: OSM/Nominatim, not an official surveyed boundary. No new official-code certification was performed. |
| Original Overture Bronze / existing Silver | POIs and address observations, not an independent road/alias/geometry registry. Not reused as street ground truth. |
| Original Geofabrik PBF | Existing 331,767,844-byte Vietnam snapshot, source cutoff 2026-10-27T21:23:59Z. Its published local extraction is configured F&B POIs, not streets. Saved working ways are candidate/support data, not a complete local road registry. The country PBF was not expanded or migrated. |
| Overture transportation | Available as a possible future segment-based reference; no local transportation release was found in the inspected POI assets. Not downloaded for this pilot. [Official guide](https://docs.overturemaps.org/guides/transportation/) |
| Fresh bounded OSM road reference | One Overpass query for named highway ways in the polygon bbox plus 0.001° buffer. 726 ways returned; 286 intersect the stored ward polygon. Geometry/tags only, no POI crawl or geocode. |

The existing metadata's legal reference is [Resolution 1685/NQ-UBTVQH15](https://xaydungchinhsach.chinhphu.vn/toan-van-nghi-quyet-so-1685-nq-ubtvqh15-sap-xep-cac-dvhc-cap-xa-cua-thanh-pho-ho-chi-minh-nam-2025-119250616211341304.htm),
Article 1(9). It establishes the new ward arrangement, but does not independently
validate this GeoJSON's vertices or prove every old ward maps wholly to Khánh Hội.
The pilot does not infer a current district or translate historical ward claims.

OSM is used strictly as an address/geometry reference, not reintroduced as a POI
source. Attribution: © OpenStreetMap contributors, [ODbL](https://www.openstreetmap.org/copyright).
The request and exact bytes/hash are retained. Query syntax and output metadata
follow [Overpass QL](https://wiki.openstreetmap.org/wiki/Overpass_API/Overpass_QL).

Reference snapshot timestamp: **2026-11-06T17:46:06Z**; retrieved
2026-11-06T17:47:26.932812Z (00:47 local, 7 November).
Version/checksum: `6e3c12e9038c142fb0ad6911efb8ecb2c32c3c13a68a9004807f19035906a412`.
Object timestamps are retained in alias evidence. **Legal street-name effective
dates remain NULL/UNKNOWN**; observation/update timestamps are not fabricated
as effective dates. `old_name` is retained as historical candidate evidence only.

## Reference-first procedure and output contract

1. Build the reference before reading/parsing the 50 evaluation addresses.
2. Group same-name ways only through shared OSM nodes: **175 connected components**,
   **326 per-way alias provenance records**. Opposite carriageways/disconnected
   pieces are not automatically equated. IDs `pilot-osm-connected:<anchor-way-id>`
   are snapshot component IDs, **not official or permanent canonical street IDs**.
3. Basic Unicode/accent/case/whitespace cleanup; retain original address.
4. Match exact reference aliases using contiguous whole tokens, with generic
   road-prefix/numbered-street handling. No per-street keyword mapping or POI-name
   exceptions. Fuzzy matching is deliberately disabled in this small pilot.
5. Check house/road/alley roles. A numeric alias requires an explicit road marker
   or preceding locator: `79 Đường Số 41` differs from `41 Tôn Đản`, `Hẻm 41 Tôn Đản`
   and `Số 41 Tôn Đản`. Filter identifiable administrative/premise contexts.
6. Compute distance only to text-matched candidate geometry. Local metric
   equirectangular projection is adequate for this tiny bbox; it is approximate,
   not survey-grade. <=100m corroborates; 100–200m stays uncertain; >200m disagrees.
   These are experimental evidence bands, not geocoding truth. Multiple different
   text streets stay ambiguous even if one is nearer. No nearest-road fallback.
7. Parse remaining house/range/alley/lot literals relative to matched spans. Missing
   values stay missing; reference-only and unsupported numeric expressions remain
   flagged. Confidence is qualitative and explicitly scoped to **street reference
   support**, not a complete address. House output is UNVERIFIED_LITERAL.
8. Assign the stored ward ID only from point-in-polygon. **49 inside, one outside**;
   outside/unknown gets no ward. District remains NULL for all 50. This establishes
   consistency with the stored polygon, not independently verified legal accuracy.

Population and area are excluded from parser inputs, confidence and exported
boundary provenance values. Original administrative metadata is not edited.

## Results by field

Numbers are correct returned values / samples where the structural target exists.
The error column counts extra or incompatible returned values across all 50 rows.
Uncertain M1.5 components retain their original parse status; these counts do not
claim that M1.5 previously published them as verified address facts.

| Field | Target present | M1.5 aligned | Reference-first aligned | Extra/incompatible values M1.5 → pilot |
|---|---:|---:|---:|---:|
| Street name, lexical candidate | 44 | 15/44 | 39/44 | 2 → 1 candidate |
| House/unit locator literal | 27 | 9/27 | 26/27 | 3 → 1 unverified literal |
| Valid increasing house range | 2 | 2/2 | 2/2 | 1 → 0 |
| Explicit alley | 4 | 4/4 | 4/4 | 0 → 0 |
| Structured lot | 8 | 0/8 | 8/8 | 0 → 0 |

M1.5 keeps premise text but has no dedicated structured lot field; its zero does
not mean the original lot information was lost. The decreasing `7-1` range is
retained as AMBIGUOUS in M1.5; the pilot leaves it unset with a note. Likewise,
reference-only source house literals are not confirmed business door numbers.

**Strict reference component ID + geometry support: 20/50**, all with lexical
street names matching the structural targets. This is an additional verification
stage, not an apples-to-apples comparison with M1.5's lexical street output.
No wrongly named strict street assignment was observed in these 20; this is not
an estimate of true canonical-ID/physical-address precision.

| Pilot street outcome | Addresses |
|---|---:|
| Text and geometry supported | 20 |
| Ambiguous geometry / no unique component | 12 |
| Multiple named streets | 3 |
| Text candidate but geometry disagrees | 8 |
| No usable reference alias | 7 |

## Improvements, failures and remaining ambiguity

- `R58 Nguyễn Hữu Hào`, `K44 Hoàng Diệu`, `R32A Bến Vân Đồn`, `U34 Nguyễn Hữu Hào`
  retain full alphanumeric locators instead of losing the street behind them.
  Geometry still prevents automatic street ID for R32A, whose parent-road
  candidates are over 400m away. Recognizing text is separate from accepting it.
- `E15 Số 38`, `F48 Đ. Số 50` receive supported numbered-street references.
  `11 Đường 41` recognizes the same reference name as `Đ. Số 41`, but two nearby
  disconnected carriageway components keep the street ID ambiguous.
- `54 Lô O, Nguyễn Hữu Hào` retains house 54, lot O and the reference street;
  `Chung cư, 016 Lô A, Tôn Thất Thuyết` still misses the nonleading house 016.
- `158-160, Hoàng Diệu` keeps a range; `301B Đ. Tôn Đản` keeps 301b. The latter
  has a reference candidate about 120m away and is deliberately not forced.
- `Đường Tôn Thất Thuyết 163` separates the explicit road-first number from
  the street. `Đường Tôn Đản 293` also recovers 293, but geometry stays uncertain.
- **Wrong candidate:** in the out-of-area Google address `Đ. Vĩnh Lộc, Tân Vĩnh Lộc…`,
  token-boundary alias `Tân Vĩnh` incorrectly matches the longer administrative
  phrase `Tân Vĩnh Lộc`. Geometry at about 16km prevents an assigned street ID.
  Token boundaries alone are insufficient without complete administrative context
  and longest-applicable reference-span validation.
- **Wrong/unresolved house literal:** Google `198/271B, 231B Đ. Vĩnh Khánh…` still
  returns the first house `198/271b` although two interpretations exist. It is
  exported as UNVERIFIED_LITERAL and explicitly listed in `known_pilot_errors`.
  It must not be used as a canonical house without resolving both candidates.
- `Tòa nhà H3, Hoàng Diệu/384 Khánh Hội` and `Tân Vĩnh/83 Khánh Hội` correctly stay
  multi-street ambiguous; the nearest road is not silently chosen.
- `Lô A, Tôn Thất Thuyết/CC Khánh Hội` exposes insufficient recognition of `CC`
  premise context: Khánh Hội also becomes a candidate. No street ID is forced.
- `Hẻm 538 Đoàn Văn Bơ` and other deep-alley addresses can be far from the parent
  road centerline. The reference contains named alley geometry, but this minimal
  pilot does not yet validate an alley-to-parent topology/hierarchy to corroborate
  the parent street. Distance disagreement here is not proof the address is wrong.
- `Lô R, Đường Số 20`, Xóm Chiếu and Vĩnh Hội cases expose reference completeness
  and strict polygon-clipping limits. A frontage/border road can be outside the
  ward polygon while serving an address inside it. No nearby-road substitution
  or street alias was invented to fill these gaps.

## Cost and feasibility across wards

One bounded reference request took **32.927s**. Offline comparison for 50 addresses
was about **0.6–0.7s**, using existing Python, Shapely and Parquet dependencies;
no packages, services or frameworks were added. These are pilot observations,
not an extrapolated city-scale benchmark or API cost guarantee.

For more wards, reuse one versioned regional road snapshot rather than request
reference data per POI. Maintain aliases, source IDs, effective-date status and
admin polygons centrally in files; partition spatially for consumers. A token
alias index plus existing STRtree/DuckDB-style spatial filtering avoids scanning
every road per address. Include frontage/buffer roads and explicit alley links.
Do not duplicate/merge a border road merely because it intersects multiple wards.
Effective-dated ward lineage and street-family identity require curation; connected
OSM components alone are not a stable city-wide canonical street registry.

Before adopting reference-first as the canonical parser, the minimum blockers are:

1. Resolve administrative/premise span ambiguity and compound house candidates.
2. Define street-family IDs across carriageways/segments without name-only merging.
3. Cover border/frontage/unnamed roads and alley-to-parent geometry relationships.
4. Supply verified effective-date evidence where historical normalization is required,
   or explicitly preserve UNKNOWN rather than assert historical canonical values.
5. Human-review the fixed sample and an independent holdout; do not tune and report
   accuracy on this same small agent-annotated sample.

## Files, validation and offline rerun

New code only: `experiments/address_reference_pilot/run.py` and
`tests/test_address_reference_pilot.py`. Ten focused offline tests pass: numbered
road/house/alley distinction, token matching, no nearest fallback, ambiguous streets,
missing/outside coordinates, ward suffixes, historical aliases, disconnected IDs,
input-order determinism and meaningful house suffix/path differences.

All artifacts are in `output/address-reference-pilot-v1/`:

- `roads_overpass_raw.json`, `roads_source.json`: immutable reference bytes/query/source.
- `street_reference.json`: 175 provisional connected components, geometry and aliases.
- `expected_samples.json`: the fixed 50 literal assessment targets.
- `pilot_comparison.csv`: raw source, M1.5/reference features, candidate distances,
  uncertainty/confidence, ward assignment and source provenance for each address.
- `pilot_summary.json`: field counts, outcomes and known errors.
- `validation_checks.json`, `manifest.json`: deterministic and hash evidence.

CSV/registry bytes are identical on rerun; summary differs only in runtime.
Unicode/JSON round-trip and point-in-polygon behavior were checked. All original
input, M1.5, ER V2/V3 artifact hashes are unchanged. No further reference network
request occurs when rerunning:

```powershell
$env:PYTHONPATH = 'src;.'
& 'D:/Projects/integration-recomentdation/output/ci01-2ac84b90/venv/Scripts/python.exe' -B -m experiments.address_reference_pilot.run `
  --directory output/address-reference-pilot-v1 `
  --samples output/address-reference-pilot-v1/expected_samples.json
```
