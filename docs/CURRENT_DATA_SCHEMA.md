# Schema dữ liệu hiện có — inventory thực tế

Ngày kiểm tra: 2026-11-05T17:01:13.426686+00:00. Không phải schema Gold đề xuất. Chỉ mô tả dữ liệu active Overture/Google và các artifact DQ/pre-dedup. Foody đang paused; OSM không thuộc luồng hiện tại.

Có 725 ID nguồn trong catalog/các queue; 437 có file metadata Google được tham chiếu bởi dispatch. Đây không phải số cửa hàng canonical hoặc số match đã xác minh. Các job bị ngắt có thể có file chưa được dispatch tham chiếu; chưa gộp các file đó vào coverage.

## Khóa và grain cần hiểu trước

- Overture giữ source_id gốc; một release dùng khóa logic (source, source_release, source_id).
- Catalog: place_key = source + ":" + source_id, ví dụ prefix overture:.
- dispatch.id là số thứ tự queue, khác dispatch.result.id (Store.id do crawler tạo).
- Nối metadata.store_id / reviews.store_id với dispatch.result.id, rồi lấy dispatch.place_key để nối catalog/source_id. Không nối store_id trực tiếp với UUID Overture.
- Google google_identifier phải đi cùng google_identifier_type; data_id không được coi là official Place ID. Một Google entity có thể được nhiều POI nguồn chọn nhưng chưa xác nhận các POI đó trùng nhau.
- Review Parquet có grain observation/change event, không có ràng buộc PK toàn bộ lake. Cùng source_review_id có thể lặp giữa run, content_hash và job/source POI.
- UNIQUE(source, source_review_id) của review_state chỉ có hiệu lực trong từng SQLite job. Không chứng minh đã dedup toàn bộ dữ liệu giữa các lần crawl.
- Identity/provenance giữ theo source và observation; chưa có trusted ER mapping hay canonical dim_store được xác nhận trong inventory này.

## Quy ước kiểu

Parquet lấy trực tiếp từ Arrow schema. Nullable là cờ lưu trữ, không thay cho quy tắc DQ nghiệp vụ. JSON ghi các kiểu thực tế quan sát qua các snapshot đã đọc; field chỉ thấy null không có nghĩa field vĩnh viễn chỉ nhận null. JSON ngày giờ vẫn là string; review published_at/last_modified_at trong Parquet là timestamp UTC.

## Overture Bronze historical

Path: `D:\Projects\integration-recomentdation-fnb-data-platform-20261028\data\bronze\overture\places.parquet`

Rows: **378036**. Grain: Một source POI trong snapshot gốc; giữ nested schema.

| Field | Physical type | Nullable |
|---|---|---|
| `id` | `string` | true |
| `names` | `struct` | true |
| `names.primary` | `string` | true |
| `names.common` | `map` | true |
| `names.common.key` | `string` | false |
| `names.common.value` | `string` | true |
| `names.rules` | `list` | true |
| `names.rules.element` | `struct` | true |
| `names.rules.element.variant` | `string` | true |
| `names.rules.element.language` | `string` | true |
| `names.rules.element.perspectives` | `struct` | true |
| `names.rules.element.perspectives.mode` | `string` | true |
| `names.rules.element.perspectives.countries` | `list` | true |
| `names.rules.element.perspectives.countries.element` | `string` | true |
| `names.rules.element.value` | `string` | true |
| `names.rules.element.between` | `list` | true |
| `names.rules.element.between.element` | `double` | true |
| `names.rules.element.side` | `string` | true |
| `basic_category` | `string` | true |
| `taxonomy` | `struct` | true |
| `taxonomy.primary` | `string` | true |
| `taxonomy.hierarchy` | `list` | true |
| `taxonomy.hierarchy.element` | `string` | true |
| `taxonomy.alternates` | `list` | true |
| `taxonomy.alternates.element` | `string` | true |
| `confidence` | `double` | true |
| `operating_status` | `string` | true |
| `addresses` | `list` | true |
| `addresses.element` | `struct` | true |
| `addresses.element.freeform` | `string` | true |
| `addresses.element.locality` | `string` | true |
| `addresses.element.postcode` | `string` | true |
| `addresses.element.region` | `string` | true |
| `addresses.element.country` | `string` | true |
| `geometry` | `binary` | true |

## Overture raw October cache

Path: `D:\Projects\integration-recomentdation-fnb-data-platform-20261028\output\district4-profile\cache\overture-2026-10-21.0-7667bf6c7576.parquet`

Rows: **35712**. Grain: Raw extract đã cache; không khẳng định là toàn TP.HCM.

| Field | Physical type | Nullable |
|---|---|---|
| `id` | `string` | true |
| `geometry` | `binary` | true |
| `confidence` | `double` | true |
| `websites` | `list` | true |
| `websites.element` | `string` | true |
| `emails` | `list` | true |
| `emails.element` | `string` | true |
| `socials` | `list` | true |
| `socials.element` | `string` | true |
| `phones` | `list` | true |
| `phones.element` | `string` | true |
| `brand` | `struct` | true |
| `brand.wikidata` | `string` | true |
| `brand.names` | `struct` | true |
| `brand.names.primary` | `string` | true |
| `brand.names.common` | `map` | true |
| `brand.names.common.key` | `string` | false |
| `brand.names.common.value` | `string` | true |
| `brand.names.rules` | `list` | true |
| `brand.names.rules.element` | `struct` | true |
| `brand.names.rules.element.variant` | `string` | true |
| `brand.names.rules.element.language` | `string` | true |
| `brand.names.rules.element.perspectives` | `struct` | true |
| `brand.names.rules.element.perspectives.mode` | `string` | true |
| `brand.names.rules.element.perspectives.countries` | `list` | true |
| `brand.names.rules.element.perspectives.countries.element` | `string` | true |
| `brand.names.rules.element.value` | `string` | true |
| `brand.names.rules.element.between` | `list` | true |
| `brand.names.rules.element.between.element` | `double` | true |
| `brand.names.rules.element.side` | `string` | true |
| `addresses` | `list` | true |
| `addresses.element` | `struct` | true |
| `addresses.element.freeform` | `string` | true |
| `addresses.element.locality` | `string` | true |
| `addresses.element.postcode` | `string` | true |
| `addresses.element.region` | `string` | true |
| `addresses.element.country` | `string` | true |
| `names` | `struct` | true |
| `names.primary` | `string` | true |
| `names.common` | `map` | true |
| `names.common.key` | `string` | false |
| `names.common.value` | `string` | true |
| `names.rules` | `list` | true |
| `names.rules.element` | `struct` | true |
| `names.rules.element.variant` | `string` | true |
| `names.rules.element.language` | `string` | true |
| `names.rules.element.perspectives` | `struct` | true |
| `names.rules.element.perspectives.mode` | `string` | true |
| `names.rules.element.perspectives.countries` | `list` | true |
| `names.rules.element.perspectives.countries.element` | `string` | true |
| `names.rules.element.value` | `string` | true |
| `names.rules.element.between` | `list` | true |
| `names.rules.element.between.element` | `double` | true |
| `names.rules.element.side` | `string` | true |
| `sources` | `list` | true |
| `sources.element` | `struct` | true |
| `sources.element.property` | `string` | true |
| `sources.element.dataset` | `string` | true |
| `sources.element.license` | `string` | true |
| `sources.element.record_id` | `string` | true |
| `sources.element.update_time` | `string` | true |
| `sources.element.confidence` | `double` | true |
| `sources.element.between` | `list` | true |
| `sources.element.between.element` | `double` | true |
| `sources.element.provider` | `string` | true |
| `sources.element.resource` | `string` | true |
| `sources.element.version` | `string` | true |
| `operating_status` | `string` | true |
| `basic_category` | `string` | true |
| `taxonomy` | `struct` | true |
| `taxonomy.primary` | `string` | true |
| `taxonomy.hierarchy` | `list` | true |
| `taxonomy.hierarchy.element` | `string` | true |
| `taxonomy.alternates` | `list` | true |
| `taxonomy.alternates.element` | `string` | true |
| `version` | `int32` | true |
| `bbox` | `struct` | true |
| `bbox.xmin` | `double` | true |
| `bbox.xmax` | `double` | true |
| `bbox.ymin` | `double` | true |
| `bbox.ymax` | `double` | true |

## Overture Silver

Path: `D:\Projects\integration-recomentdation-fnb-data-platform-20261028\output\overture-silver-m241\releases\2026-10-21.0\current_state.parquet`

Rows: **6199**. Grain: Một POI nguồn trong release; khóa logic (source, source_release, source_id).

| Field | Physical type | Nullable |
|---|---|---|
| `source_id` | `string` | true |
| `source` | `string` | true |
| `source_release` | `string` | true |
| `source_manifest_sha256` | `string` | true |
| `name` | `string` | true |
| `name_normalized` | `string` | true |
| `address` | `string` | true |
| `address_normalized` | `string` | true |
| `basic_category` | `string` | true |
| `taxonomy_primary` | `string` | true |
| `is_fnb` | `bool` | true |
| `longitude` | `double` | true |
| `latitude` | `double` | true |
| `inside_ward` | `bool` | true |
| `on_ward_boundary` | `bool` | true |
| `recommendation_category` | `string` | true |
| `dq_severity` | `string` | true |
| `gold_candidate` | `bool` | true |
| `source_operating_status` | `string` | true |
| `verified_business_status` | `string` | true |
| `confidence` | `double` | true |
| `dq_valid` | `bool` | true |
| `dq_reasons` | `list` | true |
| `dq_reasons.element` | `string` | true |
| `scope_policy` | `string` | true |
| `source_presence` | `string` | true |

## F&B eligibility

Path: `D:\Projects\integration-recomentdation-fnb-data-platform-20261028-pipeline\output\overture-fnb-eligibility-m1\releases\3bf02697d8f1d8fc34ccb752\eligibility.parquet`

Rows: **6199**. Grain: Một Silver POI được đánh giá eligibility, gồm cả không được chọn.

| Field | Physical type | Nullable |
|---|---|---|
| `source` | `string` | true |
| `source_id` | `string` | true |
| `source_release` | `string` | true |
| `source_manifest_sha256` | `string` | true |
| `name` | `string` | true |
| `name_normalized` | `string` | true |
| `address` | `string` | true |
| `address_normalized` | `string` | true |
| `longitude` | `double` | true |
| `latitude` | `double` | true |
| `geometry` | `binary` | true |
| `inside_ward` | `bool` | true |
| `on_ward_boundary` | `bool` | true |
| `taxonomy_primary` | `string` | true |
| `taxonomy_hierarchy` | `list` | true |
| `taxonomy_hierarchy.element` | `string` | true |
| `taxonomy_alternates` | `list` | true |
| `taxonomy_alternates.element` | `string` | true |
| `basic_category` | `string` | true |
| `taxonomy_reason` | `string` | true |
| `eligibility_reason` | `string` | true |
| `is_fnb_candidate` | `bool` | true |
| `original_filter_candidate` | `bool` | true |
| `eligibility_basis` | `string` | true |
| `candidate_status` | `string` | true |
| `identity_status` | `string` | true |
| `verified_business_status` | `string` | true |
| `source_operating_status` | `string` | true |
| `source_presence` | `string` | true |
| `confidence` | `double` | true |
| `dq_valid` | `bool` | true |
| `dq_severity` | `string` | true |
| `dq_reasons` | `list` | true |
| `dq_reasons.element` | `string` | true |
| `eligibility_policy_version` | `string` | true |

## F&B candidates

Path: `D:\Projects\integration-recomentdation-fnb-data-platform-20261028-pipeline\output\overture-fnb-eligibility-m1\releases\3bf02697d8f1d8fc34ccb752\candidates.parquet`

Rows: **725**. Grain: 725 candidate provisional, không phải 725 quán đã xác minh.

| Field | Physical type | Nullable |
|---|---|---|
| `source` | `string` | true |
| `source_id` | `string` | true |
| `source_release` | `string` | true |
| `source_manifest_sha256` | `string` | true |
| `name` | `string` | true |
| `name_normalized` | `string` | true |
| `address` | `string` | true |
| `address_normalized` | `string` | true |
| `longitude` | `double` | true |
| `latitude` | `double` | true |
| `geometry` | `binary` | true |
| `inside_ward` | `bool` | true |
| `on_ward_boundary` | `bool` | true |
| `taxonomy_primary` | `string` | true |
| `taxonomy_hierarchy` | `list` | true |
| `taxonomy_hierarchy.element` | `string` | true |
| `taxonomy_alternates` | `list` | true |
| `taxonomy_alternates.element` | `string` | true |
| `basic_category` | `string` | true |
| `taxonomy_reason` | `string` | true |
| `eligibility_reason` | `string` | true |
| `is_fnb_candidate` | `bool` | true |
| `original_filter_candidate` | `bool` | true |
| `eligibility_basis` | `string` | true |
| `candidate_status` | `string` | true |
| `identity_status` | `string` | true |
| `verified_business_status` | `string` | true |
| `source_operating_status` | `string` | true |
| `source_presence` | `string` | true |
| `confidence` | `double` | true |
| `dq_valid` | `bool` | true |
| `dq_severity` | `string` | true |
| `dq_reasons` | `list` | true |
| `dq_reasons.element` | `string` | true |
| `eligibility_policy_version` | `string` | true |

## Missing from release

Path: `D:\Projects\integration-recomentdation-fnb-data-platform-20261028-pipeline\output\overture-fnb-eligibility-m1\releases\3bf02697d8f1d8fc34ccb752\missing_from_release.parquet`

Rows: **24**. Grain: Một source_id không xuất hiện trong release sau; không suy ra đóng cửa.

| Field | Physical type | Nullable |
|---|---|---|
| `source_id` | `string` | true |
| `source_release` | `string` | true |
| `source_presence` | `string` | true |
| `verified_business_status` | `string` | true |
| `explanation` | `string` | true |

## Spatial pre-dedup candidates

Path: `D:\Projects\integration-recomentdation-fnb-data-platform-20261028-pipeline\output\spatial-prededup-v2\candidates.parquet`

Rows: **725**. Grain: 725 candidate giữ nguyên ID, bổ sung DQ và suy luận địa lý.

| Field | Physical type | Nullable |
|---|---|---|
| `source` | `string` | true |
| `source_id` | `string` | true |
| `source_release` | `string` | true |
| `source_manifest_sha256` | `string` | true |
| `name` | `string` | true |
| `name_normalized` | `string` | true |
| `address` | `string` | true |
| `address_normalized` | `string` | true |
| `longitude` | `double` | true |
| `latitude` | `double` | true |
| `geometry` | `binary` | true |
| `inside_ward` | `bool` | true |
| `on_ward_boundary` | `bool` | true |
| `taxonomy_primary` | `string` | true |
| `taxonomy_hierarchy` | `list` | true |
| `taxonomy_hierarchy.element` | `string` | true |
| `taxonomy_alternates` | `list` | true |
| `taxonomy_alternates.element` | `string` | true |
| `basic_category` | `string` | true |
| `taxonomy_reason` | `string` | true |
| `eligibility_reason` | `string` | true |
| `is_fnb_candidate` | `bool` | true |
| `original_filter_candidate` | `bool` | true |
| `eligibility_basis` | `string` | true |
| `candidate_status` | `string` | true |
| `identity_status` | `string` | true |
| `verified_business_status` | `string` | true |
| `source_operating_status` | `string` | true |
| `source_presence` | `string` | true |
| `confidence` | `double` | true |
| `dq_valid` | `bool` | true |
| `dq_severity` | `string` | true |
| `dq_reasons` | `list` | true |
| `dq_reasons.element` | `string` | true |
| `eligibility_policy_version` | `string` | true |
| `prededup_dq_reasons` | `list` | true |
| `prededup_dq_reasons.element` | `string` | true |
| `spatial_index_eligible` | `bool` | true |
| `ward_original` | `string` | true |
| `city_original` | `string` | true |
| `ward_inferred` | `string` | true |
| `city_inferred` | `string` | true |
| `geography_inference_basis` | `string` | true |
| `geography_boundary_sha256` | `string` | true |

## Spatial pre-dedup pairs

Path: `D:\Projects\integration-recomentdation-fnb-data-platform-20261028-pipeline\output\spatial-prededup-v2\candidate_pairs.parquet`

Rows: **3442**. Grain: Một cặp POI không hướng trong bán kính cấu hình; không phải quyết định merge.

| Field | Physical type | Nullable |
|---|---|---|
| `left_id` | `string` | true |
| `right_id` | `string` | true |
| `distance_m` | `double` | true |
| `name_similarity` | `double` | true |
| `address_similarity` | `double` | true |
| `likely_duplicate` | `bool` | true |
| `evidence` | `string` | true |
| `reason` | `string` | true |

## Google input catalog 725

Path: `D:\Projects\integration-recomentdation-fnb-data-platform-20261028-pipeline\output\google-m2-725-v5\catalog.parquet`

Rows: **725**. Grain: Một job nguồn; place_key duy nhất trong catalog.

| Field | Physical type | Nullable |
|---|---|---|
| `place_key` | `string` | true |
| `source` | `string` | true |
| `name` | `string` | true |
| `address` | `string` | true |
| `lat` | `double` | true |
| `lng` | `double` | true |
| `category_hierarchy` | `list` | true |
| `category_hierarchy.element` | `string` | true |
| `admin_area_name` | `string` | true |
| `identity_status` | `string` | true |
| `source_release` | `string` | true |
| `source_address_missing` | `bool` | true |
| `search_mode` | `string` | true |
| `dq_flags` | `list` | true |
| `dq_flags.element` | `string` | true |
| `geography_inference_basis` | `string` | true |
| `geography_boundary_sha256` | `string` | true |
| `search_eligible` | `bool` | true |
| `search_reason` | `null` | true |

## Google review Parquet — schema 1

Inspected 389 files; 10929 physical event rows across those files. Not a unique-review count.

Example: `D:\Projects\integration-recomentdation-fnb-data-platform-20261028\output\google-eight-20261104-230831-609\worker-01\job-00\lake\crawl_date=2026-11-04\run_id=20261104-160832\part-e3d099d0bdc09cc63f56.parquet`

| Field | Physical type | Nullable |
|---|---|---|
| `run_id` | `string` | true |
| `source` | `string` | true |
| `source_review_id` | `string` | true |
| `store_id` | `string` | true |
| `reviewer_name` | `string` | true |
| `rating` | `double` | true |
| `review_text` | `string` | true |
| `review_date_raw` | `string` | true |
| `owner_response` | `string` | true |
| `review_url` | `string` | true |
| `image_urls` | `list` | true |
| `image_urls.element` | `string` | true |
| `observed_at` | `string` | true |
| `content_hash` | `string` | true |
| `change_type` | `string` | true |
| `published_at` | `timestamp[us, tz=UTC]` | true |
| `last_modified_at` | `timestamp[us, tz=UTC]` | true |
| `timestamp_source` | `string` | true |
| `timestamp_status` | `string` | true |
| `change_diagnostics` | `list` | true |
| `change_diagnostics.element` | `string` | true |
| `reviewer_profile_url` | `string` | true |
| `reviewer_id` | `string` | true |
| `reviewer_avatar_url` | `string` | true |
| `reviewer_metadata_raw` | `string` | true |
| `reviewer_review_count` | `int64` | true |
| `reviewer_photo_count` | `int64` | true |
| `reviewer_is_local_guide` | `bool` | true |

## Google metadata snapshots

Records inspected: 445. `[]` denotes nested array elements. Fields may be absent in older versions.

| Field path | Observed JSON types |
|---|---|
| `address` | string |
| `business_status` | string |
| `category` | null, string |
| `collected_at` | string |
| `extraction_evidence` | object |
| `extraction_evidence.hours_expansion` | null, object |
| `extraction_evidence.hours_expansion.attempted` | boolean |
| `extraction_evidence.hours_expansion.error_type` | string |
| `extraction_evidence.hours_expansion.restored` | boolean, null |
| `extraction_evidence.hours_expansion.result` | string |
| `extraction_evidence.hours_source` | string |
| `extraction_evidence.identifier_source` | string |
| `extraction_evidence.identity_requirement` | string |
| `extraction_evidence.location_source` | string |
| `extraction_evidence.next_change_policy` | string |
| `extraction_evidence.status_conflict` | boolean |
| `google_identifier` | string |
| `google_identifier_type` | string |
| `google_place_id` | null |
| `latitude` | number |
| `longitude` | number |
| `maps_url` | string |
| `menu_url` | null, string |
| `metadata_completeness` | object |
| `metadata_completeness.missing_or_unknown` | array |
| `metadata_completeness.missing_or_unknown[]` | string |
| `metadata_completeness.weekly_hours_complete` | boolean |
| `missing_fields` | array |
| `missing_fields[]` | string |
| `name` | string |
| `next_closing_time` | null, object |
| `next_closing_time.at` | null |
| `next_closing_time.day_label` | null |
| `next_closing_time.local_time` | string |
| `next_closing_time.raw_label` | string |
| `next_opening_time` | null, object |
| `next_opening_time.at` | null |
| `next_opening_time.day_label` | null |
| `next_opening_time.local_time` | string |
| `next_opening_time.raw_label` | string |
| `observed_at` | string |
| `open_now` | boolean, null |
| `opening_hours` | null, object |
| `opening_hours.expansion` | object |
| `opening_hours.expansion.attempted` | boolean |
| `opening_hours.expansion.error_type` | string |
| `opening_hours.expansion.restored` | boolean, null |
| `opening_hours.expansion.result` | string |
| `opening_hours.raw_labels` | array |
| `opening_hours.raw_labels[]` | string |
| `opening_hours.raw_rows` | array |
| `opening_hours.raw_rows[]` | object |
| `opening_hours.raw_rows[].day` | string |
| `opening_hours.raw_rows[].day_text` | string |
| `opening_hours.raw_rows[].hours_text` | string |
| `opening_hours.raw_rows[].shifts` | array |
| `opening_hours.raw_rows[].shifts[]` | string |
| `opening_hours.summary` | string |
| `opening_hours.weekly` | array |
| `opening_hours.weekly[]` | object |
| `opening_hours.weekly[].day` | string |
| `opening_hours.weekly[].hours` | string |
| `phone` | null, string |
| `plus_code` | null, string |
| `price_range` | null, string |
| `rating` | null, number |
| `raw_status_labels` | array |
| `raw_status_labels[]` | string |
| `review_count` | integer, null |
| `schema_version` | integer |
| `source` | string |
| `source_url` | string |
| `special_hours_warnings` | array |
| `special_hours_warnings[]` | string |
| `store_id` | string |
| `visible_detail_items` | array |
| `visible_detail_items[]` | object |
| `visible_detail_items[].aria_label` | null, string |
| `visible_detail_items[].item_id` | string |
| `visible_detail_items[].text` | null, string |
| `visible_detail_items[].url` | null, string |
| `website` | null, string |
| `weekly_opening_hours` | array |
| `weekly_opening_hours[]` | object |
| `weekly_opening_hours[].day` | string |
| `weekly_opening_hours[].intervals` | array |
| `weekly_opening_hours[].intervals[]` | object |
| `weekly_opening_hours[].intervals[].closes` | string |
| `weekly_opening_hours[].intervals[].closes_day_offset` | integer |
| `weekly_opening_hours[].intervals[].opens` | string |
| `weekly_opening_hours[].iso_weekday` | integer |
| `weekly_opening_hours[].raw_hours` | string |
| `weekly_opening_hours[].status` | string |

## Google dispatch result / candidate evidence

Records inspected: 2133. `[]` denotes nested array elements. Fields may be absent in older versions.

| Field path | Observed JSON types |
|---|---|
| `access_state_after_place_resolution` | null, string |
| `attempts` | integer |
| `avg_seconds_per_review` | number |
| `branch_distance_limit_m` | number |
| `browser_lifecycle_scope` | string |
| `browser_startup_seconds` | integer, number |
| `business_status` | null, string |
| `candidate_attempts` | array |
| `candidate_attempts[]` | object |
| `candidate_attempts[].address` | null, string |
| `candidate_attempts[].address_compatibility` | null, number |
| `candidate_attempts[].available_weight` | number |
| `candidate_attempts[].base_score` | number |
| `candidate_attempts[].candidate_id` | string |
| `candidate_attempts[].crawl_eligible` | boolean |
| `candidate_attempts[].distance_m` | number |
| `candidate_attempts[].distinctive_name_supported` | boolean |
| `candidate_attempts[].expected_ward` | string |
| `candidate_attempts[].geographic_score` | integer, number |
| `candidate_attempts[].google_ward` | null, string |
| `candidate_attempts[].house_conflict` | boolean |
| `candidate_attempts[].house_number_left` | null, string |
| `candidate_attempts[].house_number_right` | null, string |
| `candidate_attempts[].house_suffix_compatible_unverified` | boolean |
| `candidate_attempts[].identifier_conflict` | boolean |
| `candidate_attempts[].identity_status` | string |
| `candidate_attempts[].local_address_variant_eligible` | boolean |
| `candidate_attempts[].local_evidence_basis` | null, string |
| `candidate_attempts[].location_conflict` | boolean |
| `candidate_attempts[].name` | string |
| `candidate_attempts[].name_similarity` | number |
| `candidate_attempts[].penalty` | number |
| `candidate_attempts[].raw_name_similarity` | number |
| `candidate_attempts[].reason` | string |
| `candidate_attempts[].rejection_reason` | string |
| `candidate_attempts[].rejection_reasons` | array |
| `candidate_attempts[].rejection_reasons[]` | string |
| `candidate_attempts[].same_street` | boolean |
| `candidate_attempts[].same_ward` | boolean |
| `candidate_attempts[].same_ward_name_eligible` | boolean |
| `candidate_attempts[].score` | number |
| `candidate_attempts[].selected_candidate_id` | string |
| `candidate_attempts[].street_conflict` | boolean |
| `candidate_attempts[].unknown_signals` | array |
| `candidate_attempts[].unknown_signals[]` | string |
| `candidate_attempts[].url` | string |
| `candidate_attempts[].ward_conflict` | boolean |
| `candidate_rankings` | array |
| `candidate_rankings[]` | object |
| `candidate_rankings[].address` | null, string |
| `candidate_rankings[].address_compatibility` | null, number |
| `candidate_rankings[].available_weight` | number |
| `candidate_rankings[].base_score` | number |
| `candidate_rankings[].candidate_id` | string |
| `candidate_rankings[].crawl_eligible` | boolean |
| `candidate_rankings[].distance_m` | number |
| `candidate_rankings[].distinctive_name_supported` | boolean |
| `candidate_rankings[].expected_ward` | string |
| `candidate_rankings[].geographic_score` | integer, number |
| `candidate_rankings[].google_ward` | null, string |
| `candidate_rankings[].house_conflict` | boolean |
| `candidate_rankings[].house_number_left` | null, string |
| `candidate_rankings[].house_number_right` | null, string |
| `candidate_rankings[].house_suffix_compatible_unverified` | boolean |
| `candidate_rankings[].identity_status` | string |
| `candidate_rankings[].local_address_variant_eligible` | boolean |
| `candidate_rankings[].local_evidence_basis` | null, string |
| `candidate_rankings[].location_conflict` | boolean |
| `candidate_rankings[].name` | string |
| `candidate_rankings[].name_similarity` | null, number |
| `candidate_rankings[].penalty` | number |
| `candidate_rankings[].rank` | integer |
| `candidate_rankings[].raw_name_similarity` | null, number |
| `candidate_rankings[].reason` | string |
| `candidate_rankings[].rejection_reasons` | array |
| `candidate_rankings[].rejection_reasons[]` | string |
| `candidate_rankings[].same_street` | boolean |
| `candidate_rankings[].same_ward` | boolean |
| `candidate_rankings[].same_ward_name_eligible` | boolean |
| `candidate_rankings[].score` | integer, number |
| `candidate_rankings[].street_conflict` | boolean |
| `candidate_rankings[].unknown_signals` | array |
| `candidate_rankings[].unknown_signals[]` | string |
| `candidate_rankings[].url` | string |
| `candidate_rankings[].ward_conflict` | boolean |
| `checkpoint` | string |
| `collected_review_count` | integer |
| `collection_status` | string |
| `configured_idle_scrolls` | integer |
| `configured_max_reviews` | integer |
| `configured_max_scrolls` | integer |
| `coordinate_distance_m` | null, number |
| `coverage` | string |
| `crawl_eligible` | boolean |
| `crawler_exit_code` | integer |
| `disposition` | string |
| `dlq_path` | string |
| `elapsed_seconds` | number |
| `expected_address` | null, string |
| `expected_latitude` | number |
| `expected_longitude` | number |
| `expected_ward` | null |
| `failure_category` | null, string |
| `failure_reason_code` | null, string |
| `failure_stage` | null, string |
| `final_access_state` | null, string |
| `finished_at` | string |
| `geographic_context` | string |
| `google_place_details_error` | null |
| `google_place_details_field_count` | integer |
| `google_place_details_path` | null, string |
| `id` | string |
| `identity_status` | string |
| `initial_url` | string |
| `local_missing_review_tab` | boolean |
| `local_navigation_failure` | null |
| `local_review_tab_click_intercepted` | boolean |
| `local_store_failure` | boolean |
| `name` | string |
| `navigation_state` | string |
| `outcome` | string |
| `place_entity_confirmed` | boolean |
| `place_key` | string |
| `query` | string |
| `reconciliation` | object |
| `reconciliation.duplicate_events` | integer |
| `reconciliation.parquet_bytes` | integer |
| `reconciliation.parquet_files` | integer |
| `reconciliation.parquet_rows` | integer |
| `reconciliation.pending` | integer |
| `reconciliation.sqlite_records` | integer |
| `resolution_outcome` | string |
| `resolution_reason` | string |
| `resolved_place_address` | null, string |
| `resolved_place_title` | null, string |
| `resolved_to_reviews_ms` | null, number |
| `resolved_url` | null, string |
| `retailer` | null |
| `review_collection_reason` | null, string |
| `review_collection_status` | null, string |
| `review_source` | string |
| `reviews_per_minute` | number |
| `reviews_persisted` | integer |
| `reviews_seen` | integer |
| `reviews_to_classification_ms` | null, number |
| `runner_error` | null |
| `scroll_count` | integer |
| `search_attempted` | boolean |
| `search_candidate_count` | integer |
| `search_eligible` | boolean |
| `search_mode` | string |
| `search_reason` | null |
| `search_scroll_count` | integer |
| `search_to_resolved_ms` | null, number |
| `security_restriction` | boolean |
| `selected_candidate` | null, object |
| `selected_candidate.address` | null, string |
| `selected_candidate.address_compatibility` | null, number |
| `selected_candidate.available_weight` | number |
| `selected_candidate.base_score` | number |
| `selected_candidate.candidate_id` | string |
| `selected_candidate.crawl_eligible` | boolean |
| `selected_candidate.distance_m` | number |
| `selected_candidate.distinctive_name_supported` | boolean |
| `selected_candidate.expected_ward` | string |
| `selected_candidate.geographic_score` | integer, number |
| `selected_candidate.google_ward` | null, string |
| `selected_candidate.house_conflict` | boolean |
| `selected_candidate.house_number_left` | null, string |
| `selected_candidate.house_number_right` | null, string |
| `selected_candidate.house_suffix_compatible_unverified` | boolean |
| `selected_candidate.identity_status` | string |
| `selected_candidate.local_address_variant_eligible` | boolean |
| `selected_candidate.local_evidence_basis` | null, string |
| `selected_candidate.location_conflict` | boolean |
| `selected_candidate.name` | string |
| `selected_candidate.name_similarity` | number |
| `selected_candidate.penalty` | number |
| `selected_candidate.rank` | integer |
| `selected_candidate.raw_name_similarity` | number |
| `selected_candidate.reason` | string |
| `selected_candidate.rejection_reasons` | array |
| `selected_candidate.rejection_reasons[]` | string |
| `selected_candidate.same_street` | boolean |
| `selected_candidate.same_ward` | boolean |
| `selected_candidate.same_ward_name_eligible` | boolean |
| `selected_candidate.score` | number |
| `selected_candidate.score_crawl_eligible` | boolean |
| `selected_candidate.selection_method` | string |
| `selected_candidate.street_conflict` | boolean |
| `selected_candidate.text` | string |
| `selected_candidate.unknown_signals` | array |
| `selected_candidate.unknown_signals[]` | string |
| `selected_candidate.url` | string |
| `selected_candidate.ward_conflict` | boolean |
| `selection_policy_version` | string |
| `selection_score` | null, number |
| `source_address_missing` | boolean |
| `started_at` | string |
| `status` | string |
| `stop_reason` | null, string |
| `store_elapsed_minutes` | number |
| `store_elapsed_seconds` | number |
| `store_finished_at` | string |
| `store_started_at` | string |
| `summary_path` | string |
| `verification_reason` | string |
| `verification_status` | string |
| `warmup_to_search_ms` | null, number |

## Google dispatch source payload

Records inspected: 2565. `[]` denotes nested array elements. Fields may be absent in older versions.

| Field path | Observed JSON types |
|---|---|
| `address` | null, string |
| `admin_area_name` | string |
| `category_hierarchy` | array |
| `category_hierarchy[]` | string |
| `dq_flags` | array |
| `dq_flags[]` | string |
| `geography_boundary_sha256` | string |
| `geography_inference_basis` | string |
| `identity_status` | string |
| `lat` | number |
| `lng` | number |
| `name` | string |
| `place_key` | string |
| `search_eligible` | boolean |
| `search_mode` | string |
| `search_reason` | null |
| `source` | string |
| `source_address_missing` | boolean |
| `source_manifest_sha256` | string |
| `source_release` | string |

## Google saved input.json

Records inspected: 1. `[]` denotes nested array elements. Fields may be absent in older versions.

| Field path | Observed JSON types |
|---|---|
| `overture` | object |
| `overture.address` | null |
| `overture.admin_area_name` | string |
| `overture.category_hierarchy` | array |
| `overture.category_hierarchy[]` | string |
| `overture.dq_flags` | array |
| `overture.dq_flags[]` | string |
| `overture.geography_boundary_sha256` | string |
| `overture.geography_inference_basis` | string |
| `overture.identity_status` | string |
| `overture.lat` | number |
| `overture.lng` | number |
| `overture.name` | string |
| `overture.place_key` | string |
| `overture.search_eligible` | boolean |
| `overture.search_mode` | string |
| `overture.search_reason` | null |
| `overture.source` | string |
| `overture.source_address_missing` | boolean |
| `overture.source_release` | string |
| `search_plan` | object |
| `search_plan.dq_flags` | array |
| `search_plan.dq_flags[]` | string |
| `search_plan.eligible` | boolean |
| `search_plan.geographic_context` | string |
| `search_plan.latitude` | number |
| `search_plan.longitude` | number |
| `search_plan.mode` | string |
| `search_plan.query` | string |
| `search_plan.reason` | null |
| `search_plan.ward` | null |
| `source_record` | object |
| `source_record.address` | null |
| `source_record.admin_area_name` | string |
| `source_record.category_hierarchy` | array |
| `source_record.category_hierarchy[]` | string |
| `source_record.dq_flags` | array |
| `source_record.dq_flags[]` | string |
| `source_record.geography_boundary_sha256` | string |
| `source_record.geography_inference_basis` | string |
| `source_record.identity_status` | string |
| `source_record.lat` | number |
| `source_record.lng` | number |
| `source_record.name` | string |
| `source_record.place_key` | string |
| `source_record.search_eligible` | boolean |
| `source_record.search_mode` | string |
| `source_record.search_reason` | null |
| `source_record.source` | string |
| `source_record.source_address_missing` | boolean |
| `source_record.source_release` | string |
| `store` | object |
| `store.branch_distance_limit_m` | number |
| `store.expected_address` | null |
| `store.expected_latitude` | number |
| `store.expected_longitude` | number |
| `store.expected_ward` | null |
| `store.geographic_context` | string |
| `store.id` | string |
| `store.name` | string |
| `store.query` | string |
| `store.retailer` | null |
| `store.search_eligible` | boolean |
| `store.search_reason` | null |

## Google checkpoint JSON

Records inspected: 1. `[]` denotes nested array elements. Fields may be absent in older versions.

| Field path | Observed JSON types |
|---|---|
| `elapsed_seconds` | number |
| `failure_stage` | string |
| `finished_at` | string |
| `last_progress_at` | null |
| `reviews_seen` | integer |
| `scroll_count` | integer |
| `started_at` | string |
| `status` | string |
| `store_id` | string |

## Google DLQ JSONL

Records inspected: 941. `[]` denotes nested array elements. Fields may be absent in older versions.

| Field path | Observed JSON types |
|---|---|
| `attempts` | integer |
| `checkpoint_path` | string |
| `elapsed_seconds` | number |
| `error` | string |
| `failure_category` | string |
| `failure_reason_code` | string |
| `failure_stage` | string |
| `final_status` | string |
| `finished_at` | string |
| `query` | string |
| `retailer` | null |
| `retryable` | boolean |
| `reviews_persisted` | integer |
| `reviews_seen` | integer |
| `run_id` | string |
| `started_at` | string |
| `status` | string |
| `store_id` | string |
| `store_name` | string |

### JSON next-opening/closing contract

`next_opening_time` / `next_closing_time` can be null or an object from the current extractor with `local_time: string`, `day_label: string|null`, `at: null`, `raw_label: string`. No date/timezone is fabricated. Other all-null fields such as menu_url/google_place_id can hold strings when actually extracted.

## Google dispatch

Representative DB: `D:\Projects\integration-recomentdation-fnb-data-platform-20261028-pipeline\output\google-m2-remaining-core-v5-four\dispatch.sqlite3`

### dispatch

```sql
CREATE TABLE dispatch (id INTEGER PRIMARY KEY, place_key TEXT UNIQUE NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL, owner TEXT, result TEXT);
```

## Google job review state

Representative DB: `D:\Projects\integration-recomentdation-fnb-data-platform-20261028-pipeline\output\google-m2-remaining-core-v5-four\worker-01\job-00\reviews.sqlite3`

### review_state

```sql
CREATE TABLE review_state(
                source TEXT NOT NULL,
                source_review_id TEXT NOT NULL,
                store_id TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                first_seen_run_id TEXT NOT NULL,
                last_seen_run_id TEXT NOT NULL, miss_count INTEGER NOT NULL DEFAULT 0, is_active INTEGER NOT NULL DEFAULT 1, published_at TEXT, last_modified_at TEXT, timestamp_source TEXT, timestamp_status TEXT NOT NULL DEFAULT 'MISSING', change_diagnostics TEXT NOT NULL DEFAULT '[]',
                UNIQUE(source, source_review_id)
            );
```

### pending_review_events

```sql
CREATE TABLE pending_review_events(
                event_id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                source_review_id TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
```

### checkpoint_metadata

```sql
CREATE TABLE checkpoint_metadata(
                run_id TEXT NOT NULL,
                store_id TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(run_id, store_id)
            );
```

### place_incremental_state

```sql
CREATE TABLE place_incremental_state(
                place_id TEXT PRIMARY KEY,
                last_successful_incremental_at TEXT,
                newest_review_id_seen TEXT,
                newest_review_time_seen TEXT,
                last_incremental_run_id TEXT
            );
```

### place_crawl_access_state

```sql
CREATE TABLE place_crawl_access_state (
            place_id TEXT PRIMARY KEY, last_access_state TEXT,
            last_attempt_at TEXT, last_successful_crawl_at TEXT,
            consecutive_limited_count INTEGER NOT NULL DEFAULT 0,
            consecutive_unknown_count INTEGER NOT NULL DEFAULT 0,
            consecutive_runtime_error_count INTEGER NOT NULL DEFAULT 0,
            next_eligible_at TEXT);
```

## Ngữ nghĩa khi clean/dedup

- source_operating_status là quan sát Overture; verified_business_status không tự biến thành xác minh ngoài đời.
- business_status Google có OPERATIONAL, CLOSED_TEMPORARILY, CLOSED_PERMANENTLY, UNKNOWN; open_now là trạng thái tại thời điểm quan sát. UNKNOWN hours/status không có nghĩa đóng cửa.
- raw_status_labels và extraction_evidence giữ lý do đọc trạng thái, gồm nhãn “Bị đóng vĩnh viễn”.
- rating/review_count của metadata là số Google hiển thị; collected_review_count là số đã thu theo giới hạn. Hai số này không tương đương.
- Review text có thể NULL/không có text; timestamp_status MISSING/PARSE_FAILED/AMBIGUOUS không được thay bằng thời gian observed_at.
- metadata/weekly hours có thể thiếu; extraction success không đảm bảo đầy đủ. Một số field ở bản cũ có thể không tồn tại.
- candidate_rankings/selected_candidate là bằng chứng collection, không phải accepted ER links.
- Spatial pairs vẫn là artifact pre-dedup hai tín hiệu hiện đã lưu: likely_duplicate/evidence/reason. Không giả rằng đã có schema decision ba trạng thái hay weighted rules mới được triển khai.
- Missing-from-release, UNRESOLVED, failed extraction và chưa crawl là các tình trạng khác nhau; không suy ra business closure.
- Chưa có Gold, fact_review hợp nhất, canonical_store_id đáng tin cậy, sentiment, interaction hay recommendation features được tạo bởi inventory này.

Machine-readable inventory: `output/current-data-schema/inventory.json`. Schema inventory không chứa giá trị review/phone/profile; chỉ field/type/path và SQL DDL.
