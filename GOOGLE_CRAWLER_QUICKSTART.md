# Chạy Google Maps crawler (Windows PowerShell)

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
  --profile-dir 'output/google-crawler-login' `
  --profile-dir 'output/google-crawler-login-02' `
  --profile-dir 'output/google-crawler-login-03' `
  --profile-dir 'output/google-crawler-login-04'
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
- `--max-scrolls 5` là **giới hạn mỗi quán**, không bảo đảm lấy hết review; có thể tăng khi đã kiểm tra kết quả mẫu. `--limit 4` là số quán thử, `--workers 4` là số worker đồng thời.
- Ví dụ đã chạy ngày **03/11/2026**: `output/g-20261103-221019/summary.json` có **4 browser, 69 review lưu Parquet, 1 FULL, 1 PARTIAL, 2 UNKNOWN** (`acceptance_pass=false`). Hai quán UNKNOWN chưa khớp chính xác với kết quả Google Maps.

**Trước khi chạy nhiều quán:** kiểm tra 4-quán smoke và các trường hợp `UNKNOWN`/`PARTIAL`; sau đó mới cân nhắc tăng `--limit` và `--total-seconds` cùng giới hạn phù hợp. Không dùng kết quả `FINISHED` làm bằng chứng đã thu thập đủ 725 quán.
