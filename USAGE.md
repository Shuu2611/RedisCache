# Hướng dẫn sử dụng

Tài liệu ngắn giúp bạn chạy benchmark và proxy trong project.

## Yêu cầu
- Windows (hướng dẫn dùng PowerShell)
- Python 3.8+
- Redis chạy trên máy (mặc định port 6379)
- MongoDB chạy trên máy (mặc định port 27017)
- Java (để chạy YCSB)
- (Tùy chọn) Git và GitHub CLI `gh`

## Cài đặt môi trường Python
1. Tạo và kích hoạt virtualenv (nếu chưa có):

```powershell
python -m venv .venv
& .\.venv\Scripts\Activate.ps1
```

2. Cài phụ thuộc:

```powershell
pip install -r requirements.txt
```

## Khởi động dịch vụ cần thiết
- Khởi Redis và MongoDB theo cách bạn thường dùng (dịch vụ Windows, Docker, hoặc cài đặt cục bộ).
- Kiểm tra trạng thái:

```powershell
redis-cli ping
# mongosh --eval "db.adminCommand('ping')"
```

## Chạy benchmark (bước chính)
1. Nạp dữ liệu YCSB vào MongoDB:

```powershell
python .\benchmark.py
```

Lệnh trên sẽ kiểm tra dịch vụ, nạp dữ liệu (YCSB -> MongoDB), khởi proxy (port mặc định 6380), chạy YCSB client và sinh báo cáo.

2. Sau khi chạy hoàn tất, báo cáo sẽ được lưu trong thư mục `result/report/` và log trong `result/log/`.

## Các tệp và script hữu ích
- `benchmark.py` — kịch bản chạy toàn bộ benchmark, nạp dữ liệu và khởi proxy.
- `adaptive_partition.py`, `tier_manager.py`, `promotion.py` — mã xử lý logic cache và phân tầng.
- `result/log/` — chứa log runtime.
- `result/report/` — chứa file báo cáo kết quả benchmark.

## Git & Remote
Nếu bạn muốn đẩy project lên GitHub:

```powershell
git init
git add .
git commit -m "Initial commit"
git remote add origin https://github.com/Shuu2611/RedisCache.git
git branch -M main
git push -u origin main
```

## Gợi ý khi gặp lỗi
- Kiểm tra Redis/MongoDB đang chạy.
- Xem log trong `result/log/` để xác định vấn đề.
- Đảm bảo `Java` và `YCSB` đã cài và có thể chạy từ command line.

## Muốn tùy chỉnh
- Thay đổi cấu hình bộ nhớ, kiến trúc, hay tham số trong `benchmark.py` hoặc các file cấu hình liên quan.

---
File này được tạo tự động. Nếu bạn muốn mở rộng (thêm phần demo, Docker, hoặc hướng dẫn deploy), mình có thể cập nhật tiếp.
 
## Pull & Về bản trước
Các lệnh cơ bản để cập nhật repository local và cách "về" một commit trước:

- Cập nhật nhánh hiện tại từ remote (an toàn):

```powershell
git pull origin main
```

- Chỉ tải về thay đổi từ remote mà không merge tự động:

```powershell
git fetch origin
```

- Xem lịch sử commit để chọn commit muốn quay về:

```powershell
git log --oneline --graph --decorate -n 50
```

- Checkout tạm (detached HEAD) để kiểm tra nội dung commit:

```powershell
git checkout <commit-hash>
# thử chạy, kiểm tra; quay trở lại main khi xong:
git checkout main
```

- Tạo branch mới từ commit cũ (an toàn, không thay đổi main):

```powershell
git checkout -b restore-previous <commit-hash>
```

- Đặt `main` trở về commit cũ (thay đổi lịch sử; cẩn trọng nếu repo đã chia sẻ):

```powershell
git checkout main
git reset --hard <commit-hash>
git push --force origin main
```

	Cảnh báo: `--force` sẽ ghi đè remote; thông báo cho đồng đội trước khi dùng.

- Hoàn tác bằng cách tạo commit đảo ngược (an toàn khi đã push và chia sẻ):

```powershell
git revert <commit-hash>
git push origin main
```

Nếu bạn muốn mình thực hiện thao tác (ví dụ: tạo branch từ commit cũ hoặc reset `main`), gửi cho mình `commit hash` hoặc xác nhận hành động bạn muốn.