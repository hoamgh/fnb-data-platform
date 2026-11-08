# Ghi chú mở rộng từ Khánh Hội ra toàn TP.HCM

Đây là ghi chú cho lần mở rộng sau, chưa phải cấu hình đã triển khai.
Hiện dữ liệu và các kiểm tra M1 vẫn được pin cho tập Khánh Hội 725 candidates.

## Cách làm đã thống nhất

- Overture: truy vấn POI theo bbox bao TP.HCM; nếu cần đúng ranh giới thành phố,
  lọc tiếp bằng polygon TP.HCM. Không truyền riêng chuỗi `TP.HCM` cho truy vấn
  không gian. Chỉ bbox thì có thể lấy dư ngoài thành phố. Chọn và lưu rõ phiên
  bản/phạm vi hành chính của ranh giới trước khi chạy.
- Google: tên gần trùng và địa chỉ Google thuộc phạm vi TP.HCM là điều kiện
  thu thập dự kiến. Không bắt phường Google phải trùng phường Overture.
- Lấy phường/thành phố từ địa chỉ Google đã quan sát; địa chỉ có rõ phường thì
  ghi nhận luôn. Không cần tính polygon Google cho bước phân loại này.
- Card search có thể thiếu địa chỉ: mở trang chi tiết của candidate tên phù hợp
  để đọc metadata và xác định phạm vi trước khi lấy reviews. Thiếu thông tin
  không đồng nghĩa ngoài phạm vi; ghi UNKNOWN/UNRESOLVED và lý do, không bịa phường.
- Không coi chữ `HCM` trong query hoặc context nguồn là bằng chứng địa chỉ Google
  thuộc TP.HCM. Lưu giá trị địa chỉ Google thực tế và bằng chứng nhận diện phạm vi.
- Tọa độ/địa chỉ Overture có thể sai: không dùng chúng làm điều kiện loại cứng
  candidate có tên phù hợp và địa chỉ Google thuộc phạm vi đang crawl.
- Giữ raw Overture và Google riêng; vẫn lưu tọa độ Google nếu có để dùng sau.
  Candidate thu thập vẫn là `IDENTITY_UNVERIFIED`; không merge tự động. Nhiều
  chi nhánh ngang mức phù hợp thì không ép chọn.

## Những chỗ cần chỉnh khi triển khai

| Chỗ hiện tại | Việc cần làm cho phạm vi TP.HCM |
|---|---|
| `experiments/overture_incremental/run.py`: `OLD`, `NEW`, `WARD`, `DEST`, `BUFFER_DEGREES`, `main()` | Hiện là benchmark offline trên cache Khánh Hội, không phải downloader TP.HCM. Chuẩn bị extract Overture có phạm vi đủ rộng; tham số hóa bbox, boundary và output. Kiểm tra bbox hiện pin trong `main()` và mô tả scope trong manifest. |
| `config/admin_areas/phuong_khanh_hoi.geojson` | Giữ nguyên file hiện tại. Thêm ranh giới TP.HCM cùng metadata nguồn, phiên bản, CRS và checksum riêng nếu cần lọc chính xác. |
| `experiments/overture_silver/consumer.py`: `WARD_PATH`, `WARD_METADATA`, xử lý địa lý và manifest | Cho phép nhận boundary/scope mới; không thay file polygon cũ hoặc sửa checksum để hợp thức hóa dataset khác. |
| `experiments/overture_gold_contract/m242.py`: `analyze(..., boundary_path=...)` | Tái sử dụng rule taxonomy/eligibility; truyền phạm vi mới khi đánh giá, giữ nguyên lý do chọn candidate. |
| `experiments/overture_gold_contract/eligibility.py`: `BASELINE`, `SOURCE_DIR`, `SILVER_ROOT`, `HIST`, `REFERENCE`, `BOUNDARY`, `validate_inputs()` | Các đường dẫn, checksum và số 6.199 / 557 + 168 = 725 là baseline Khánh Hội. Tạo baseline/manifest phiên bản riêng cho TP.HCM; không yêu cầu dataset TP.HCM phải có 725 hàng. Không thay logic taxonomy chỉ để mở rộng địa lý. |
| `crawl_experiment/sources/google_maps/poi_input.py`: `search_plan()` | Nhận context TP.HCM từ input khi crawl toàn thành phố; không hardcode Khánh Hội hoặc dùng context làm địa chỉ thật. |
| `crawl_experiment/core/models.py`: `Store`; `scripts/google_live_workers.py`: `store_for()` | Hiện truyền `geographic_context` và `expected_ward`. Khi triển khai city scope, thêm/truyền cấu hình phạm vi rõ ràng; không giả TP.HCM là một phường. |
| `crawl_experiment/sources/google_maps/candidate_ranking.py`: `ward_evidence()`, `business_name_evidence()`, `evaluate()` | Hiện route rộng là tên gần trùng + cùng phường. Cần thêm chế độ phạm vi thành phố và bằng chứng thành phố từ địa chỉ Google; trong chế độ đó, khác phường nguồn không phải veto. Tên riêng vẫn cần phù hợp. |
| `crawl_experiment/sources/google_maps/navigator.py`: `open_store()`, `_best_exact_candidate()`, `_resolved_place()` | Kiểm tra cả gate card search và gate trang chi tiết đều dùng scope thành phố. Cho candidate tên phù hợp nhưng thiếu địa chỉ card được mở để đọc địa chỉ chi tiết; không bị ward gate cũ chặn trước. Giữ chọn tối đa một candidate và lưu audit. |
| `scripts/google_m2_batch.py`; `scripts/google_live_workers.py`; file catalog/manifest của lần chạy | Chuẩn bị catalog thành phố mới, kiểm tra reconcile toàn bộ ID; không tái dùng catalog 725 hoặc queue của lượt cũ. Dùng checkpoint/persistence hiện có, không xóa kết quả đã thu. |

**Chưa có CLI `--scope city` hay `--city TP.HCM` được triển khai.** Chỉ đổi
`admin_area_name` hoặc query sang TP.HCM chưa đủ: gate hiện tại vẫn xét phường.

## Giữ nguyên khi mở rộng

- Bốn profile/worker hiện có nếu phù hợp tài nguyên; không nhân bản một POI cho
  nhiều worker. Chỉ chạy khi các profile đã được lượt trước giải phóng.
- Search: tối đa 5 scroll, dừng sau 2 scroll không có candidate ID mới.
- Reviews: tối đa 5 scroll, 50 reviews; dừng sau 2 scroll không có review ID mới.
- Review tab, newest sort, SQLite/Parquet và checkpoint: tái sử dụng phần đang chạy.
- `Bị đóng vĩnh viễn` → `CLOSED_PERMANENTLY`; lấy được reviews thì lưu, không lấy
  được vì review UI cục bộ thì giữ metadata và tính hoàn thành phần thu thập.
- Giữ các hạn chế Google Maps về truy cập, lưu trữ, tái sử dụng; không vượt
  auth/challenge/rate limit. Phân biệt DLQ thật với UNRESOLVED và pending.

Trước khi chạy lớn: test offline cùng tên/khác phường trong TP.HCM, ngoài TP.HCM,
thiếu địa chỉ card nhưng có địa chỉ chi tiết, thiếu địa chỉ chi tiết, nhiều chi nhánh,
và giữ NULL/provenance. Sau đó pilot nhỏ có profile, đọc kết quả rồi mới chạy rộng.
Ghi chú này không tự khởi chạy hay phê duyệt một lượt crawl toàn thành phố.
