# M2 — Contract bắt buộc: ER giải thích được và truy vết được

Trạng thái: yêu cầu đã chốt cho lần triển khai M2. Tài liệu này chưa triển khai
rule ER, chưa tạo quyết định mới hoặc review queue, chưa sửa artifact M1.
Phạm vi M2 là offline từ dữ liệu hiện có; không crawl lại, không sửa raw,
không thêm service/database/workflow.

## 1. DQ flag tách biệt với ER decision

- DQ flag mô tả dữ liệu thiếu, invalid, mâu thuẫn hoặc provenance cần kiểm tra.
- ER decision mô tả quan hệ giữa một Overture POI và một Google candidate:
  `MATCH`, `NON_MATCH`, `REVIEW_REQUIRED`, `ER_PENDING`.
- Có DQ flag không tự động thành NON_MATCH; không có flag không tự động thành MATCH.
- NULL/UNKNOWN/field vắng mặt là thiếu bằng chứng, không phải bằng chứng mâu thuẫn.
- Trùng Google ID, gần tọa độ hoặc score crawler cao không đủ để tự MATCH/merge.
  Google technical identity dedup ở M1 không phải business/entity dedup ở M2.
- Rule ER và điều kiện chấp nhận MATCH/NON_MATCH cần được xác định riêng khi triển
  khai; contract này không tự đặt ngưỡng, nhãn hay quy tắc quyết định mới.

## 2. Contract mỗi flag

Mỗi flag cần liên kết với composite key nguồn/candidate và có đầy đủ:

| Field | Yêu cầu |
|---|---|
| `flag_code` | Mã ổn định, ưu tiên ánh xạ/tái sử dụng issue_code/dq_flags của M1. |
| `reason` | Mô tả giá trị cụ thể thiếu hoặc không tương thích; không dùng “Suspicious link” hay chỉ lặp flag_code. |
| `evidence_json` | Các giá trị thực tế được đối chiếu, nguồn của từng giá trị, quy tắc chuẩn hóa và kết quả kiểm tra. |
| `observation_ref` | Trỏ được tới observation/file hoặc dòng dispatch cụ thể; đủ định danh nếu có nhiều observation. |
| `rule_version` | Phiên bản rule tạo flag, liên kết được với code/input manifest tương ứng. |
| `recommended_action` | Hành động cụ thể cần kiểm tra bằng dữ liệu hiện có; không tự crawl, tự merge hay mặc định reviewer xác nhận MATCH. |

Không phát flag thiếu reason/evidence/provenance mà vẫn coi là đã giải thích.
Nếu bằng chứng nguồn không truy cập/đọc được, cần nêu rõ ref đó và phần thiếu;
không điền giá trị phỏng đoán hoặc dùng dữ liệu mới để giả làm observation cũ.

## 3. Nội dung bằng chứng

Theo rule đang đánh giá, evidence_json phải giữ dữ liệu đối chiếu thật:

- ID Overture; Google ID kèm kind; candidate liên quan và các observation refs.
- Tên raw và normalized của hai phía.
- Địa chỉ raw và normalized của hai phía; số nhà/tên đường đã parse nếu có.
- Tọa độ từng nguồn, khoảng cách nếu đã có bằng chứng tính toán; nêu rõ nguồn và
  cách tính. Không cần tính khoảng cách cho rule không dùng khoảng cách, không
  bịa khoảng cách khi thiếu tọa độ; ghi NULL và lý do thiếu.
- Score gốc và selection policy nếu dùng làm bằng chứng phụ; không diễn giải
  score crawler như xác suất hoặc quyết định ER đã xác minh.
- Thời điểm quan sát của từng field, hash/path snapshot và phiên bản chuẩn hóa.
- Kết quả kiểm tra và thông tin nào còn thiếu. Nêu rõ một field lấy từ snapshot
  cũ qua fallback thay vì từ observation mới nhất.

Reason phải được tạo từ chính các giá trị và kiểm tra trong evidence_json để
reviewer có thể xác minh lại. Không đưa tên/địa chỉ minh họa vào bằng chứng thật.

## 4. Tái sử dụng M1, không sửa lịch sử

Nguồn hiện có:

- `silver/google_places.parquet`: display/normalized fields,
  `field_provenance_json`, `conflicting_values_json`, `metadata_path`,
  `metadata_sha256`, `current_observation_id`, `observed_at` và `dq_flags`.
- `silver/source_links.parquet`: composite key, `candidate_evidence_json`,
  `selection_evidence_json`, `opened_evidence_json`, `source_observation_refs`,
  `selection_policies`, original crawler labels và cờ collision/ambiguity.
- `reports/google_silver_dq_details.csv`: `issue_code`, `overture_id`, Google key,
  `observation_ref`, `field`, `detail_json`.
- Existing Overture Silver và hash input trong manifest M1 để đọc đúng thông tin
  phía Overture; không rebuild hoặc ghi đè dataset đó.

**DQ M1 hiện chưa có đầy đủ contract reason/recommended_action của M2.** Khi
triển khai M2, dùng adapter nhỏ để bổ sung evidence/reason từ các observation
được tham chiếu. Giữ mã/cờ và provenance M1; không sửa release M1 hoặc chỉ đổi
nhãn một thông báo chung thành lời giải thích đầy đủ.

| Cờ M1 | Bằng chứng/lý do cần bổ sung ở M2 |
|---|---|
| `GOOGLE_ID_SHARED_BY_OVERTURE_IDS` | ID Google và danh sách ID/tên/địa chỉ Overture liên quan; nêu đây là nhiều nguồn chọn cùng candidate, chưa chứng minh các nguồn trùng thực thể. |
| `OVERTURE_MULTIPLE_GOOGLE_CANDIDATES` | Một ID Overture, mọi Google ID/tên/địa chỉ, run/time và selection policy liên quan; nêu rõ candidate khác nhau ở field nào. |
| `MISSING_METADATA` / `MISSING_CURRENT_FIELD` | Liệt kê metadata/field thiếu và observation đã kiểm tra; không diễn giải NULL như một địa chỉ khác hoặc quán đóng cửa. |
| `HOUSE_CONFLICT` / `STREET_CONFLICT` | Giá trị raw và kết quả parse từng phía, độ rõ của parser và các field còn thiếu; không chỉ sao chép boolean từ crawler. |
| `OPENED_CANDIDATE_NOT_CONFIRMED_FOR_COLLECTION` | Candidate đã mở, observation/outcome/reason cụ thể và metadata còn thiếu; không coi lỗi UI hay collection là NON_MATCH. |
| `CONFLICTING_METADATA` | Từng giá trị không NULL, snapshot/time/hash và khác biệt normalized thực sự; tách thay đổi theo thời gian khỏi mâu thuẫn chưa giải thích. |
| `INTERRUPTED_UNKNOWN` | Run/job/checkpoint và trạng thái đã ghi; chưa biết kết quả không đồng nghĩa chưa tìm thấy hay không tồn tại quán. |

## 5. Quyết định ER

Mọi quyết định cần composite key nguồn/candidate, status, reason, evidence_json,
observation refs và rule_version; đủ dấu vết để chạy lại kiểm tra. Quyết định
review thủ công cần giữ người/nguồn quyết định và observation đã được review.

Không nâng crawler VERIFIED/RESOLVED hoặc candidate eligibility thành MATCH.
Không tự thay quyết định đang có khi thiếu bằng chứng mới đủ điều kiện. Giữ
lịch sử quyết định thay vì ghi đè mất bằng chứng cũ. M2 không tự merge POI hay
gán lại reviews chỉ vì một quyết định/cờ mới xuất hiện.

## 6. `review_queue.csv`

Khi triển khai M2, xuất queue để review trực tiếp, một dòng cho một flag của
quan hệ cần kiểm tra, với key rõ để nhóm các flag cùng quan hệ. Tên và địa chỉ
đặt trước, provenance và JSON bằng chứng đặt sau. Các cột tối thiểu:

```text
overture_name, google_name, overture_address, google_address,
er_status, flag_code, reason, recommended_action,
overture_id, google_id_kind, google_identity_value,
evidence_json, observation_ref, rule_version
```

Các tên/địa chỉ trong queue phải khớp observation được dẫn, không trộn tùy ý
giá trị aggregate từ snapshot khác. Giữ NULL/thiếu rõ ràng và escape CSV/JSON
đúng; có thể đọc bằng DBeaver/Excel mà không mất reason/evidence.

## 7. Tests bắt buộc trước publication M2

- Mỗi flag và decision có đủ field bắt buộc; reason cụ thể, không chỉ là thông
  báo chung hoặc lặp mã flag.
- Các giá trị nêu trong reason khớp evidence và observation thực tế.
- Ref phân giải được tới đúng raw record/snapshot, Google key và source ID;
  phát hiện hash mismatch hoặc ref không đọc được. Không được âm thầm thay ref.
- NULL/UNKNOWN không kích hoạt cờ mâu thuẫn; thiếu số nhà khác số nhà không bằng nhau.
- Trùng Google ID hoặc score cao không tự tạo MATCH/merge.
- Nhiều nguồn chung Google ID và một nguồn có nhiều candidate đều giữ đầy đủ ID.
- review_queue.csv có tên/địa chỉ hai phía, lý do/bằng chứng/hành động, key và
  provenance; kiểm tra round-trip Unicode và JSON/CSV escaping.
- Cùng input + rule_version cho cùng logical output và thứ tự ổn định, kể cả
  đảo thứ tự input; stable ID/tie-breaker không dựa vào wall-clock xử lý.
- Không ghi raw/checkpoint, không crawl, không gán lại reviews; publication lỗi
  không để lộ bundle chưa hoàn tất. Tái sử dụng cơ chế publication versioned của M1.

Không thêm service, database, orchestration framework hoặc quarantine workflow
để đáp ứng contract này; ưu tiên module/CLI và file versioned hiện có.
