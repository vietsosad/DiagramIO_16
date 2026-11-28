# Kết quả pha 2

| | |
|---|---|
| Trạng thái | B1, B2 đã đo (phiên `b12`, mục 1-6). B3 và lần đo lại C2, C3 sau bản sửa ở mục 6 đã đo (phiên `b3`, mục 7). B4 chờ phiên `b4` |
| Phiên đo | [`20261126-144513-b12`](../../bench/results/20261126-144513-b12/report.md), Kaggle CPU, 26-27/11/2026; [`20261127-152353-b3`](../../bench/results/20261127-152353-b3/report.md), Kaggle CPU, 27/11/2026 |
| Phương pháp | [ADR-0001](../adr/0001-phuong-phap-do.md); cách chạy lại: [bench/README.md](../../bench/README.md) |
| Quyết định liên quan | [ADR-0002](../adr/0002-toi-uu-truy-cap-du-lieu.md) (B1), [ADR-0003](../adr/0003-cache-hai-tang-etag.md) (B2), [ADR-0004](../adr/0004-cache-token-chia-se.md) (B3) |
| Mốc so sánh | [Đánh giá hiện trạng](danh-gia-hien-trang.md) (C0, phiên M0) |

## 1. Tóm tắt

- **B1 (C0 → C1) đạt và vượt mục tiêu chính.** Sức chứa W2 với p95 ≤ 100 ms tăng từ **200 lên 800 req/s** (gấp 4, mục tiêu QA-P1 là gấp 2). Ở 200 req/s, p95 giảm từ 100,8 xuống 9,0 ms, CPU PostgreSQL từ 1,12 xuống 0,26 nhân, số khối CSDL mỗi request từ 839 xuống 101. W1 (ghi nhiều) tăng 43% throughput. W3 (sơ đồ 0,5-1,5 MB) phục vụ kịp tới 70 req/s, trước đây là 30.
- **B2 (C1 → C2, cache trong JVM) giảm việc của CSDL, độ trễ W2 gần như không đổi.** Tỉ lệ trúng L1 là 80%. Số câu SQL mỗi request của W2 giảm 42% (1,49 → 0,87). Ở trạng thái gần ổn định, CPU PostgreSQL giảm 18% (W2) và 53% (W3), thời gian xử lý phía server của W3 giảm 44%. p95 của W2 vẫn khoảng 9-14 ms: phần còn lại là danh sách, tìm kiếm, lịch sử và các lần ghi, không thuộc phạm vi cache. Sức chứa theo SLO vẫn là 800 req/s, vì ở mức đó máy tạo tải (k6, 1 vCPU) đã là nút thắt. Tốc độ còn phục vụ kịp tăng từ khoảng 850 lên khoảng 1.000 req/s.
- **C3 (thêm Redis, một instance) đắt hơn C2 mà không nhẹ thêm cho CSDL.** CPU app cao hơn 15%, W1 chậm hơn 18%. Đúng như dự kiến: L2 chỉ có ích khi nhiều instance dùng chung (B4).
- **Phiên đo phát hiện hai lỗi thật của B2 và hai thiếu sót của bộ đo, đều đã sửa** (mục 6). Lỗi B2 là 6 lần 409 sai ở C3, do cache trả bản cũ sau các lần Redis hết thời gian chờ. Thiếu sót của bộ đo là warm-up 60 giây chưa đủ, và lần "mở lại" hầu như không gửi ETag hợp lệ. Vì vậy số của C3 (và một phần của C2) ở phiên này bị ảnh hưởng; phiên `b3` đo lại C2, C3 bằng mã đã sửa.
- **Bản sửa của B2 đứng vững khi đo lại (phiên `b3`, mục 7.2).** Không có lần 409 nào trong cả 28 lần chạy. Trong đó 10 lần chạy có Redis mà mỗi lần vẫn có 71-268 lệnh Redis hết thời gian chờ lúc warm-up (phiên `b12`: 6 lần 409 sai trong hoàn cảnh đó). 304 tăng từ 0,3% lên 2,5-3,0% số request. Hai tầng trên một instance vẫn tốn hơn C2 khoảng 15% CPU app.
- **B3 (cache token) bỏ được việc của CSDL, không đổi được độ trễ hay sức chứa trên máy này** (mục 7.3). Số câu SQL mỗi request giảm 19-20%, số transaction giảm 21-22%, tỉ lệ trúng cache token 80%. Một instance: p50 của request qua link giảm từ 2,0 xuống 1,1 ms ở lần lặp không bị nhiễu. Hai tầng: một lượt Redis thay một câu SQL, p50 giữ 2,1 ms. Sức chứa của bốn cấu hình là 700-750 req/s, chênh nhau không quá một bậc. Hai lần chạy có cache token trùng lúc máy ảo bị chiếm CPU (steal tới 10,6%), nên chênh lệch CPU app (−7% đến +9% tùy lát cắt) không tách được khỏi nhiễu.

## 2. Phiên đo

- Máy: Kaggle CPU, Intel Xeon 2,20 GHz, 4 vCPU (2 nhân × 2 luồng), RAM 31 GB, Ubuntu 24.04. JDK Temurin 21.0.13.1, k6 2.3.0, PostgreSQL 16.16, Redis 7.4.12. Steal dưới 0,6%. Phiên M0 chạy trên AMD EPYC 7B12, nhanh hơn, nên C0 ở đây thấp hơn số của M0 (sức chứa W2 200 so với 300 req/s): chỉ so các cấu hình trong cùng một phiên.
- Bố trí CPU: CPU 0-2 cho hệ thống được đo (app, PostgreSQL, Redis), CPU 3 cho k6 và bộ thu số liệu. Heap 2 GB, G1.
- Dữ liệu: scale 1,0, seed 20261123: 2.000 user, 50.000 sơ đồ (1.582 MB data), 148.345 phiên bản, 20.000 link; CSDL chép lại từ bản mẫu trước mỗi lần chạy.
- Cấu hình:

| Cấu hình | Mã | Tham số |
|---|---|---|
| C0 | `v2.0-m0` (`15f2356`), pha 1 cộng phần đo | |
| C1 | `v2.1-b1` (`1383937`), B1 | |
| C2 | `v2.2-b2` (`3aa8d6a`), B2 | `--diagramio.cache.mode=local` |
| C3 | `v2.2-b2` (`3aa8d6a`), B2 | `--diagramio.cache.mode=two-level` |

- 2 lần lặp cho mỗi phép đo, các cấu hình chạy xen kẽ; warm-up 60 giây, đo 180 giây. Tổng 56 lần chạy, khoảng 4,7 giờ (14:45 tới 19:25 UTC, kể cả cài đặt, build và nạp dữ liệu). Các bảng dưới là trung vị qua các lần lặp (khoảng min-max trong báo cáo đầy đủ).

## 3. B1: C0 → C1

### 3.1 W2, tốc độ đến cố định

| Cấu hình | Tốc độ | Đạt (req/s) | p50 (ms) | p95 (ms) | p99 (ms) | CPU app | CPU PostgreSQL | SQL/request | Khối CSDL/request |
|---|---|---|---|---|---|---|---|---|---|
| C0 | 200 | 200 | 6,6 | 100,8 | 429,9 | 0,99 | 1,12 | 2,14 | 839 |
| C1 | 200 | 200 | 2,4 | **9,0** | 20,7 | 0,44 | **0,26** | 1,49 | **101** |
| C0 | 400 | 259 (quá tải) | 3.674 | 5.624 | 6.836 | 1,38 | 1,58 | 2,15 | 859 |
| C1 | 400 | 400 | 2,6 | **12,8** | 31,3 | 0,70 | 0,59 | 1,49 | 199 |

Ở 400 req/s, C0 không phục vụ kịp: 200/200 luồng Tomcat bận, tới 192 request chờ kết nối CSDL. C1 không có request nào phải chờ kết nối (tối đa 1-2 kết nối đang dùng). p95 theo thao tác ở C1, 400 req/s: danh sách của tôi 7,6 ms, tìm theo tên 25,7 ms, danh sách công khai 15,0 ms, sửa 19,5 ms, khôi phục 15,6 ms (C0: 5-7 giây cho mọi thao tác vì xếp hàng).

### 3.2 Sức chứa

Sức chứa là bậc cuối cùng còn giữ p95 ≤ 100 ms, lỗi < 1% và đạt ≥ 95% tốc độ yêu cầu. Cột "Phục vụ kịp tới" bỏ điều kiện p95, chỉ cho biết hệ thống còn xử lý hết tải tới đâu.

| Kịch bản | Cấu hình | Sức chứa theo SLO (req/s) | Phục vụ kịp tới (req/s, 2 lần lặp) | CPU k6 cao nhất |
|---|---|---|---|---|
| W2 | C0 | 200 | 300, 300 | 0,36 |
| W2 | C1 | **800** | 800, 900 | 0,80 |
| W3 | C0 | không bậc nào | 30, 30 | 0,31 |
| W3 | C1 | 10 ở một lần lặp | **70, 70** | 0,41 |

Ở bậc 900 req/s, C1 đã dùng hết CPU của hệ thống được đo (app 1,16 + PostgreSQL 1,61 trên 3 nhân), nên 800 req/s là trần thật của C1 trên máy này. Với W3, SLO p95 ≤ 100 ms gần như không đạt được ở bất kỳ mức nào, vì riêng các lần PUT sơ đồ 1 MB đã có p95 khoảng 145 ms ngay ở tải thấp. Cột "Phục vụ kịp tới" phản ánh W3 đúng hơn.

### 3.3 W1 (mô hình đóng, 50 VU, chủ yếu là ghi)

| Cấu hình | Throughput (req/s) | p50 (ms) | p95 (ms) | p99 (ms) | CPU app | CPU PostgreSQL | SQL/request |
|---|---|---|---|---|---|---|---|
| C0 | 832 (775-889) | 50,2 | 150,5 | 225,1 | 1,99 | 0,76 | 3,33 |
| C1 | **1.191** (1.095-1.286) | 36,5 | **99,0** | 142,0 | 1,78 | 0,82 | 2,17 |

Throughput tăng 43%, p95 giảm 34%. Số câu SQL mỗi request giảm đúng như bảng của `SqlPerRequestE2eTest`: sửa 3 → 2 câu, lưu phiên bản 4 → 2, khôi phục 6 → 4, tạo link 4 → 2 ([ADR-0002](../adr/0002-toi-uu-truy-cap-du-lieu.md)).

### 3.4 W3 (sơ đồ 0,5-1,5 MB), 20 req/s

| Cấu hình | p50 (ms) | p95 (ms) | p99 (ms) | CPU app | CPU PostgreSQL | Server trung bình (ms) | SQL/request | Khối CSDL/request | Khối TOAST/request |
|---|---|---|---|---|---|---|---|---|---|
| C0 | 110,1 | 156 / 21.450 (2 lần lặp) | 219 / 30.143 | 0,98 | 0,65 | 3.392 | 2,19 | 857 | 93,2 |
| C1 | **19,2** | **97,3** | 131,3 | 0,24 | 0,34 | 24,1 | 1,52 | **133** | 75,9 |

Một lần lặp của C0 bị quá tải ngay ở 20 req/s (chỉ đạt 17 req/s), lần còn lại có p95 156 ms. Lần lặp không quá tải đó vẫn chậm hơn C1 tới 60% ở p95.

### 3.5 Kế hoạch thực thi (EXPLAIN ANALYZE, BUFFERS)

| Truy vấn | C0 | C1 |
|---|---|---|
| Danh sách công khai (trang 10 và tổng) | 2 câu: đếm 19,8 ms (quét tuần tự, 6.144 khối) + trang 1,3 ms | **1 câu, 2,5 ms** (index-only `idx_diagrams_public_updated`, 251 khối) |
| Sơ đồ của tôi (trang và tổng) | 2 câu: 7,6 + 0,3 ms (3.131 khối) | **1 câu, 1,3 ms** (index-only `idx_diagrams_owner_updated`, 57 khối) |
| Tìm theo tên | 2 câu: 10,8 + 2,3 ms (3.380 khối) | 1 câu, 6,6 ms (bitmap qua index trigram, 706 khối) |
| Link của một sơ đồ | 2,5 ms (quét tuần tự) | 0,5 ms (index `diagram_shares(diagram_id)`) |
| Lịch sử (389 phiên bản) | 2,9 ms | 2,2 ms (không đọc `data`) |

Kế hoạch đầy đủ: [`explain-C0.txt`](../../bench/results/20261126-144513-b12/explain-C0.txt), [`explain-C1.txt`](../../bench/results/20261126-144513-b12/explain-C1.txt).

## 4. B2: C1 → C2, C3

### 4.1 W2, tốc độ đến cố định

| Cấu hình | Tốc độ | p50 (ms) | p95 (ms) | p99 (ms) | CPU app | CPU PostgreSQL | CPU Redis | SQL/request | Transaction CSDL/request | Trúng cache |
|---|---|---|---|---|---|---|---|---|---|---|
| C1 | 200 | 2,4 | 9,0 | 20,7 | 0,44 | 0,26 | | 1,49 | 1,40 | |
| C2 | 200 | 2,1 | 8,9 | 19,7 | 0,45 | 0,20 | | 0,89 | 0,80 | L1 80% |
| C3 | 200 | 2,4 | 10,0 | 19,8 | 0,51 | 0,20 | 0,02 | 0,90 | 0,82 | L1 78%, L2 1% |
| C1 | 400 | 2,6 | 12,8 | 31,3 | 0,70 | 0,59 | | 1,49 | 1,40 | |
| C2 | 400 | 2,2 | 13,7 | 40,3 | 0,78 | 0,48 | | 0,87 | 0,78 | L1 80% |
| C3 | 400 | 2,7 | 17,2 | 74,5 | 0,96 | 0,49 | 0,04 | 0,88 | 0,79 | L1 79%, L2 15% |

"L2 15%" là tỉ lệ trúng Redis trong số lần trượt L1.

### 4.2 Trạng thái gần ổn định

Warm-up 60 giây chưa đủ để JVM chạy ổn định (mục 6.2): CPU app ở phút đầu của pha đo cao hơn hẳn phút cuối. Bảng dưới so cả pha đo với một phần ba cuối, tính từ số liệu phía server (`procstat.csv`, histogram `http.server.requests`):

| Kịch bản | Tốc độ | Cấu hình | CPU app (cả pha / 1/3 cuối) | CPU PostgreSQL (cả pha / 1/3 cuối) | Server trung bình ms (cả pha / 1/3 cuối) |
|---|---|---|---|---|---|
| W2 | 400 | C1 | 0,70 / 0,56 | 0,59 / 0,61 | 3,47 / 3,37 |
| W2 | 400 | C2 | 0,78 / 0,59 | 0,48 / **0,50** | 3,20 / **3,17** |
| W2 | 400 | C3 | 0,96 / 0,68 | 0,49 / 0,50 | 3,95 / 3,52 |
| W3 | 20 | C1 | 0,24 / 0,23 | 0,34 / 0,34 | 24,1 / 24,1 |
| W3 | 20 | C2 | 0,21 / 0,18 | 0,17 / **0,16** | 15,0 / **13,6** |
| W3 | 20 | C3 | 0,24 / 0,22 | 0,17 / 0,17 | 16,5 / 16,2 |

Ở trạng thái gần ổn định, C2 so với C1: CPU PostgreSQL −18% ở W2, −53% ở W3; CPU app gần như không đổi (+5% ở W2, −22% ở W3); thời gian xử lý phía server −6% ở W2, −44% ở W3.

### 4.3 W3 và sức chứa

| Cấu hình | W3 20 req/s: p50 (ms) | p95 (ms) | Khối TOAST/request | Trúng cache | Phục vụ kịp tới, W3 (req/s) | Sức chứa W2 theo SLO | Phục vụ kịp tới, W2 | CPU k6 cao nhất (W2) |
|---|---|---|---|---|---|---|---|---|
| C1 | 19,2 | 97,3 | 75,9 | | 70, 70 | 800 | 800, 900 | 0,80 |
| C2 | **5,2** | 95,2 | 60,1 | L1 81% | **110, 130** | 800 | **1.000, 1.000** | 0,84-0,90 |
| C3 | 5,2 | 103,0 | 63,6 | L1 81%, L2 41% | 130, 90 | 800 | 800, 1.000 | 0,87-0,88 |

Ở bậc 1.000 req/s, C2 chỉ dùng khoảng 2,1 trong 3 nhân của hệ thống được đo (app 1,11, PostgreSQL 0,97), trong khi k6 đã dùng tới 0,9 nhân của nó. Nút thắt lúc này là máy tạo tải, nên số đo không cho biết sức chứa thật của C2. Kết luận an toàn: sức chứa theo SLO **ít nhất** 800 req/s, còn tốc độ phục vụ kịp tăng khoảng 10-25%. Phiên `b3` thêm mức 700 req/s, dưới ngưỡng k6 bão hòa, để so các cấu hình có cache ở tải cao mà vẫn đo tin được.

### 4.4 W1 và chi phí của Redis

| Cấu hình | Throughput W1 (req/s) | p95 (ms) | CPU app | CPU PostgreSQL | CPU Redis |
|---|---|---|---|---|---|
| C1 | 1.191 | 99,0 | 1,78 | 0,82 | |
| C2 | 1.156 | 108,4 | 1,81 | 0,74 | |
| C3 | 950 | 125,9 | 2,03 | 0,61 | 0,07 |

W1 gần như toàn là ghi, nên cache L1 không giúp được (chênh lệch giữa C1 và C2 nằm trong khoảng lặp). Ở C3, mỗi lần ghi còn chép cả sơ đồ lên Redis; bản `v2.2-b2` được đo tốn hai lượt tới Redis cho mỗi lần ghi (ghi rồi publish), bản sau rà soát ([ADR-0005](../adr/0005-ra-soat-gia-co-sau-b3.md)) gộp còn một. Khi app đã dùng gần hết CPU, phần việc thêm đó làm throughput giảm 18%.

### 4.5 Diễn giải

- **Cache giúp nhiều nhất khi đọc CSDL đắt.** Ở W3, mỗi lần đọc sơ đồ 1 MB buộc PostgreSQL giải nén TOAST và chuyển `jsonb` sang text. Trúng L1 bỏ được cả hai, nên p50 giảm từ 19,2 xuống 5,2 ms. Ở W2, B1 đã làm đường đọc rẻ (kiểm tra quyền 4 cột, index-only), nên phần CSDL tiết kiệm được nhỏ và không hiện ra ở p95.
- **p95 của W2 do phần cache không bỏ được việc của CSDL quyết định.** Ở C2, 400 req/s, các thao tác có p95 cao nhất là tìm theo tên 26,7 ms, sửa 23,7 ms, khôi phục 22,1 ms, lưu phiên bản 19,6 ms, danh sách công khai 17,2 ms: danh sách không được cache, còn ghi thì luôn phải ghi CSDL. Các thao tác mở sơ đồ chỉ còn 5-8 ms.
- **ETag/304 chưa được đo đúng.** Chỉ 0,3% request được trả 304, vì thao tác "mở lại" của bộ đo hầu như không gửi ETag hợp lệ (mục 6.3). Lợi ích này sẽ được đo ở phiên `b3`.
- **Hai tầng trên một instance là chi phí thuần.** Đây là phép đo có chủ đích của kế hoạch (C3 "để đo riêng chi phí của Redis"). L2 chỉ trả lại phần chi phí đó khi nhiều instance dùng chung nó (B4), hoặc khi một instance vừa khởi động lại với L1 rỗng.

## 5. Đánh giá theo thuộc tính chất lượng

| ID | Mục tiêu | Kết quả | Đánh giá |
|---|---|---|---|
| QA-P1 | Sức chứa W2 (p95 ≤ 100 ms) ≥ 2 lần C0 | C0 200 → C1 800 req/s; C2, C3 ≥ 800 (bị k6 chặn) | **Đạt** (gấp 4) |
| QA-P2 | W3: SQL mỗi request giảm ≥ 50%, p95 giảm | C1: SQL −31% (2,19 → 1,52); khối CSDL −84%; p95 156 → 97 ms (lần lặp C0 không quá tải). C2: SQL −58% so với C0 | **Đạt một phần với riêng B1** (số câu lệnh giảm 31%, lượng dữ liệu giảm 84%); B1 cộng B2 đạt |
| QA-P3 | Danh sách, tìm kiếm p95 ≤ 50 ms, không đọc `data` | W2 400 req/s, C1: 7,6 / 25,7 / 15,0 ms; kế hoạch là quét index, không chạm `data` | **Đạt** |
| QA-M1 | Domain không import thư viện mới; `ArchitectureTest` xanh | Xanh ở mọi commit | **Đạt** |

Số câu lệnh của W3 chỉ giảm 31% ở B1 vì mỗi lần mở sơ đồ vẫn cần một câu đọc nội dung. B1 bỏ các lần đọc thừa và lượng dữ liệu đi kèm, không bỏ được chính lần đọc đó; việc đó thuộc về B2 (cache), và cộng cả hai thì số câu lệnh giảm 58%.

## 6. Sự cố phát hiện trong phiên đo và cách đã sửa

### 6.1 Cache trả bản cũ, PUT nhận 409 sai (lỗi của B2, chỉ thấy ở C3)

Ở W2, mỗi VU chỉ sửa sơ đồ của mình và tự đếm version, nên không thể có 409. Vậy mà C3 có 6 lần 409 (4 ở lần lặp 1, 2 ở lần chạy lấy flame graph). Diễn biến, theo `app.log` và `procstat.csv`:

1. Trong 25-40 giây đầu, cả 3 CPU của hệ thống được đo bận 95-100% (JIT biên dịch, cache còn rỗng; app dùng 2,4-2,8 nhân). Các lệnh Redis chở sơ đồ tới 1,5 MB qua một kết nối dùng chung, nên vượt thời gian chờ 250 ms (tới 364 lệnh lỗi trong một lần chạy, đều trong lúc warm-up; W1 chỉ có sơ đồ nhỏ nên không có lỗi nào dù CPU cũng bão hòa).
2. Mỗi lần lỗi, cache ngừng dùng Redis **và bỏ toàn bộ L1**, dồn mọi lần đọc xuống CSDL.
3. Khi bộ theo dõi thấy Redis trả lời lại, cache dùng lại L2, nhưng chỉ 2-50 ms sau lại ngừng. Nguyên nhân: một lệnh gửi *trước* lần hồi phục báo lỗi muộn, khi luồng của nó chờ xong khóa của lần hồi phục. Chuỗi này lặp 5 lần trong 25 giây.
4. Một request đọc CSDL thấy version N, rồi một lần ghi tạo N+1, rồi L1 bị bỏ toàn bộ. Khi request đọc kia ghi vào cache, phép so "chỉ nhận bản mới hơn" không còn gì để so, nên bản N quay lại cache.
5. Lần PUT tiếp theo của VU gửi đúng version N+1. Bước kiểm tra trong `DiagramService` so với version N lấy từ cache và trả 409. Đọc lại cũng thấy N, nên lần PUT sau thua ở câu UPDATE có điều kiện: mỗi bản cũ sinh ra hai lần 409.

Dữ liệu không bao giờ sai, vì câu UPDATE có điều kiện vẫn là chốt chặn cuối. Nhưng người dùng thấy bản cũ và bị từ chối sai, tức là vi phạm cam kết "người vừa ghi luôn đọc được bản mới" của ADR-0003. Bản sửa (commit `3ea68b5`, chi tiết ở [ADR-0003](../adr/0003-cache-hai-tang-etag.md) mục 8):

- **Sàn version thay cho xóa.** Mọi chỗ trước đây bỏ một mục giờ để lại "các version dưới N đã cũ". Đó là các trường hợp: vừa ghi, instance khác báo đổi, UPDATE không khớp. Trên Redis, mục bị thay bằng một dấu chỉ có version. Mỗi lần ghi vào L1 kiểm tra sàn ngay trong `compute` của khóa. Bỏ toàn bộ L1 thì tăng một epoch, nên mục đang được ghi dở cũng vô hiệu.
- **Lỗi lệnh Redis chỉ tắt L2.** L1 chỉ bị bỏ khi mất pub/sub (heartbeat không về). Lỗi của một lệnh gửi trước lần đổi trạng thái gần nhất bị bỏ qua.
- **PUT chỉ từ chối ngay khi version gửi lên nhỏ hơn version đọc được.** Version đọc được có thể chậm hơn CSDL nhưng không bao giờ đi trước. Mọi trường hợp khác do câu UPDATE có điều kiện quyết định. Câu UPDATE không ghi `is_public` khi người sửa không phải chủ, nên một cờ công khai cũ trong cache không thể bị ghi ngược lại.
- **UPDATE không khớp đọc version hiện tại từ CSDL** (4 cột, chỉ khi có cache) và dựng sàn ở đó, cho cả cache, Redis và các instance khác.

Kiểm chứng: 22 test mới. Bốn test của `DiagramCacheTest` tái hiện đúng lỗi (đỏ trên mã cũ, xanh sau khi sửa). `TwoLevelCacheStateTest` có 9 test với Redis giả: lỗi lệnh không bỏ L1, lỗi muộn không làm hỏng lần hồi phục, mất pub/sub thì L1 làm lại từ rỗng, thay đổi trong lúc mất Redis được dựng sàn khi Redis trở lại. Đã thử gây lỗi lại (mutation) ba chỗ chính, lần nào cũng có test đỏ. Toàn bộ 611 test xanh.

### 6.2 Warm-up 60 giây chưa đủ

Ở tốc độ đến cố định, CPU app phải gần như không đổi trong pha đo. Thực tế nó giảm dần, vì JIT còn biên dịch suốt phút đầu: C1 ở 400 req/s dùng 0,93 nhân ở phút đầu của pha đo, 0,64 ở phút thứ hai, 0,56 ở phút thứ ba. Ở W2, CPU app của phần ba đầu pha đo cao hơn phần ba cuối 27-137% (C1 tới C3, cả hai mức tải).

- Ảnh hưởng: CPU và p99 trung bình cả pha đo bị đẩy lên. So sánh giữa các cấu hình trong cùng phiên vẫn công bằng ở mức thô, vì cấu hình nào cũng chịu. Riêng C3 chịu nặng hơn, vì L1 của nó bị bỏ nhiều lần trong warm-up (mục 6.1). Mục 4.2 đưa thêm số của phần ba cuối.
- Sửa (commit `7117f32`): `report.py` báo độ chênh này và cảnh báo khi vượt 25%; ma trận `b3` và `tune` warm-up 180 giây. `m0`, `b12` giữ 60 giây để lần chạy lại so được với phiên đã có. Báo cáo của phiên này được sinh lại với các kiểm tra mới (commit `a422425`, mọi con số cũ giữ nguyên).

### 6.3 Thao tác "mở lại" hầu như không gửi ETag hợp lệ

Kế hoạch định nghĩa 10% request của W2 là client mở lại sơ đồ đã có, kèm `If-None-Match`. Bộ đo lại chọn một sơ đồ ngẫu nhiên mới cho mỗi lần mở lại, và chỉ gửi ETag nếu tình cờ trùng sơ đồ VU đã mở trước đó. Kết quả chỉ 0,3% request được 304. Sửa (commit `27987bc`): mỗi VU nhớ 256 sơ đồ vừa mở cùng ETag và mở lại một trong số đó. Cấu hình không trả ETag (C0, C1) nhận đúng chuỗi request như cũ.

### 6.4 k6 là nút thắt từ khoảng 850 req/s

Với 1 vCPU, k6 dùng quá 80% CPU từ khoảng 850 req/s. Trên một máy Kaggle 4 vCPU không thể cấp thêm CPU cho k6 mà không lấy của hệ thống được đo. Vì vậy sức chứa của cấu hình có cache chỉ được báo là "ít nhất", và phiên `b3` thêm một mức tốc độ cố định 700 req/s.

## 7. B3 và đo lại C2, C3 (phiên `b3`)

### 7.1 Phiên đo

- Phiên [`20261127-152353-b3`](../../bench/results/20261127-152353-b3/report.md), 27/11/2026, 15:23 tới khoảng 19:05 UTC. Máy cùng loại với phiên `b12` (Kaggle CPU, Xeon 2,20 GHz, 4 vCPU); bố trí CPU, heap, dữ liệu như mục 2.
- Một jar (`v2.3.2`, commit `50683e5`, có bản sửa ở mục 6.1), bật tắt cache token bằng cờ:

| Cấu hình | Tham số | Ý nghĩa |
|---|---|---|
| C2 | `--diagramio.cache.mode=local --diagramio.cache.share-tokens=false` | B2, cache sơ đồ trong JVM, không cache token |
| C2b3 | `--diagramio.cache.mode=local` | C2 cộng cache token trong JVM (B3) |
| C3 | `--diagramio.cache.mode=two-level --diagramio.cache.share-tokens=false` | B2 hai tầng, không cache token |
| C3b3 | `--diagramio.cache.mode=two-level` | C3 cộng cache token trên Redis (B3) |

- W2 ở 400 và 700 req/s, sức chứa, 2 lần lặp, cộng một lần chạy lấy flame graph ở 400 req/s cho mỗi cấu hình. Warm-up 180 giây, đo 180 giây. 28 lần chạy.
- Báo cáo được sinh lại bằng `report.py` của B4 (commit B4 cùng nhánh). Mọi con số giữ nguyên. Hai thay đổi: dòng bố trí CPU (trước in "?"), và bảng p95 theo thao tác lấy mức cao nhất mà mọi cấu hình còn giữ SLO (400) thay vì mức giữa (700, nơi mọi cấu hình đều quá tải, mục 7.4).

### 7.2 Đo lại C2, C3 sau bản sửa cache

| Chỉ số (W2, 400 req/s) | C2 ở `b12` | C2 ở `b3` | C3 ở `b12` | C3 ở `b3` |
|---|---|---|---|---|
| p95 / p99 (ms) | 13,7 / 40,3 | 12,9 / 55,8 | 17,2 / 74,5 | 14,0 / 41,6 |
| CPU app (cả pha đo; `b12`: 1/3 cuối) | 0,78 (0,59) | 0,58 | 0,96 (0,68) | 0,68 |
| Server trung bình (ms) | 3,20 | 2,93 | 3,95 | 3,33 |
| Trúng cache | L1 80% | L1 84% | L1 79%, L2 15% | L1 83%, L2 35% |
| 304 | 0,3% | 2,6% | 0,3% | 2,9% |
| Lần 409 | 0 | 0 | 6 (sai) | 0 |

Hai phiên chạy trên hai máy ảo khác nhau, nên cột `b12` chỉ để đối chiếu, không để tính phần trăm cải thiện. Ba điều so được:

- **Không còn 409 sai.** Hoàn cảnh gây lỗi ở `b12` vẫn xảy ra: lúc warm-up, cả 3 CPU bão hòa và các lệnh Redis chở sơ đồ lớn hết thời gian chờ (71-268 lệnh mỗi lần chạy, ở cả 10 lần chạy tốc độ cố định có Redis; 0 lệnh trong pha đo). Với bản sửa, cache không còn trả bản cũ: không lần chạy nào có 409, và L1 vẫn trúng 83%.
- **Warm-up 180 giây đủ ở 400 req/s.** CPU app ở phần ba đầu và phần ba cuối pha đo chênh nhau dưới 25% ở mọi lần chạy 400 req/s, trừ một lần (`C2b3_w2_fixed400_r2`, 34%, trùng lúc máy ảo bị chiếm CPU, mục 7.3). CPU app cả pha đo bằng mức của phần ba cuối ở `b12`.
- **Hai tầng trên một instance vẫn là chi phí thuần, đúng như `b12`.** C3 so với C2 trong cùng phiên: CPU app +17% (0,68 so với 0,58), thời gian phía server +14%. L2 trúng 35% số lần trượt L1 nhưng chỉ bớt được 0,02 câu SQL mỗi request (0,82 so với 0,84): trên một instance, Redis chỉ có ích với những sơ đồ L1 đã bỏ ra (hết chỗ, hoặc quá 10 phút thời gian sống của L1).

Mở lại sơ đồ kèm ETag (mục 6.3) giờ được đo thật: 2,5-3,0% số request nhận 304, tức khoảng một phần tư số lần mở lại.

### 7.3 Cache token (B3): C2 → C2b3, C3 → C3b3

W2, 400 req/s, trung vị (min-max) của 2 lần lặp:

| Cấu hình | p50 (ms) | p95 (ms) | p99 (ms) | CPU app | CPU PostgreSQL | CPU k6 | Server trung bình (ms) | SQL/request | Transaction/request | Trúng cache |
|---|---|---|---|---|---|---|---|---|---|---|
| C2 | 2,1 | 12,9 (12,4-13,4) | 55,8 | 0,58 | 0,45 | 0,41 | 2,93 | 0,84 | 0,75 | L1 84% |
| C2b3 | 2,7 | 45,6 (19,1-72,1) | 470 (148-792) | 0,71 | 0,49 | 0,47 | 3,87 | **0,68** | **0,59** | L1 84%, token 80% |
| C3 | 2,3 | 14,0 | 41,6 | 0,68 | 0,46 | 0,42 | 3,33 | 0,82 | 0,73 | L1 83%, L2 35% |
| C3b3 | 2,5 | 16,5 | 64,8 | 0,71 | 0,45 | 0,44 | 3,49 | **0,66** | **0,57** | L1 83%, L2 34%, token 80% |

**Phần đo được chắc chắn: việc của CSDL.** Mỗi request qua link (22% số request của W2) trước đây tốn một câu tra `diagram_shares`. Với cache token, số câu SQL mỗi request giảm 19% (C2 → C2b3) và 20% (C3 → C3b3), số transaction giảm 21% và 22%. Kết quả giống nhau ở mọi lần lặp và mọi mức tải. Tỉ lệ trúng 80% (tính cả cache âm): mỗi token được nạp một lần rồi dùng lại tới khi hết 5 phút. Khi trúng cache, đường đọc qua link không chạy câu SQL nào (`ShareTokenCacheE2eTest`), đúng chỉ tiêu "từ 2 câu xuống 0" của kế hoạch.

**Đường qua link nhanh hơn ở một instance, không đổi ở hai tầng.** p50 của `share_open` và `share_header` (mở link, đọc kèm `X-Share-Token`):

| | C2 | C2b3 | C3 | C3b3 |
|---|---|---|---|---|
| Lần lặp 1 | 2,0 / 2,0 | **1,1 / 1,2** | 2,1 / 2,1 | 2,1 / 2,2 |
| Lần lặp 2 | 2,0 / 2,1 | 2,1 / 2,1 | 2,2 / 2,3 | 2,1 / 2,1 |

Ở lần lặp không bị nhiễu, cache trong JVM giảm p50 gần một nửa. Ở chế độ hai tầng, mỗi lần tra token là một lượt Redis (một script Lua) thay cho một câu SQL, tốn gần như bằng nhau. Đây là đánh đổi đã chọn ở [ADR-0004](../adr/0004-cache-token-chia-se.md): ở nhiều instance, token chỉ nằm trên Redis để thu hồi có hiệu lực ngay ở mọi instance. Lợi ích là bớt việc cho PostgreSQL (nút thắt chung khi thêm instance), không phải độ trễ.

**Phần không kết luận được: CPU app và p95.** Theo bảng, C2b3 và C3b3 có p95, p99 và CPU app cao hơn, ở mọi thao tác, kể cả các thao tác không dùng token. Phân tích kỹ cho thấy chênh lệch này chủ yếu đến từ máy ảo, không phải từ cache token:

1. **Máy chậm đi đúng lúc chạy C2b3.** k6 làm cùng một việc ở mọi lần chạy (cùng tốc độ, cùng script), vậy mà CPU của k6 cho mỗi request tăng 9%, 24% và 9% ở ba lần chạy C2b3 (hai lần lặp và lần lấy flame graph) so với lần chạy C2 ngay trước. CPU PostgreSQL cũng tăng dù C2b3 gửi ít câu SQL hơn. Hai trong ba lần đó có steal (máy ảo bị chiếm CPU) tới 10,6% và 4,6% trong những khoảng 15 giây, trong khi ở mọi lần chạy khác không quá 1,9%. Ở `C2b3_w2_fixed400_r2` (p99 792 ms), phía server không có request nào quá 0,5 giây trong cả pha đo, nên đuôi đó nằm ngoài thời gian app xử lý: trong hàng đợi kết nối, hoặc ở chính k6 khi CPU của nó bị chiếm.
2. **Chỉ xét các khoảng 10 giây "yên"** (steal dưới 1%, CPU k6 không vọt lên). Thời gian xử lý phía server gần như bằng nhau: C2 2,95 ms, C2b3 3,01 ms, C3 3,28 ms, C3b3 3,42 ms. CPU app vẫn cao hơn 7% (C2b3) và 9% (C3b3). Lần lặp 2 của C2b3 không có khoảng yên nào.
3. **Ở các lần đo sức chứa**, nơi CPU k6 cho mỗi request bằng nhau giữa các cấu hình, C2b3 lại dùng *ít* CPU app cho mỗi request hơn C2 7% (1,72 so với 1,86 ms). C3b3 bằng C3.
4. **Mã và flame graph không chỉ ra chi phí nào.** Khóa của `LocalShareTokenStore` chỉ bao bước ghi, không bao lần đọc CSDL. Trên flame graph, phần xác thực link rẻ đi: `ShareService` từ 7,6% xuống 3,7% số mẫu, adapter `diagram_shares` từ 7,3% xuống 2,9%. Phần mẫu tăng thêm rải đều trên Tomcat, Spring, Jackson và kernel, đúng kiểu "cùng việc nhưng CPU chậm hơn". Ở C3b3 có thêm phần của Lettuce (+2,4 điểm) cho lượt Redis.

Kết luận: cache token bớt được 20% số câu lệnh của CSDL. Trên máy này, nó không làm giảm, cũng không làm tăng CPU app một cách đo được: chênh lệch nằm trong khoảng −7% đến +9% tùy lát cắt, cùng cỡ với nhiễu của máy ảo. Cờ `diagramio.cache.share-tokens` giữ bật mặc định. Phiên `b4` chạy mọi cấu hình với cache token bật.

**Sức chứa** (p95 ≤ 100 ms): C2 700, C2b3 700, C3 700, C3b3 750 req/s (700 và 800 ở hai lần lặp). Chênh lệch không quá một bậc 100 req/s. Ở các bậc 700-800 req/s, k6 dùng 0,81-0,88 nhân, sát giới hạn 0,8 của nó (mục 6.4). Sức chứa của C2 thấp hơn 800 của phiên `b12`. Hai phiên chạy trên hai máy ảo khác nhau, và script W2 mới (vòng 256 sơ đồ mở lại) tốn CPU k6 hơn một chút (0,41 so với 0,39 nhân ở 400 req/s). Vì vậy chỉ so các cấu hình trong cùng phiên.

QA-SEC1 (thu hồi ở instance A, request đầu tiên ở instance B bị từ chối, 100%) được kiểm bằng test E2E hai instance thật (`TwoInstanceCacheE2eTest`), không cần phép đo tải: **đạt**.

### 7.4 Mức 700 req/s không đo được: JVM nguội nhận ngay toàn bộ tải

Cả 8 lần chạy 700 req/s đều có p95 trên 1 giây. Nguyên nhân là cách bơm tải, không phải sức chứa: tốc độ cố định bắt đầu ngay ở 700 req/s khi JVM còn nguội (JIT chưa biên dịch, cache rỗng). App dùng hết 3 CPU, request xếp hàng, và hàng đợi cần nhiều phút mới tan:

- C2, C2b3 hết quá tải sau 200-270 giây kể từ lúc bắt đầu, tức 20-90 giây vào pha đo. Ở phút cuối, cả bốn lần chạy phục vụ đủ 700 req/s với 5,0-6,5 ms phía server và 0,9-1,06 nhân app.
- C3, C3b3 cần 330-340 giây, một lần không hết quá tải trong cả lần chạy. Ở hai tầng, một request tốn nhiều CPU hơn, nên khi máy đã bão hòa thì hàng đợi tan chậm hơn.

Các lần đo sức chứa tăng tải từ 100 req/s theo bậc. Ở đó, cả bốn cấu hình đều giữ SLO ở bậc 700 req/s. Như vậy hệ thống đã ấm chịu được 700 req/s, chỉ JVM nguội là không. Ở 400 req/s, khởi động nguội cũng để lại dấu vết: 731-2.133 iteration bị k6 bỏ trong phút đầu (thiếu VU vì độ trễ vọt lên).

Sửa bộ đo: chế độ tốc độ cố định có thêm `WARMUP_RAMP`, tăng tuyến tính từ 50 req/s lên mức yêu cầu trong những giây đầu warm-up rồi giữ nguyên tới hết pha đo ([ADR-0001](../adr/0001-phuong-phap-do.md)). Ma trận `b4` dùng 90 giây tăng dần trong 180 giây warm-up. `m0`, `b12`, `b3` giữ cách cũ (`WARMUP_RAMP=0`) để chạy lại vẫn so được.

## 8. Giới hạn

- Hai lần lặp cho mỗi phép đo (khoảng min-max chỉ có hai điểm); chọn để vừa một phiên Kaggle 12 giờ.
- Số của C3, và một phần của C2, ở phiên `b12` chịu ảnh hưởng của lỗi ở mục 6.1 và warm-up ngắn ở mục 6.2. Mục 7.2 có số đo lại.
- Kết luận chỉ đúng trên một máy 4 vCPU, nơi app, PostgreSQL và Redis dùng chung 3 CPU. Ở đó Redis cạnh tranh CPU với chính app. Trên máy riêng cho Redis, chi phí của C3 sẽ khác.
- SLO p95 ≤ 100 ms không hợp với W3 (PUT 1 MB đã gần 150 ms). Báo cáo dùng thêm "phục vụ kịp tới" cho W3.
- Máy ảo Kaggle có thể bị chiếm CPU giữa phiên (steal tới 10,6% trong phiên `b3`). Kiểm tra tính hợp lệ chỉ cảnh báo khi steal trung bình cả pha đo vượt 5%, nên những đợt ngắn vẫn lọt qua. Khi một chênh lệch nhỏ (dưới 10%) quan trọng, cần xem CPU k6 cho mỗi request như một tiến trình đối chứng (mục 7.3).

## 9. Việc tiếp theo

1. B4: phiên `b4` ở tag `v2.4-b4`. C3 (một app) so với 1, 2, 3 instance sau Nginx; W2 400, 700 req/s (warm-up tăng dần), sức chứa; W7 (một instance chết đột ngột) và W7r (khởi động lại cuốn chiếu) ở 3 instance ([ADR-0006](../adr/0006-mo-rong-ngang-san-sang.md)).
2. Tùy chọn: ma trận `tune` (pool Hikari 20, virtual threads). Phiên `b3` cho thấy pool 10 thừa ở 400 req/s: trung bình 0,7-1,0 kết nối đang dùng, không request nào phải chờ. Vì vậy B4 dùng tổng 12 kết nối chia đều cho các instance (12, 6, 4).
