# Area Demographics and Ward Assignment V1

Local staging only. Existing Overture/Google Silver, ER, human history, Category
Quality and catalog Gold Staging releases are read-only inputs.

## Source and temporal meaning

- Endpoint: <https://diachiviet.com/api/v1/xa/ho-chi-minh.json>.
- Documentation: <https://diachiviet.com/api-tinh-thanh/> and
  <https://diachiviet.com/du-lieu-mo/>.
- License: CC BY 4.0. Attribution: Địa Chỉ Việt (diachiviet.com), dữ liệu mở
  đơn vị hành chính, CC BY 4.0. Terms: <https://diachiviet.com/dieu-khoan-su-dung/>.
  The provider presents reference data without a correctness guarantee and
  prohibits abusive automated traffic. Ingestion fetches one static JSON;
  subsequent normalization and dbt builds use local snapshots only.
- Audited response: HTTP 200, application/json, 52,226 bytes, 168 units. Top-level
  keys: `nguon`, `giay_phep`, `ngay`, `du_lieu_den`, `cot`, `dong`.
  Source columns: `ma_xa`, `ten`, `loai`, `ma_tinh`, `ten_tinh`, `ma_buu_chinh`,
  `dien_tich_km2`, `quy_mo_dan_so`, `nghi_quyet`, `url`.
- Snapshot built 2026-11-05; legal-data cutoff 2026-09-20. These are **not** the
  population reference year. The provider describes population/area from 2025
  reorganization proposals. `reference_year=2025` and
  `population_basis=2025_REORGANIZATION_PROPOSAL_NOT_CENSUS` are explicit metadata,
  supported by the provider documentation, not a census assertion.
- Khánh Hội: ward code `27265`, city code `79`, 1.07 km², 94,507 people.
  Both numerical values come from the API, not from the polygon. Its resolution
  field is `Nghị quyết số 1685/NQ-UBTVQH15`; these demographic values have not been
  independently checked against a primary demographic document.
- Administrative code/name check uses `config/admin_areas/qd19_2025_hcm_codes.json`,
  extracted from the HCMC section of Decision 19/2025/QĐ-TTg, effective 2025-07-01:
  <https://congbaocdn.chinhphu.vn/CongBaoCP/VanBan/2025/6/45430/57441-1-2025921-92219-2025-qd-ttg.pdf>.
  Khánh Hội appears on printed page 49 (PDF page index 46). The complete primary
  PDF and retrieval metadata are cached under
  `output/admin-reference-bronze/official-qd19-2025/`; its SHA256 is recorded in
  the checked code reference. Code agreement does not validate demographics.

## Artifacts and grains

Bronze: immutable snapshot directory containing exact `response.json`, original
retrieval `metadata.json`, and checksummed `manifest.json`. Replaying a snapshot
preserves its retrieval time; a new HTTP observation creates a new snapshot.

Area Silver: `area_demographics.parquet`, one row per ward code in the pinned
provider snapshot. Main columns: `ward_code`, `ward_name`, `city_code`, `city_name`,
`area_km2`, `population`, `population_density`, `reference_year`, `legal_document`,
`source_url`, `source_record_url`, `source_version`, `data_quality_status`.
Additional fields preserve raw source rows, demographic basis, build/cutoff dates,
retrieval time, source-field provenance, official code evidence, flags and rule
version. Original NULL and empty values remain distinguishable in `raw_row_json`.
Invalid numbers become NULL with explicit flags; they do not become zero.

Area Enrichment: `ward_assignment.parquet`, one row per existing catalog anchor.
Main columns: `store_id`, `overture_id`, typed Google identity, original selected
address, source fields, coordinates, `ward_code`, `ward_name`, `area_id`,
`assignment_method`, `assignment_status`, `address_lookup_status`,
`address_ward_code`, `spatial_ward_code`, `spatial_status`, `boundary_version`,
`reference_version`, `boundary_distance_m`, `near_boundary`, `flags`,
`provenance_json`, `rule_version`.

The separate dbt project `pipelines/area_enrichment` materializes:

| Model | Grain / meaning |
|---|---|
| `dim_area` | One officially code-checked area in the pinned reference; currently all 168 HCMC units, even where no polygon is available. |
| `fact_area_demographics` | One area × reference year × provider source version; preserves proposal basis and source provenance. |
| `store_area_mapping` | One existing catalog anchor; adds ward assignment without changing catalog or identity mapping. |
| `mart_fnb_by_area` | One demographic grain with counts of assigned anchors, anchors/km², anchors/1,000 population. |

All 725 anchors remain in the mapping. Unresolved/conflicting assignments are
excluded from area numerators and explicitly accounted for in `dq_summary.json`.
Zero anchors in another ward means zero from this **Khánh Hội candidate catalog**,
not zero businesses in that ward. No metric is a verified physical-business count
or a claim of complete city/ward coverage. Ratios combine the current catalog with
2025 proposal denominators, not synchronized census observations.

## Ward assignment policy

1. Parse observed administrative labels using the existing Google address parser,
   or use structured source ward/city fields when supplied. The current source
   Silver schemas lack separate ward/city columns, so observed selected address
   strings are parsed; their original strings remain unchanged.
2. Exact lookup by `(city_code, normalized ward label)` in the code-checked
   reference. Prefix/case/Unicode/accent normalization is for comparison only;
   collisions remain multiple candidates. No substring, fuzzy match, district-only
   assignment or pilot-wide default city/ward. A city must be observed or supplied
   as a source code. Google fields are used only for links already attached to the
   input catalog; unresolved ER proposals are not borrowed.
3. Fall back to a unique polygon `covers(Point)` result for missing, unknown or
   ambiguous address lookup. Reuses `experiments.overture_silver.consumer.load_ward`.
   Only the existing Khánh Hội polygon is loaded; it does not cover all 168 units.
4. Different unique address/spatial codes: `AREA_CONFLICT`, final ward/area NULL,
   method `UNRESOLVED`. Multiple covering polygons: `AMBIGUOUS`, no silent choice.
5. Outside the available polygon is not proof of another ward. A unique address
   match can still be `RESOLVED/ADDRESS`, with `OUTSIDE_SUPPORTED_BOUNDARIES`.
   If that address claims the ward whose polygon is available but the point is
   outside it, also flag `ADDRESS_WARD_OUTSIDE_ITS_BOUNDARY`.
6. Old numbered wards and unrecognized old names are not converted to current
   wards. They are retained as unresolved address evidence; spatial fallback may
   independently resolve a valid point.
7. `--near-boundary-m` defaults to 10 m as a configurable warning only. Approximate
   boundary distance uses a local equirectangular metric; membership uses the
   unchanged EPSG:4326 polygon. It does not snap points or choose a nearest ward.

Boundary version hashes both existing GeoJSON and metadata. Source remains OSM/
Nominatim relation 19260589, with the existing ODbL attribution and valid-from
2025-07-01. Polygon approximate area (1.059391912109375 km²) is retained separately
from provider area (1.07 km²); neither is silently substituted for the other.

## Quality and publication

Hard fails: malformed schema, invalid/duplicate codes, duplicate anchor keys,
unvalidated supported boundary code, missing provenance, missing input checksums,
referential-integrity failure, incorrect conflict handling, join fanout, dbt test
failure or changed inputs during publication. Optional missing/invalid demographic
numbers, unknown/old address labels, unavailable boundaries and near-boundary
points are warnings. Administrative conflicts remain in Silver but cannot be
looked up or published into the area dimension as validated codes.

All releases have input/code hashes, versions, checksummed Parquet artifacts and
DQ summaries. Existing publication locking, immutable release directories and
atomic `current.json` replacement are reused. Failed releases cannot replace the
previous active pointer. Determinism means identical logical tables for pinned
inputs, not identical dbt log timestamps or database bytes.

## PowerShell replay

```powershell
Set-Location 'D:\Projects\integration-recomentdation-fnb-data-platform-20261028-pipeline'
$py = 'D:\Projects\integration-recomentdation\output\ci01-2ac84b90\venv\Scripts\python.exe'
$env:PYTHONPATH = 'src;.'

# Already downloaded: replay without network.
& $py -B scripts/area_demographics.py ingest `
  --snapshot output/admin-reference-bronze/diachiviet-hcm/20261109T100301598510Z-0b851ae1015a/response.json `
  --bronze-root output/admin-reference-bronze/diachiviet-hcm-v1

& $py -B scripts/area_demographics.py silver `
  --snapshot-dir output/admin-reference-bronze/diachiviet-hcm-v1/snapshot-3ef6cb5ba2a8580a94bbdb69 `
  --output-root output/area-demographics-silver-v1

& $py -B scripts/area_enrichment.py `
  --reference-root output/area-demographics-silver-v1 `
  --catalog-root output/gold-staging-v1 `
  --output-root output/area-enrichment-staging-v1
```

For a deliberate future refresh, replace the offline `ingest --snapshot ...`
with `ingest --url https://diachiviet.com/api/v1/xa/ho-chi-minh.json`, retain the
returned snapshot directory, and pass it to `silver --snapshot-dir`. Do not call
this endpoint per POI or per dbt build. A different province requires a suitable
official-code reference via `--codes`; unknown codes are not implicitly verified.
Expanding address lookup across HCMC reuses the 168-unit reference. Expanding
spatial fallback requires separately supplied, validated and versioned boundaries;
V1 does not download them or modify the crawler's Overture polygon selection.
