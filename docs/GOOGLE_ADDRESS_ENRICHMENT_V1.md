# Google-first Address Enrichment V1

Offline transformation from published Google Silver M1 and ER V3. Google is the
preferred address observation only for eligible MATCH_CANDIDATE attachments.
Raw, Silver, M1.5 and ER V3 remain unchanged. No crawler, external reference query,
geometry requirement, ML, business merge or Gold publication.

## Actual coverage and grains

| Metric | Count |
|---|---:|
| Unique typed Google identities | 460 |
| Google identities with source address | 426 |
| PARSED address status | 381 |
| Usable observed street label | 392 |
| AMBIGUOUS address status | 20 |
| PARTIAL address status | 25 |
| Missing source address | 34 |
| No street suitable for these analytics | 68 |
| Source-supported ward label | 369 |
| Source-supported city label | 425 |
| Distinct normalized street names | 46 |
| City-scoped observed street dictionary entries | 47 |
| Overture IDs retained | 725 |
| ER relationships retained in candidate references | 476 |
| Provisional Overture attachments | 346 |
| Distinct Google identities behind those attachments | 345 |
| Provisional Overture attachments with usable street | 322 |
| Zero-candidate Overture IDs retained | 251 |

PARSED means an unambiguous street extraction without a critical unresolved
expression; optional house/ward fields may still be NULL. It does not mean all
address fields are populated or the physical address has been verified. Eleven
additional rows have a usable street but PARTIAL/AMBIGUOUS status for other fields.
Of the 68 without usable streets, 34 have no source address and 34 have only
partial/ambiguous or internal-premise address information. There are 91 unknown
ward values and 35 unknown city values, including missing metadata rows.

M1 already provides one current observation per typed Google identity. This layer
reuses that dedup: zero extra exact duplicate rows were found. Exact repeated input
rows can be collapsed deterministically; conflicting duplicate Silver identity
rows fail validation rather than introducing a competing metadata precedence rule.
Names, phone, proximity and address similarity are never identity dedup keys.

## Outputs and rules

Active pointer: `output/google-address-enrichment-v1/current.json`.
Release: `4470ff1dee0accf7dfc86568`.

| File | Grain / key | Rows |
|---|---|---:|
| `google_address_enrichment.parquet` | `(google_id_kind, google_identity_value)` | 460 |
| `provisional_poi_address_links.parquet` | `overture_id`, including unenriched IDs | 725 |
| `street_dictionary.parquet` | observed normalized street name + city scope | 47 |
| `summary.json` | counts, top 20, 20 difficult cases, inputs and semantics | — |

Manifest includes exact Arrow schemas, artifact hashes, input hashes, parser code
hashes and version `google-address-enrichment-v1`. Publication validates a staging
bundle before atomically replacing the current pointer.

Google enrichment contains original name/address, requested house/alley/lot/street/
ward/city/legacy-district fields, `address_parse_status`, `street_status`, analytical
eligibility, notes, field provenance and parser evidence. `house_identifier` keeps
suffixes/paths (122 vs 122a vs 122/14) and may be a RANGE, explicitly identified by
`house_kind`. Multiple observed houses yield NULL plus the candidate values in
evidence; no arbitrary first-house selection. Explicit alleys and lots are separate.

The parser reuses M1.5 Unicode/text normalization and preserves M1.5 parsing in
evidence. A small Google-focused adapter separates source administrative clauses,
plus codes, building/lot context, explicit streets and compound expressions.
Two passes build an exact alias inventory from clear Google street observations,
then recognize otherwise bare street clauses. No fuzzy matching, invented street
labels, nearest-road fallback or Overture-address repair is used.

Street dictionary aliases collapse accents/case/spacing, road prefixes and generic
numbered-road forms. It does not declare that `20 Thước` and `20 Thước Đoàn Văn Bơ`
are the same street; those remain distinct observed labels. Dictionary IDs are
not official road IDs or proof of complete physical-street identity/coverage.
An unknown city remains a separate dictionary scope: Bến Vân Đồn has one such row,
which explains 47 dictionary entries versus 46 distinct normalized names.

Administration is source-stated, not coordinate-inferred. Explicit Phường/Quận/
Thành phố clauses are retained as claims. An unlabeled locality matching the
existing versioned administrative label reference can populate ward/city with
that evidence method. Other bare localities such as Phú Nhuận or Vĩnh Hội remain
`locality_name_raw`, with ward/district NULL unless explicitly supported. Quận
values are recorded only when present as explicit source district claims; no
modern district membership or old-to-new ward crosswalk is inferred. Conflicting
old/new ward or malformed city clauses remain NULL with actual claims in evidence.

Only a single eligible MATCH_CANDIDATE may transfer the Google observation into
the derived Overture attachment. Status stays PROVISIONAL and ER_PENDING.
Ambiguous parse fields remain NULL, even in provisional attachments. All other
ER classes retain their original Overture address without Google field transfer.
Multiple Google candidates cause a hold; all relationship keys/classifications/
observation refs remain in `candidate_relationships_json`. Shared identities retain
separate Overture rows and flags; 12 provisional rows carry a shared-ID flag.
The 346 attachments map to 345 distinct Google IDs, without merging any source ID.

| Overture attachment status | IDs |
|---|---:|
| PROVISIONAL | 346 |
| NOT_ENRICHED_ER_REVIEW | 89 |
| NOT_ENRICHED_ER_NON_MATCH_CANDIDATE | 6 |
| NOT_ENRICHED_ER_INSUFFICIENT_DATA | 31 |
| NOT_ENRICHED_MULTIPLE_CANDIDATES | 2 |
| NOT_ENRICHED_NO_GOOGLE_CANDIDATE | 251 |

These are source-ID statuses, not sums of ER link classifications: two source IDs
have multiple relationships. Original source names, addresses and NULLs are retained.

## Top 20 observed street names

Count distinct Google identities with `street_analysis_eligible=true`, not Overture
links, reviews or verified physical businesses. All Google observations are counted,
including candidates not provisionally linked to an Overture POI. This is not
complete Khánh Hội street coverage. The report groups by observed normalized name;
dictionary IDs retain city scope. Bến Vân Đồn includes one unknown-city observation;
the other city claims in this top 20 are Hồ Chí Minh.

| Street label | Unique Google identities |
|---|---:|
| Tôn Đản | 60 |
| Vĩnh Khánh | 56 |
| Bến Vân Đồn | 38 |
| Hoàng Diệu | 37 |
| Đoàn Văn Bơ | 36 |
| Khánh Hội | 19 |
| Số 41 | 19 |
| Nguyễn Hữu Hào | 18 |
| Tân Vĩnh | 15 |
| Xóm Chiếu | 15 |
| Tôn Thất Thuyết | 14 |
| Số 45 | 13 |
| Vĩnh Hội | 13 |
| 20 Thước | 2 |
| Số 10C | 2 |
| Số 11 | 2 |
| Số 12A | 2 |
| Số 38 | 2 |
| Số 50 | 2 |
| 20 Thước Đoàn Văn Bơ | 1 |

## Twenty difficult/risky actual parses

Address excerpts below identify the source issue. Full original strings, Google
IDs, provenance and all candidates are in `summary.json.difficult_cases` and the
enrichment Parquet. No missing field was filled from an illustrative business.

| Google observation | Source issue | Handling |
|---|---|---|
| Cơm Tấm Bin Bo | `95 D5, Thạnh Mỹ Tây` | House/street role unclear; both NULL, locality level unknown |
| Tiệm trà MOKA | `459-460-461 Lo P Đường Số 20` | Unsupported multiple numeric expression; house/street NULL |
| Quán An _ món trưa + Chè bình dân+ nước mát mát | `500 32 Đoàn Văn Bơ` | No first-number guess; house/street NULL |
| Bun rieu | `H3 Building, Y012, 183/Y/012G Hoàng Diệu` | Multiple/unsupported unit expressions retained as evidence |
| Bột Chiên Ông Ba (Chợ Cũ) | `40/53A, Đ. Vĩnh Khánh Đ. Vĩnh Hội` | House retained; street NULL, two-road expression flagged |
| Bếp Chay Minh | `Tôn Đản/243/26A Khánh Hội` | Multiple streets; no street selection |
| Honey Moon | `Tòa nhà H3, Hoàng Diệu/384 Khánh Hội` | Building context retained; street NULL |
| Cơm Ngọc Đức | `Đ. cư xá Vĩnh Hội/G30 Đ. Số 50` | Compound internal/numbered road; street NULL |
| Súp Mộc - Hoàng Diệu | `Đ. cư xá Vĩnh Hội/K48 Hoàng Diệu` | Multiple streets; K48 retained as literal |
| Le Blanc. Coffee & Spaces | `Tân Vĩnh/83 Khánh Hội` | Multiple streets; no preferred road invented |
| Bún Bò Huế Duyên Mai Quận 4 | `cổng, Bến Vân Đồn/132 Nguyễn Hữu Hào, Quận 4` | Multiple streets; explicit district claim retained |
| Cơm Tấm Hoàng Hoa | `Đ. Số 50/G28A Đ. cư xá Vĩnh Hội` | Compound recognized; not one bogus dictionary label |
| ABIESBBQ NƯỚNG & LẨU | `Bến Vân Đồn/G42 Đ. Số 50` | Ambiguous street even though ER has name/address support |
| NGÔ KÝ MÌ GIA | `Đ. cư xá Vĩnh Hội/F42 Đ. Số 50` | Multiple streets; no street selection |
| Tiệm nhà Gió | `…phường 9 Quận 4…, Khánh Hội` | Conflicting ward claims; ward NULL |
| Bánh cuốn nóng | `7-1, Đường Số 10A` | Reversed/unclear range; house NULL, street retained |
| Nhà hàng chay Nhất Tâm | `T6 Phường 6,…, Khánh Hội` | Conflicting ward claims; ward NULL |
| Quán Cá Biển Nướng | `198/271B, 231B Đ. Vĩnh Khánh` | House NULL; both candidates retained; street usable |
| Sophia's persian kitchen | `151-155 Bến Vân Đồn phường 6 Q4 rivegate,…Khánh Hội` | Range retained; conflicting ward claim unresolved |
| Hi Coffee | `…Thành phố H ồ, Hồ Chí Minh 72800` | Conflicting city claims; city NULL; street retained |

## Files, checks and rerun

New files: `src/fnb_platform/google_address_enrichment.py`,
`scripts/google_address_enrichment.py`, `tests/test_google_address_enrichment.py`,
and this report. No existing parser or ER policy was edited.

75 focused/M1.5/M1 tests passed. Real-data validation confirmed:
unique Google/current and Overture keys, all 725 Overture IDs and 476 candidate
relationships preserved, all 251 zero-candidate IDs preserved, only approved class
proposals receiving Google observations, typed identity counts, original raw/NULL
values and fallback field provenance, distinct-ID dictionary/top counts, immutable
inputs, deterministic reruns and reversed-input equality. Metadata snapshot hashes
and address values were checked against source field provenance before publication.
Validation/publication sequence took 10.842s locally.

Checks: `output/google-address-enrichment-v1-validation/validation_checks.json`.

```powershell
& 'D:/Projects/integration-recomentdation/output/ci01-2ac84b90/venv/Scripts/python.exe' -B -m scripts.google_address_enrichment `
  --silver-root output/google-silver-m1 `
  --er-root output/er-m2-v3-experimental `
  --output-root output/google-address-enrichment-v1
```

This is an observed enrichment layer. PARSED/provisional street counts do not
certify identity, operating status, eligibility, exact postal structure or complete
business coverage. Existing Google data restrictions and source provenance remain
applicable; no additional collection was performed. No commit or push.
