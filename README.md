# 🚀 Tiered Cache System với Adaptive Partitioning

Hệ thống cache phân tầng hiệu năng cao với **SlimCache Adaptive Partitioning Algorithm**, batch promotion/demotion, nén dữ liệu LZ4, và tự động điều chỉnh giới hạn HOT/COLD tier dựa trên cost model.

## 📑 Mục Lục

1. [🎯 Kiến Trúc](#-kiến-trúc)
2. [🔥 Tính Năng Chính](#-tính-năng-chính)
3. [📊 Hiệu Năng (Benchmark Mới Nhất)](#-hiệu-năng-benchmark-mới-nhất)
4. [🚀 Bắt Đầu Nhanh](#-bắt-đầu-nhanh)
5. [⚙️ Cấu Hình](#️-cấu-hình)
6. [📁 Cấu Trúc Dự Án](#-cấu-trúc-dự-án)
7. [🔬 Adaptive Partitioning](#-adaptive-partitioning)
8. [🛠️ Chi Tiết Triển Khai](#️-chi-tiết-triển-khai)
9. [📈 Quy Trình Benchmark](#-quy-trình-benchmark)
10. [🎓 Bài Học Kinh Nghiệm](#-bài-học-kinh-nghiệm)
11. [🔧 Xử Lý Sự Cố](#-xử-lý-sự-cố)
12. [📚 Tài Liệu Tham Khảo](#-tài-liệu-tham-khảo)

---

## 🎯 Kiến Trúc

### 📐 Architecture Diagrams

Hệ thống được mô tả qua 3 diagrams chính:

📊 **[Architecture Overview](docs/architecture_overview.md)** - Tổng quan kiến trúc
- Components và vai trò của từng layer
- Kết nối giữa Client, Proxy, Storage, Compression
- Benchmark results và performance metrics

🔄 **[Data Flow Diagram](docs/data_flow.md)** - Luồng dữ liệu chi tiết
- 3 scenarios: HOT hit, COLD hit, Cache miss
- Background operations: Promotion và Adaptive Partitioning
- Latency breakdown và pipeline optimization

🧠 **[Adaptive Partitioning Flow](docs/adaptive_partitioning_flow.md)** - Thuật toán adaptive
- SlimCache cost model chi tiết
- Step-by-step calculation examples
- Convergence analysis và tuning parameters

### Quy Trình Hoạt Động

**1. Đọc Dữ Liệu:**
- HOT tier → COLD tier (giải nén LZ4) → MongoDB (cache miss)

**2. Ghi Dữ Liệu:**
- Vào tier hiện tại của key (COLD tier được nén tự động)

**3. Thăng Hạng (Promotion):**
- **Real-time**: Score ≥ 20 → promote ngay lập tức
- **Background**: Worker mỗi 60s quét và batch promote

**4. Giáng Hạng (Demotion):**
- Key HOT idle >60s → di chuyển xuống COLD (nén LZ4)

**5. Eviction:**
- HOT đầy → evict LRU key sang COLD (có nén)
- COLD đầy → evict LRU key hoàn toàn (xóa khỏi cache)

## 🔥 Tính Năng Chính

### 1. Quản Lý Tầng Dựa Trên Bộ Nhớ
- **Tầng HOT/COLD**: Tỉ lệ chia theo `HOT_MEMORY_PERCENT` (configurable)
- **LRU eviction**: Tự động loại bỏ khi vượt giới hạn
- **Theo dõi bộ nhớ**: Theo dõi kích thước từng key (ước tính)
- **TTL**: Hiện tại không dùng TTL cho cả HOT và COLD tier (eviction thuần theo memory)

### 2. Thăng/Giáng Hạng Thông Minh
- **Thăng hạng thời gian thực**: COLD hit có `score >= SCORE_THRESHOLD (20)` và HOT còn chỗ → promote ngay
- **Thăng hạng nền**: Worker chạy theo `promotion_interval` và quét score-based để promote batch
- **Giáng hạng tự động**: Key HOT idle >60s → COLD (nén)
- **Nhận biết bộ nhớ**: Thăng hạng bị chặn nếu tầng HOT đầy

### 3. Nén Dữ Liệu
- **LZ4** nén cho tầng COLD (nhanh, khuyến nghị cho production)
- **zlib** cũng được hỗ trợ (nén tốt hơn 55% vs 43%, nhưng chậm hơn 34%)
- **Có thể cấu hình**: Đổi thuật toán qua `COMPRESSION_ALGORITHM` trong benchmark.py
- **Trade-off đã được benchmark**: LZ4 = performance, zlib = storage efficiency

### 4. Tối Ưu Hiệu Năng (Quick Win Package - 18 LOC)
- **Redis Pipelining**: Giảm RTT từ 3→1 cho mỗi read operation (5 LOC)
  - Batch HGETALL + GET thành 1 pipeline
  - Tiết kiệm ~300μs latency per request
- **ThreadPoolExecutor**: Tái sử dụng threads thay vì tạo mới mỗi request (8 LOC)
  - Giảm overhead từ 100-500μs xuống ~10μs
  - Max workers: 16 threads
- **Size Caching**: Cache kết quả ước tính kích thước (5 LOC)
  - Tránh tính toán lặp lại cho cùng 1 key
  - Tiết kiệm ~50-100μs per access
- **Kết quả**: +55.2% throughput, -35.5% latency

### 5. Logging Chi Tiết
- Theo dõi luồng với mũi tên: `user123 -> HOT tier -> cache hit`
- Ghi lại eviction dựa trên bộ nhớ
- Theo dõi sự kiện thăng/giáng hạng

## 📊 Hiệu Năng (Benchmark Mới Nhất)

### Test Configuration
```
Load:         50,000 records (Zipfian s=1.5)
Operations:   200,000 read operations (workload C)
Memory:       200 MB total (30 MB HOT, 170 MB COLD)
Compression:  zlib (level 6)
Adaptive:     Enabled (đánh giá mỗi 3 lần chạy worker, yêu cầu >=1000 requests)
```

### Performance Results
```
Throughput:       1,635 ops/sec
Avg Latency:      2,430 μs
Redis Hit Rate:   77.16%

Execution Time:
  YCSB Load:      7.90s
  YCSB Run:       122.93s
  YCSB Total:     130.83s
  
  Wall-Clock:     145.30s (2.42 mins)
  Overhead:       14.47s (11%)
```

### Tier Distribution
```
HOT tier:      124,635 hits (62.3%) - Uncompressed, fast
COLD tier:     29,676 hits (14.8%) - zlib compressed
MongoDB:       45,689 hits (22.8%) - Fallback storage
```

### Memory Utilization
```
HOT:   32,041 keys, 32.02 MB (100.1% utilized) 
COLD:  13,644 keys, 12.36 MB (7.4% utilized)
Total: 45,685 keys, 44.38 MB used

Distribution: HOT 72.2%, COLD 27.8%
Evictions: 0 (no data loss)
Real-time promotions: 11,749
```

### Compression Comparison
```
**LZ4** (fast, default):
  - Throughput: 2,484 ops/sec
  - Latency: 1,601 μs
  - Compression ratio: ~43%
  
**zlib** (better compression):
  - Throughput: 1,635 ops/sec (-34%)
  - Latency: 2,430 μs (+52%)
  - Compression ratio: ~55%
  
Trade-off: LZ4 recommended for performance, zlib for storage efficiency
```

---

## 🚀 Bắt Đầu Nhanh

### Yêu Cầu

1. **Redis** (port 6379)
   ```bash
   # Khởi động Redis
   redis-server
   
   # Fix Redis MISCONF error (nếu gặp)
   redis-cli CONFIG SET stop-writes-on-bgsave-error no
   ```

2. **MongoDB** (port 27017)
   ```bash
   mongod --dbpath C:\data\db
   ```

3. **Python 3.8+** với dependencies

### Cài Đặt

```bash
# Clone hoặc tải project
cd d:\test2

# Tạo môi trường ảo (khuyến nghị)
python -m venv .venv

# Kích hoạt venv
.venv\Scripts\activate

# Cài đặt dependencies
pip install -r requirements.txt
```

### Chạy Benchmark

```bash
python benchmark.py
```

Quá trình thực thi (mặc định 50k records / 200k operations):
1. Kiểm tra và khởi động Redis + MongoDB (auto-start nếu chưa chạy)
2. Xóa Redis + MongoDB databases
3. Load 50k records vào MongoDB (YCSB: ~7.9s)
4. Khởi động proxy phân tầng trên port 6380
5. Chạy 200k read operations qua proxy (YCSB: ~123s)
   - Zipfian distribution tạo hot keys  
   - Score-based classification (frequency + recency)
   - Real-time và background promotions
6. Background worker thăng/giáng hạng (mỗi 60s)
7. Generate báo cáo chi tiết + thống kê tầng

**Thời gian thực tế**: ~145s wall-clock (131s YCSB + 14s overhead)

**Lưu ý**: Adaptive partitioning đang bật mặc định. Trong benchmark hiện tại, worker chạy mỗi 30s nên adaptive được gọi mỗi 3 runs (xấp xỉ 90s), và chỉ thực thi khi có tối thiểu 1000 requests kể từ lần reset stats gần nhất.

### Vị Trí Kết Quả

- **Logs**: `result/log/tiered_log_YYYYMMDD_HHMMSS.txt` (chi tiết từng operation)
- **Báo cáo**: `result/report/tiered_report_YYYYMMDD_HHMMSS.txt` (summary + metrics)

## ⚙️ Cấu Hình

### Cấu hình cơ bản trong benchmark.py

```python
class TieredCacheBenchmark:
    def __init__(self):
        # Benchmark settings
        self.RECORD_COUNT = 50000            # Records to load (default: 50k)
        self.OPERATION_COUNT = 200000        # Operations to run (default: 200k)  
        self.WORKLOAD_TYPE = 'C'             # YCSB workload (C = read-only)
        
        # Compression algorithm
        self.COMPRESSION_ALGORITHM = 'zlib'  # 'lz4' or 'zlib'
        
        # Memory configuration
        self.HOT_MEMORY_PERCENT = 15         # 15% for HOT tier
        self.TOTAL_MEMORY_MB = 200           # 200MB total Redis memory
```

### Cấu hình nâng cao trong tier_manager.py

```python
class TierManager:
    # Classification thresholds
    SCORE_THRESHOLD = 20      # score >= 20: HOT tier
    
    # Score weights
    WEIGHT_FREQUENCY = 0.8    # Frequency weight (80%)
    WEIGHT_RECENCY = 0.2      # Recency weight (20%)
```

### Adaptive Partitioning trong adaptive_partition.py

```python
class AdaptivePartitioner:
    # Cost model (microseconds)
    C_HOT = 100               # Redis read (no decompression)
    C_COLD = 300              # Redis read + LZ4 decompress  
    C_MISS = 5000             # MongoDB query
    
    # Adjustment parameters
    STEP_SIZE_PERCENT = 1     # Adjust by 1% each time
    MIN_HOT_PERCENT = 5       # Minimum HOT tier (5%)
    MAX_HOT_PERCENT = 30      # Maximum HOT tier (30%)
```

### Hướng dẫn tùy chỉnh

**Chạy với workload lớn hơn:**
```python
self.RECORD_COUNT = 100000       # 100k records
self.OPERATION_COUNT = 500000    # 500k operations
self.TOTAL_MEMORY_MB = 400       # 400MB memory budget
```

**Điều chỉnh tỉ lệ HOT tier:**
```python
self.HOT_MEMORY_PERCENT = 20     # 20% (higher for more hot keys)
# Note: > 20% may cause Java heap errors on low-memory systems
```

**Thay đổi compression:**
```python
self.COMPRESSION_ALGORITHM = 'lz4'   # Fast (2484 ops/sec)
self.COMPRESSION_ALGORITHM = 'zlib'  # Better compression (1635 ops/sec)
```

## 📁 Cấu Trúc Dự Án

```
d:\test2/
├── benchmark.py           # YCSB benchmark runner
├── redis_proxy.py         # Tiered cache proxy server
├── tier_manager.py        # Phân loại tầng + quản lý bộ nhớ
├── compression.py         # Nén LZ4/zlib
├── promotion.py           # Worker thăng/giáng hạng nền
├── requirements.txt       # Python dependencies
├── README.md             # File này
├── result/
│   ├── log/              # Logs operations chi tiết
│   └── report/           # Báo cáo benchmark
├── scripts/
│   ├── setup.bat         # Thiết lập môi trường
│   ├── run_benchmark.bat # Helper chạy benchmark
│   └── manual.bat        # Test thủ công
└── ycsb/                 # Công cụ benchmark YCSB
    ├── bin/ycsb.bat
    └── workloads/
```

## 🔬 Phân Tích Phân Phối Zipfian

### Zipfian Hoạt Động Như Thế Nào

Zipfian phân phối requests theo quy luật lũy thừa: `P(k) = 1 / k^s`

Với `s=1.5` (phân phối dốc):
- **Top 1% keys** (96 keys) → **23.4% traffic**
- **Top 10% keys** (964 keys) → **40.3% traffic**
- **Top 20% keys** (1,929 keys) → **52.6% traffic**

### Mẫu Truy Cập (Dữ Liệu Thực Tế)

```
Rank 1:  1,988 requests (3.98%) - Key cực nóng
Rank 2:    995 requests (1.99%)
Rank 3:    752 requests (1.50%)
Rank 10:   236 requests (0.47%)
Rank 50:    51 requests (0.10%)
```

**Insight quan trọng**: Tầng HOT nhỏ (5%) có thể phục vụ phần lớn traffic do độ lệch cực đoan.

---

## 🛠️ Chi Tiết Triển Khai

### Python Access Counter

```python
def record_access(self, key):
    """Ghi nhận truy cập - tăng access_count"""
    self.key_stats[key]['count'] += 1
    self.key_stats[key]['last_access'] = time.time()

def calculate_score(self, key):
    """Tính score dựa trên frequency + recency"""
    stats = self.key_stats[key]
    frequency = stats['count']
    hours_since = (time.time() - stats['last_access']) / 3600.0
    recency = 100.0 / (1.0 + hours_since)
    return (frequency * 0.8) + (recency * 0.2)

def classify_tier(self, key):
    """Phân loại dựa trên score"""
    score = self.calculate_score(key)
    return 'hot' if score >= SCORE_THRESHOLD else 'cold'
```

**Ưu điểm Redis LFU:**
- Tự động tracking, không cần Python counter
- Persistent, không mất khi restart proxy
- IDLETIME built-in cho demotion

### Ước Tính Bộ Nhớ

```python
def _estimate_size(self, hash_data, key=None):
    """Ước tính kích thước hash (cached để tăng tốc)"""
    # Use cached size if available
    if key and key in self.size_cache:
        return self.size_cache[key]
    
    # YCSB: 10 fields × 100 bytes each (approximate)
    size = len(hash_data) * 100 + 50
    
    # Cache for future use
    if key:
        self.size_cache[key] = size
    
    return size
```

Kích thước key trung bình: ~1,100 bytes (dữ liệu YCSB)
**Tối ưu hóa**: Kết quả được cache để tránh tính toán lặp lại (tiết kiệm 50-100μs/access)

### LRU Eviction

```python
def check_memory_limit(tier):
    """Kiểm tra nếu tầng vượt giới hạn, trả về LRU key"""
    if current_memory > max_memory:
        lru_key = get_lru_key(tier)  # Ít được dùng gần đây nhất
        return True, lru_key
    return False, None
```

### Logic Thăng Hạng

```python
# Thời gian thực (trong đường dẫn đọc)
if access_count >= 3 and HOT có chỗ:
    promote_immediately(key)

# Worker nền (mỗi 30s)
if access_count >= 10 and HOT có chỗ:
    promote_key(key)
```

## 📈 Quy Trình Benchmark

```
1. Kiểm tra và khởi động databases (Redis + MongoDB)
   - Auto-start nếu services chưa chạy
   ↓
2. Xóa databases (Redis + MongoDB)
   ↓
3. YCSB Load → MongoDB (50k records, ~7.9s)
   ↓
4. Khởi động proxy phân tầng (port 6380)
   - Promotion worker starts (interval: 60s)
   - Access counting begins
   - HOT tier: 15% (30 MB), COLD tier: 85% (170 MB)
   ↓
5. YCSB Run → Proxy (200k read ops, Zipfian s=1.5)
   - Keys fetched from MongoDB on cache miss
   - Stored in COLD tier (zlib compressed)
   - Score = 0.8 × frequency + 0.2 × recency
   - Hot keys automatically promoted to HOT (uncompressed)
   ↓
6. Real-time promotions (when score ≥ 20)
   - Immediate promotion during read path
   - Decompression happens once, then stays in HOT
   ↓
7. Background worker thăng/giáng hạng (60s intervals)
   - Promote: COLD → HOT (score ≥ 20, space available)
   - Demote: HOT → COLD (idle > 60s, re-compress)
   - LRU eviction if memory limit exceeded
   ↓
8. Tạo báo cáo với thống kê tầng
   - Memory usage, hit rates, tier distribution
   - Compression stats (for zlib variant)
   - Wall-clock time vs YCSB time breakdown
   - Real-time promotion count
```

## 🎓 Bài Học Kinh Nghiệm

### 1. Compression Algorithm Trade-offs Đã Được Xác Nhận
- **LZ4**: 2,484 ops/sec, 1,601μs latency, 43% compression ratio
- **zlib**: 1,635 ops/sec (-34%), 2,430μs latency (+52%), 55% compression ratio
- **Bài học**: LZ4 khuyến nghị cho production (performance > storage), zlib cho hệ thống hạn chế RAM

### 2. Wall-Clock Time vs YCSB Time
- **YCSB reported**: 130.83s (chỉ đo load + run)
- **Wall-clock actual**: 145.30s (đo toàn bộ quá trình)
- **Overhead**: 14.47s (11%) - database clearing, service checks, proxy startup/shutdown, report generation
- **Bài học**: Luôn đo wall-clock time để biết chi phí thực sự của hệ thống

### 3. Score-Based Classification Đơn Giản Nhưng Mạnh
- **Formula**: Score = 0.8 × frequency + 0.2 × recency
- **Threshold**: Score ≥ 20 → HOT tier
- **Kết quả**: 77% Redis hit rate với chỉ 5-6% HOT tier
- **Bài học**: Không cần ML phức tạp, heuristic đơn giản + adaptive partitioning là đủ

### 4. Compression Benchmarks Hoàn Tất
- **LZ4 vs zlib đã được test đầy đủ**: Performance vs compression ratio
- **zlib tốt hơn 28% về compression** (55% vs 43%), nhưng **chậm hơn 52% về latency**
- **COLD tier với zlib**: Ít keys hơn trong COLD (13.6k vs 33.7k với LZ4) do HOT tier được ưu tiên
- **Bài học**: Chọn algorithm dựa trên bottleneck: CPU-bound → LZ4, memory-bound → zlib

### 5. HOT Tier Size Impact
```
HOT 15% (30 MB):
  - 32,041 keys (70%), 32.02 MB (100% utilized) → 62.3% hits
  - COLD: 13,644 keys (30%), 12.36 MB (7% utilized) → 14.8% hits
  
HOT 6% (12 MB) [previous test]:
  - 11,972 keys (26%), 12.00 MB (100% utilized) → 45.7% hits
  - COLD: 33,693 keys (74%), 34.60 MB (18% utilized) → 31.4% hits
```
- **Bài học**: Larger HOT tier → more hits from HOT, fewer from COLD, same total hit rate (~77%)

### 6. Zipfian Distribution Xác Nhận Quy Tắc 80/20
- **Top 70% keys** (HOT tier với 15% memory) → **62.3% traffic**
- **Top 100% keys** (HOT+COLD) → **77% traffic** (rest: MongoDB miss)
- **Consistent hit rate**: ~77% across different HOT tier sizes (6%, 10%, 15%)
- **Bài học**: Zipfian workload self-optimizes, hit rate stable regardless of tier size

### 7. Promotion Strategy: Real-time vs Background
- **Real-time**: Promote ngay khi score ≥ 20 trong read path (11,749 promotions với HOT 15%)
- **Background**: Worker mỗi 60s scan COLD tier tìm candidates
- **Trade-off**: Real-time nhanh hơn nhưng tăng latency (~100μs), background overhead thấp
- **Kết quả**: Hybrid approach hoạt động tốt, majority promoted via real-time path
- **Observation**: Với HOT tier lớn hơn (15% vs 6%), ít promotions hơn do nhiều keys đã ở HOT

### 8. Race Conditions với Daemon Threads
- **Vấn đề**: Background workers ghi log vào file đã đóng khi shutdown → "I/O operation on closed file"
- **Fatal error**: Print error khi Python finalizing → stdout deadlock → crash
- **Giải pháp**: Check `file.closed` trước khi ghi, suppress closed file errors, sleep 2s sau stop()
- **Bài học**: Daemon threads cần graceful shutdown, tránh I/O operations khi Python finalizing

### 9. Hardcoded Config vs Centralized
- **Thử nghiệm**: Tạo config.py để centralize constants
- **Vấn đề**: Import cycles, complexity tăng, khó debug
- **Quyết định**: Restore về hardcoded values trong từng file
- **Bài học**: KISS principle - với small project, hardcoded values trong từng module dễ maintain hơn

## 🔧 Xử Lý Sự Cố

### Lỗi "Connection refused"
```bash
# Kiểm tra Redis đang chạy
redis-cli ping  # Phải trả về PONG

# Kiểm tra MongoDB đang chạy
mongosh  # Phải kết nối được
```

### Lỗi "MISCONF Redis is configured to save RDB snapshots"
**Nguyên nhân**: Redis không thể save RDB snapshot (disk full, permissions, etc.)

**Giải pháp nhanh**:
```bash
redis-cli CONFIG SET stop-writes-on-bgsave-error no
```

**Giải pháp lâu dài**: Fix disk space hoặc permissions cho Redis data dir

### Timeout Errors (YCSB)
**Triệu chứng**: "Operation timed out" trong YCSB run

**Nguyên nhân**: 
- Workload quá lớn (>200k records với 500k ops)
- MongoDB chậm do không index
- Proxy overload

**Giải pháp**:
```python
# Giảm workload trong benchmark.py
self.RECORD_COUNT = 50000        # Thay vì 100000
self.OPERATION_COUNT = 200000    # Thay vì 500000

# Hoặc tăng timeout trong YCSB
# ycsb/workloads/workloadc
operationtimeout=20000  # 20 seconds instead of 10
```

### Java Heap Errors (HOT tier > 20%)
**Triệu chứng**: "Could not reserve enough space for object heap" khi chạy với HOT tier ≥ 20%

**Nguyên nhân**: 
- Hệ thống hết RAM hoặc bị phân mảnh bộ nhớ
- Redis + MongoDB + Python processes + YCSB Java → vượt quá RAM khả dụng
- Java cần contiguous memory block (1GB)

**Giải pháp**:
```python
# Option 1: Giảm Java heap trong benchmark.py
env['JAVA_OPTS'] = '-Xmx512M -Xms256M'  # Thay vì 1G

# Option 2: Giảm workload size
self.RECORD_COUNT = 25000     # Fewer records
self.OPERATION_COUNT = 100000 # Fewer operations

# Option 3: Giảm HOT tier
self.HOT_MEMORY_PERCENT = 15  # Safe limit on most systems
```

**Cleanup trước khi chạy**:
```powershell
# Dừng tất cả processes cũ
Get-Process python | Where-Object {$_.Path -like "*test2*"} | Stop-Process -Force
Get-Process redis-server | Stop-Process -Force
Get-Process mongod | Stop-Process -Force

# Xóa Java error logs
Remove-Item hs_err_pid*.log
```

### Race Condition Errors at Shutdown
**Triệu chứng**: "I/O operation on closed file" và "Fatal Python error" khi benchmark kết thúc

**Nguyên nhân**: Background worker threads cố ghi log vào file đã đóng

**Đã sửa** (10/3/2026):
- Kiểm tra `log_file.closed` trước khi ghi
- Suppress "closed file" errors để tránh stdout deadlock
- Thêm sleep 2s sau `proxy.stop()` để threads dừng hoàn toàn

**Files sửa**: promotion.py, benchmark.py

### Mức sử dụng tầng HOT thấp
**Solution 1**: Giảm SCORE_THRESHOLD trong tier_manager.py
```python
SCORE_THRESHOLD = 15  # Thay vì 20 (easier to promote)
```

**Solution 2**: Tăng WEIGHT_FREQUENCY
```python
WEIGHT_FREQUENCY = 0.9  # Thay vì 0.8 (favor hot keys more)
WEIGHT_RECENCY = 0.1    # Thay vì 0.2
```

**Solution 3**: Tăng initial HOT tier
```python
HOT_MEMORY_PERCENT = 10  # Thay vì 5 (more space for hot keys)
```

### Vượt giới hạn bộ nhớ / Evictions cao
```python
# Tăng total memory trong benchmark.py
self.TOTAL_MEMORY_MB = 400  # Thay vì 200

# Hoặc tăng COLD tier ratio
self.HOT_MEMORY_PERCENT = 3  # Giảm HOT, tăng COLD
```

### Missing Operations trong Log
**Triệu chứng**: Số operations trong log < OPERATION_COUNT

**Nguyên nhân**: 
- Timeout errors không được log
- YCSB client disconnect

**Kiểm tra**: 
```bash
# Đọc report file
cat result/report/tiered_report_*.txt | grep "Missing Operations"

# Nếu > 10% missing → giảm workload hoặc fix timeout
```

## 📚 Tài Liệu Tham Khảo

- **YCSB**: https://github.com/brianfrankcooper/YCSB
- **Redis**: https://redis.io/docs/
- **MongoDB**: https://www.mongodb.com/docs/
- **LZ4**: https://github.com/lz4/lz4
- **Phân Phối Zipfian**: https://en.wikipedia.org/wiki/Zipf%27s_law

### 📖 Tài Liệu Dự Án

- **[Redis & MongoDB Command Reference](REDIS_MONGODB_COMMANDS.md)** - Hướng dẫn sử dụng các câu lệnh cơ bản cho Redis và MongoDB trên terminal
- **[System Status Script](system_status.py)** - Script kiểm tra trạng thái hệ thống nhanh

## 📝 Giấy Phép

Dự án giáo dục - tự do sử dụng và chỉnh sửa.

## 🤝 Đóng Góp

Đây là dự án benchmark/nghiên cứu. Thoải mái:
- Thử nghiệm với các ngưỡng khác nhau
- Test các thuật toán nén khác
- Thử các workload YCSB khác (A, B, D, E, F)
- Mở rộng thành 3 tầng (thêm WARM tier) để so sánh

---

**Cập nhật lần cuối**: 10 tháng 3, 2026  
**Phiên bản**: 3.1 (Compression Benchmarks + Race Condition Fixes)  
**Benchmark mới nhất**: 50k records, 200k ops, zlib compression, HOT 15% → 1635 ops/sec, 77.16% hit rate  
**Compression**: LZ4 vs zlib fully benchmarked | Zstandard removed from project
