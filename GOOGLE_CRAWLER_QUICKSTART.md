# M2 — Google Maps bounded pilot và batch (PowerShell)

**Mở rộng toàn TP.HCM sau này:** xem
[ghi chú phạm vi Overture và Google](docs/TPHCM_CRAWL_EXPANSION_NOTES.md).
Tài liệu chỉ rõ các file/gate cần chỉnh; city scope chưa được triển khai.

**Cập nhật recovery:** xem `docs/GOOGLE_CRAWLER_RECOVERY.md` và pilot thủ công
`output/google-recovery-pilot-v1-input/run.ps1`. Đã phục hồi navigator hooks,
Capture-style fallback và CSS/XPath reviews; nội dung moderate v1 bên dưới là
baseline scoring, có thêm fallback clear-winner được ghi bằng chứng. UNRESOLVED
bình thường không vào DLQ. Không tự chạy lại campaign bị ngắt.

Chạy tại **pipeline worktree**; Overture là POI gốc. Chỉ dùng dữ liệu Google trong giới hạn được phép về truy cập, lưu trữ, tái sử dụng và chia sẻ. Có crawler hoặc quan sát cũ không tự chứng minh quyền dùng; dừng khi gặp sign-in/challenge/rate limit, không vượt hạn chế.

## Chính sách input dùng chung cho mọi nguồn POI

### Mở tab reviews

Ưu tiên tab đang hiển thị có text **Bài đánh giá / Đánh giá / Reviews**, kể cả
không có aria-label. Chờ có giới hạn theo `render_wait` (mặc định 3 giây), chọn
tab trước nút tổng số reviews, không chọn “Viết bài đánh giá”; tab đã selected
không bị click lại. Sau native click mới resolve review pane và sort.
Sort kiểm tra từ/nhãn rõ ràng, không nhận “Assorted …” của ảnh là “Sort”.
Nếu metadata báo review_count > 0 thì thông báo “chưa có bài đánh giá” không
được tự phân loại NO_REVIEWS; ghi conflict/UNKNOWN để kiểm tra thay vì suy ra zero.

### Quán đóng cửa vĩnh viễn

Chỉ khi metadata Google đã lưu có `business_status=CLOSED_PERMANENTLY`:
vẫn thử lấy reviews theo giới hạn bình thường. Nếu reviews khả dụng thì lưu;
nếu thiếu review UI, không sort/parse được hoặc pagination không còn lấy được
reviews thì giữ metadata và review đã thu, đánh dấu `COMPLETE`,
`review_collection_status=UNAVAILABLE_CLOSED_PERMANENTLY` hoặc
`PARTIAL_CLOSED_PERMANENTLY`, disposition `COMPLETED_CLOSED`, coverage `METADATA_ONLY`.
Tính là một POI hoàn thành, không đưa vào DLQ; không khẳng định đã lấy đủ reviews
hoặc quán không có reviews. Chỉ áp dụng lỗi reviews cục bộ; rate-limit/auth/challenge,
lỗi server, browser và persistence vẫn giữ nguyên xử lý lỗi. Không áp dụng tự động
cho quán tạm đóng hoặc không rõ trạng thái. Identity vẫn chưa xác minh cho ER.

### Moderate selection v1 — crawl eligibility khác identity verification

Rule mới xếp hạng tất cả candidate đã thấy trong tối đa **5 search scrolls**,
ngừng sau **2 scroll liên tiếp không có candidate ID mới**, rồi chọn **zero hoặc one**.
Không còn mở lần lượt 3 candidate. ID nguồn Google dùng để theo dõi candidate, không
phải bằng chứng tự gộp POI. Địa chỉ đọc từ parent card; NULL vẫn là UNKNOWN.

`RankingConfig`: name_weight=.45, address_weight=.40, distance_weight=.15,
eligibility_threshold=.65, minimum_name_support=.35, same_street_threshold=.80,
house_conflict_penalty=.30, street_conflict_penalty=.35, distance_scale_m=200.
Score = weighted mean trên các signal có dữ liệu, trừ penalty; không đặt signal
thiếu thành zero. Distance có dữ liệu nhưng xa là zero geographic support.
Cần tên có liên quan và ít nhất địa chỉ tương thích hoặc vị trí gần để tránh name-only.
Đây là ngưỡng screening ban đầu, chưa calibration/precision guarantee.

Chuẩn hóa dấu tiếng Việt, case, punctuation và Đ./Đường/street/st; không sửa raw.
Cùng phố có compatibility .85; cùng số nhà + phố là 1.0. Số nhà rõ ràng khác nhau
trên cùng phố bị trừ .30; phố rõ ràng khác nhau bị trừ .35. Số nhà range/không rõ
không được coi là confirmed house conflict. Trạng thái đóng cửa không loại candidate.
Tọa độ chỉ từ entity !3d/!4d, không dùng camera viewport @lat,lng.

Sau khi mở candidate được chọn, kiểm tra entity thực tế bằng cùng rule và phát hiện
redirect sang ID khác; thất bại thì UNRESOLVED, không tự chọn candidate thứ hai.
Thành công là **crawl_eligible=true, IDENTITY_UNVERIFIED**, không phải accepted ER link.
`place_entity_confirmed` chỉ xác nhận trang entity được mở; không xác nhận cùng thực thể
với Overture. Không dùng field này riêng lẻ để tạo mapping đáng tin cậy.
Metadata/summary giữ ranking, score, unknown signals, penalties, candidate attempts,
policy version và provenance. Legacy resolver không nâng selection mới thành RESOLVED ER.

Search và review scroll độc lập. Review vẫn **5 scroll / 50 reviews / 2 idle scrolls**.
Quyền truy cập/lưu trữ/tái sử dụng và rate-limit/challenge stop không đổi.
Worker đang chạy đã import code cũ không tự nhận bản sửa; chỉ áp dụng ở lượt mới.
Kiểm tra offline thực tế: `output/google-moderate-ranking-offline-v1/real_examples.json`
và `comparison.json`. Negative controls là cross-pair các record thật, không phải
labels ER đã kiểm chứng. Chưa chạy lại DLQ trong thay đổi này.

`poi_input.search_plan` tách điều kiện search khỏi xác minh danh tính, không yêu cầu Overture ID hoặc category/phone/website/rating để admission. Mã nguồn + source ID (hoặc place_key) vẫn phải duy nhất để checkpoint/recovery; dataset đã chọn phạm vi nghiệp vụ ở upstream.

Thứ tự query: tên + địa chỉ + tọa độ; tên + tọa độ + ngữ cảnh nếu thiếu địa chỉ; tên + địa chỉ nếu thiếu tọa độ; tên + ngữ cảnh nếu thiếu cả hai. Không hardcode địa chỉ/ngữ cảnh trong builder. Context suy ra từ polygon chỉ là ngữ cảnh địa lý, không điền vào expected_address. Tọa độ invalid được giữ ở source_record và gắn DQ, không tự coi là vị trí hợp lệ.

Thiếu tên hoặc chỉ có tên mà không có địa chỉ/tọa độ/ngữ cảnh: vẫn giữ record trong plan, ghi UNRESOLVED/INSUFFICIENT_SEARCH_INFORMATION và hoàn tất dispatch mà không mở browser. Không có bằng chứng nguồn đầy đủ không đồng nghĩa WRONG_BRANCH. Google metadata và resolution diagnostics nằm riêng, không sửa NULL nguồn. `input.json` giữ source_record nguyên gốc và search_plan/DQ riêng; ID ổn định khi chạy lại.

VERIFIED chỉ sau xác minh nhánh; REVIEW_REQUIRED = có ứng viên nhưng bằng chứng chưa đủ/mơ hồ; UNRESOLVED = không đủ input hoặc không xác định được nhánh; WRONG_BRANCH = có mâu thuẫn tên/địa chỉ/tọa độ ở entity; FAILED = lỗi thực thi. Các trạng thái này giữ riêng với collection_status và coverage. Rule moderate chỉ cấp crawl eligibility từ các signal có sẵn; mọi selection mới giữ IDENTITY_UNVERIFIED, chờ ER downstream.

## 1. Chuẩn bị offline — đủ 725 ứng viên

```powershell
Set-Location 'D:\Projects\integration-recomentdation-fnb-data-platform-20261028-pipeline'
$python = 'D:\Projects\integration-recomentdation\output\ci01-2ac84b90\venv\Scripts\python.exe'
$campaign = 'output/google-m2-725-v5'
& $python -B -m scripts.google_m2_batch `
  --input-root 'D:/Projects/integration-recomentdation-fnb-data-platform-20261028' `
  --output $campaign --pilot
```

Rebuild từ Silver + M242 với checksum, không dùng catalog cũ có manifest mismatch. Giữ mọi ứng viên, không merge/xóa. `preparation_summary.json` ghi 725 input; **725/725 đủ điều kiện thử tìm kiếm**: 693 dùng tên + địa chỉ + tọa độ + khu vực; 32 thiếu địa chỉ dùng tên + tọa độ + ngữ cảnh Khánh Hội/TP.HCM suy ra từ polygon. Giữ nguyên NULL và DQ flag `SOURCE_ADDRESS_MISSING`; không tự chọn chi nhánh gần nhất hoặc dùng địa chỉ Google để điền ngược làm bằng chứng nguồn.

## 2. Pilot live — tối đa 4 POI (2 thiếu địa chỉ + 2 có địa chỉ), chạy thủ công

Chỉ chạy sau khi đã xác nhận quyền dùng phù hợp và đóng Chrome đang dùng các profile riêng. Dùng trực tiếp profile có sẵn, không cần copy:

```powershell
& $python -B -u -m scripts.google_m2_batch `
  --input-root 'D:/Projects/integration-recomentdation-fnb-data-platform-20261028' `
  --output $campaign --pilot --execute --operator-submit `
  --profile-dir 'D:/Projects/integration-recomentdation-fnb-data-platform-20261028/output/google-crawler-login' `
  --profile-dir 'D:/Projects/integration-recomentdation-fnb-data-platform-20261028/output/google-crawler-login-02' `
  --profile-dir 'D:/Projects/integration-recomentdation-fnb-data-platform-20261028/output/google-crawler-login-03' `
  --profile-dir 'D:/Projects/integration-recomentdation-fnb-data-platform-20261028/output/google-crawler-login-04'
```

Các profile riêng phải tồn tại trong thư mục `output/` của worktree đang dùng hoặc worktree khác; dùng đường dẫn tuyệt đối để tránh nhầm. Runner kiểm tra Chrome đang dùng profile và khóa liên tiến trình theo đúng đường dẫn; không tự copy profile cá nhân hoặc profile đang dùng từ worktree gốc. Có thể bỏ cả bốn `--profile-dir` để dùng session mới theo cơ chế hiện có; không bỏ qua access restrictions.

Giới hạn M2 cố định: **MAX_REVIEW_SCROLLS=5**, **MAX_REVIEWS_PER_STORE=50**, **2 scroll liên tiếp không thấy review ID mới**. Dừng khi chạm bất kỳ giới hạn nào, kể cả DOM đầu tiên đã có hơn 50 review. Metadata và reviews dùng cùng driver/session; chỉ mở review sau candidate selection đạt crawl eligibility và xác nhận trang entity; identity vẫn UNVERIFIED cho ER. Tên/tọa độ chỉ là ngữ cảnh search: ranking dùng tên/địa chỉ/tọa độ có sẵn, trừ penalties và ghi unknown; không tự biến crawl eligibility thành VERIFIED identity. Nếu không có candidate đạt ngưỡng hoặc trang entity không còn đủ điều kiện thì giữ `PLACE_RESOLUTION_FAILED` (campaign `UNRESOLVED`), không lấy reviews. Google address/title quan sát được giữ riêng trong resolution diagnostics, không ghi đè địa chỉ Overture. Đây là IDENTITY_UNVERIFIED, không phải accepted cross-source match hoặc bằng chứng cửa hàng đang hoạt động.

`max_reviews`, `max_scrolls`, `no_new_reviews` được ghi **PARTIAL_LIMIT**, không phải đã lấy toàn bộ review. Review IDs, rating/text, timestamp evidence và provenance giữ theo SQLite/outbox/Parquet hiện có; thiếu timestamp không được tự điền. Xem mỗi store trong `batch-*/worker-*/progress.jsonl`: `scroll_count`, `collected_review_count`, `collection_status`, `verification_status`, `stop_reason`; metadata/checkpoints ở thư mục job.

## 3. Sau pilot đạt — mỗi lệnh chỉ một batch 25–50 POI

```powershell
# Chỉ sau khi pilot_acceptance.json có passed=true và operator đã xem kết quả.
& $python -B -u -m scripts.google_m2_batch `
  --input-root 'D:/Projects/integration-recomentdation-fnb-data-platform-20261028' `
  --output $campaign --batch-size 25 --execute --operator-submit
```

Có thể dùng cùng bốn profile như pilot. Không tự chạy liên tiếp 725. `campaign_progress.json` đối soát một record cho mỗi trong 725 ID: PENDING, CRAWL_ELIGIBLE (identity unverified), VERIFIED (legacy), REVIEW_REQUIRED, UNRESOLVED, WRONG_BRANCH, FAILED hoặc IN_PROGRESS_UNKNOWN; cập nhật sau batch kể cả lỗi. Chạy lại cùng campaign để tiếp tục batch kế tiếp: kiểm tra manifest/input/policy, kiểm tra branch evidence + metadata, reconcile SQLite/outbox/Parquet rồi tái dùng kết quả hoàn thành hợp lệ (kể cả PARTIAL_LIMIT do cap). Không tự nhập các run cũ không có policy/bằng chứng tương ứng. Giữ original job thay vì copy review sang nguồn mới.

RUNNING/unknown/failed không tự replay; `campaign_progress.json` giữ `unknown_no_replay`. Access/session stop hoặc lock còn lại sau hard-kill cần operator xem xét trước khi tiếp tục; không tự xóa lock hoặc vượt access gate. Checkpoints và partial results được giữ nguyên. Campaign thay code/input phải dùng thư mục mới.

```powershell
Get-Content "$campaign/preparation_summary.json"
Get-Content "$campaign/pilot_acceptance.json"
Get-Content "$campaign/last_batch_summary.json"
Get-ChildItem $campaign -Recurse -Filter 'progress.jsonl' | Select-Object FullName
```

Pilot gate chấp nhận xử lý giới hạn đúng hoặc từ chối match thận trọng với zero reviews; không yêu cầu ép cả 4 POI thành VERIFIED. Live pilot chưa chạy trong thay đổi cấu hình này. Pilot offline dùng DOM giả cùng persistence thật; không thay thế xác nhận live.

---

## Tham khảo lệnh benchmark lịch sử (không phải cấu hình M2)

Chạy trong **PowerShell**, tại thư mục dự án. Lệnh dưới đây là mẫu **4 quán / 4 worker**, mỗi worker dùng một Chrome profile riêng. Không cần cài lại thư viện.

```powershell
Set-Location 'D:\Projects\integration-recomentdation-fnb-data-platform-20261028'

$python = 'D:\Projects\integration-recomentdation\output\ci01-2ac84b90\venv\Scripts\python.exe'
$runOutput = 'output/google-khanh-hoi-' + (Get-Date -Format 'yyyyMMdd-HHmmss')

& $python -B -u -m scripts.google_live_workers `
  --operator-submit `
  --catalog 'output/google-khanh-hoi-725/catalog.parquet' `
  --output $runOutput `
  --limit 4 `
  --workers 4 `
  --review-source dom `
  --max-scrolls 5 `
  --per-store-seconds 120 `
  --total-seconds 600 `
  --profile-dir 'D:/Projects/integration-recomentdation-fnb-data-platform-20261028/output/google-crawler-login' `
  --profile-dir 'D:/Projects/integration-recomentdation-fnb-data-platform-20261028/output/google-crawler-login-02' `
  --profile-dir 'D:/Projects/integration-recomentdation-fnb-data-platform-20261028/output/google-crawler-login-03' `
  --profile-dir 'D:/Projects/integration-recomentdation-fnb-data-platform-20261028/output/google-crawler-login-04'
```

**Lưu ý:** Dấu backtick (`` ` ``) phải nằm **cuối dòng**, không có dấu cách theo sau. Giữ 4 profile riêng, không mở đồng thời các phiên crawler dùng chung profile. `$runOutput` tạo thư mục mới mỗi lần; **không dùng lại thư mục output cũ**. Nếu đường dẫn Python thay đổi, sửa biến `$python` cho đúng môi trường hiện có.

## Xem kết quả

```powershell
# Tổng kết phiên chạy
$s = Get-Content "$runOutput/summary.json" -Raw | ConvertFrom-Json
$s | Select-Object status, assigned, completed, workers, full_store_count, partial_store_count, unknown_store_count, parquet_rows, acceptance_pass | Format-List

# Log từng worker (đổi số worker nếu cần)
Get-Content "$runOutput/worker-03/worker.log" -Tail 50

# File review và metadata đã lưu
Get-ChildItem $runOutput -Recurse -Filter '*.parquet' | Select-Object FullName
Get-ChildItem $runOutput -Recurse -Filter '*.place_details.json' | Select-Object FullName
```

- `FULL`: quán đã được xác nhận thu thập đầy đủ theo trạng thái crawler; `PARTIAL`: chỉ lấy được một phần; `UNKNOWN`/`PLACE_RESOLUTION_FAILED`: chưa xác định chắc chắn quán trên Google Maps, **không tự chọn quán gần nhất**.
- `FINISHED` chỉ là đã kết thúc việc chạy. Muốn đánh giá kết quả, xem cả `acceptance_pass`, số review (`parquet_rows`), `full_store_count`, `partial_store_count`, `unknown_store_count`.
- `--max-scrolls 5` là **giới hạn mỗi quán**, không bảo đảm lấy hết review; M2 giữ nguyên 5 scroll, không tự tăng. `--limit 4` là số quán thử, `--workers 4` là số worker đồng thời.
- Ví dụ đã chạy ngày **03/11/2026**: `output/g-20261103-221019/summary.json` có **4 browser, 69 review lưu Parquet, 1 FULL, 1 PARTIAL, 2 UNKNOWN** (`acceptance_pass=false`). Hai quán UNKNOWN chưa khớp chính xác với kết quả Google Maps.

**Trước khi chạy nhiều quán:** kiểm tra 4-quán smoke và các trường hợp `UNKNOWN`/`PARTIAL`; sau đó mới cân nhắc tăng `--limit` và `--total-seconds` cùng giới hạn phù hợp. Không dùng kết quả `FINISHED` làm bằng chứng đã thu thập đủ 725 quán.
