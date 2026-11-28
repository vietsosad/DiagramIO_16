# ADR-0001: Phương pháp đo hiệu năng cho pha 2

- Trạng thái: Đề xuất, chờ nhóm duyệt
- Mốc: M0 trong [kế hoạch pha 2](../ke-hoach-pha-2.md)
- Liên quan: [bench/README.md](../../bench/README.md), [danh-gia-hien-trang.md](../pha-2/danh-gia-hien-trang.md)

## Bối cảnh

Đề bài pha 2 yêu cầu đánh giá trước và sau mỗi cải tiến trên đúng một cấu hình phần cứng (Kaggle CPU) và chỉ ra vì sao cải tiến có tác dụng. Phép đo của pha 1 (README mục 6) không đáp ứng được, vì các lý do đã phân tích ở mục 2.2 của kế hoạch:

1. Locust, app và PostgreSQL dùng chung 4 vCPU, không tiến trình nào bị giới hạn CPU, nên không biết đang đo backend hay đo Locust. Ở 100 user, throughput đứng yên ~747 req/s trong khi độ trễ tăng tuyến tính.
2. Kịch bản 100% ghi, dữ liệu sơ đồ nhỏ, CSDL gần rỗng: các vấn đề của đường đọc, danh sách, sơ đồ lớn không lộ ra.
3. Mỗi mức tải chạy một lần, không warm-up, không ghi phiên bản phần mềm và cấu hình máy.
4. Không có số liệu phía server (CPU từng tiến trình, pool kết nối, số câu SQL), nên không giải thích được kết quả.
5. Chỉ có mô hình tải đóng không thời gian nghỉ: khi hệ thống chậm, tải gửi tới tự giảm theo (coordinated omission), nên độ trễ bị đánh giá thấp.

Ngoài ra kịch bản Locust gốc chỉ nằm trong notebook Kaggle của pha 1, không có trong repo, và không đọc được ngoài Kaggle.

## Các phương án

| Vấn đề | Phương án đã xét | Chọn |
|---|---|---|
| Công cụ tạo tải | Locust (như pha 1); k6; wrk2 / vegeta | **k6** v2.3.0 (ghim phiên bản): một file thực thi viết bằng Go, tốn ít CPU hơn Locust; có executor theo tốc độ đến cho mô hình tải mở; tính được số liệu riêng theo tag (pha đo, thao tác, bậc tải) ngay trong bản tóm tắt; hỗ trợ WebSocket cho W4 sau này. wrk2 và vegeta không diễn đạt được kịch bản nhiều bước có đăng nhập và dữ liệu |
| Mô hình tải | Đóng (số VU cố định) như pha 1; mở (tốc độ đến cố định) | **Mở** cho mọi phép đo độ trễ và sức chứa; **đóng** chỉ cho W1 để nối tiếp số liệu pha 1 |
| Định nghĩa sức chứa | Throughput lớn nhất đạt được; throughput tại điểm gãy; throughput lớn nhất còn giữ SLO | **Lớn nhất còn giữ SLO**: p95 ≤ 100 ms, lỗi < 1%, đạt ≥ 95% tốc độ yêu cầu, tính liên tục từ bậc đầu |
| Đếm câu SQL mỗi request | Profile `profiling` bật Hibernate statistics, đếm trong lúc chạy tải; test đặc tả | **Test đặc tả** `SqlPerRequestE2eTest` (datasource-proxy chỉ trong test), cộng với đối chiếu ở tầng CSDL (`pg_stat_statements` chia số request) trong mỗi lần đo |
| CPU theo tiến trình | `pidstat` (sysstat); đọc `/proc` | **Đọc `/proc`** (`collect/procstat.py`): gộp được mọi backend của PostgreSQL thành một nhóm, có cả `steal` và độ bận từng CPU, không phụ thuộc định dạng của từng phiên bản sysstat |
| Số liệu của app | Prometheus + Grafana; cào thẳng endpoint ra CSV | **Cào thẳng** `/actuator/prometheus` mỗi 5 giây (`collect/prom_scrape.py`), không cần dựng thêm dịch vụ trên Kaggle |
| Dữ liệu | Tạo qua API trước khi đo; sinh ngẫu nhiên mỗi lần; sinh xác định rồi nạp bằng `COPY` | **Sinh xác định** (`seed/gen.py`, cùng seed thì giống từng byte, có SHA-256 trong `manifest.json`), nạp một lần vào CSDL mẫu, mỗi lần chạy chép lại |
| Mốc C0 | Tag `v1.0-phase1` (mã pha 1 nguyên vẹn); mã pha 1 cộng phần đo | **Mã pha 1 cộng phần đo** (Actuator, Micrometer): không đổi hành vi nghiệp vụ, nhưng có mặt ở mọi cấu hình nên chi phí của nó không làm lệch phép so sánh |

## Quyết định

**Môi trường.** Mọi lần đo chạy trên Kaggle Notebook CPU. `bench/kaggle/setup.sh` cài PostgreSQL 16 (kho PGDG), JDK Temurin 21.0.13.1, k6 v2.3.0, async-profiler 4.6, có kiểm tra SHA-256 khi bản phát hành công bố. Image Kaggle CPU hiện dựng trên image Colab (Python 3.13), nên script tự nhận bản Ubuntu/Debian thay vì giả định một phiên bản; đã chạy thử trên Ubuntu 24.04 và 22.04. Mỗi phiên ghi `env.txt`: CPU, cặp hyper-thread, hạn mức cgroup, bộ nhớ, phiên bản phần mềm, cấu hình PostgreSQL, commit.

**Bố trí CPU.** Từ tập CPU tiến trình được phép dùng, 3 CPU đầu cho hệ thống được đo (PostgreSQL khởi động bằng `taskset`, mọi backend thừa hưởng; JVM có `-XX:ActiveProcessorCount=3`), CPU thứ 4 cho k6 và bộ thu số liệu. Với 4 vCPU (2 nhân × 2 luồng) thì k6 vẫn chia một nhân vật lý với hệ thống được đo; đây là giới hạn của máy, được ghi trong báo cáo. Báo cáo cảnh báo khi k6 dùng quá 80% CPU của nó hoặc khi `steal` vượt 5%.

**PostgreSQL.** Cấu hình một lần, dùng chung cho mọi cấu hình được đo: `shared_buffers` 4 GB (CSDL mẫu ở scale 1 khoảng 1,1 GB), `max_wal_size` 8 GB và `checkpoint_timeout` 30 phút để không checkpoint giữa lần đo (mỗi lần chạy checkpoint tay trước khi bắt đầu, báo cáo kiểm tra lại), `jit = off` (truy vấn OLTP ngắn; JIT chỉ thêm vài ms biên dịch cho các lần quét lớn và làm nhiễu kết quả), `pg_stat_statements`, `track_io_timing`. `synchronous_commit` giữ mặc định (bật) như khi chạy thật.

**Dữ liệu.** Quy mô theo mục 3.2 của kế hoạch: 2.000 user (vài user có hơn 1.000 sơ đồ), 50.000 sơ đồ (80% ~5 KB, 18% ~50 KB, 2% 0,5-1,5 MB; 10% công khai), ~150.000 phiên bản (50 sơ đồ có 200-400 phiên bản), 20.000 link. Schema do Flyway của chính jar C0 tạo. Trước mỗi lần chạy: `CREATE DATABASE ... TEMPLATE ... STRATEGY FILE_COPY`, `pg_prewarm` mọi bảng, index và TOAST, `CHECKPOINT`; vì vậy lần nào cũng bắt đầu từ cùng trạng thái, dữ liệu ghi của lần trước không ảnh hưởng lần sau.

**Kịch bản.** W1 lặp lại chuỗi 6 request của pha 1 (dựng lại theo README vì kịch bản gốc không có trong repo). W2 là hỗn hợp 85% đọc / 15% ghi, chọn sơ đồ theo phân bố lũy thừa xấp xỉ Zipf (s = 1) trên danh sách đã xếp theo độ phổ biến. W3 là W2 chỉ trên sơ đồ lớn. Trong W2 và W3:

- mỗi iteration là đúng một request, nên tốc độ đến của k6 chính là request/giây;
- mỗi VU chỉ ghi vào phân vùng sơ đồ riêng và tự nhớ version (sửa hay khôi phục thành công thì +1), nên không có 409 giả tạo; tranh chấp thật thuộc về W4;
- thao tác "mở lại" mở lại một trong 256 sơ đồ VU vừa mở, kèm `If-None-Match` của lần trước: ở C0, C1 không có ETag nên là một lần đọc đầy đủ, khi B2 thêm ETag thì cùng kịch bản tự nhận 304. (Trước phiên `b3`, thao tác này chọn một sơ đồ ngẫu nhiên mới nên hiếm khi có ETag hợp lệ: phiên `b12` chỉ 0,3% request được 304; [kết quả](../pha-2/ket-qua.md) mục 6.3);
- 200 user nhiều sơ đồ nhất đăng nhập trước khi đo (trong `setup`), access token kéo dài 8 giờ trong bench để lần chạy dài không bị hết hạn; thời hạn token không ảnh hưởng chi phí kiểm tra chữ ký.

**Thời lượng và lặp.** Mỗi lần chạy khởi động JVM mới, warm-up (không tính) rồi đo 180 giây. Mỗi phép đo lặp 3 lần, báo cáo trung vị và min–max. Trong một phiên, các cấu hình chạy xen kẽ trong từng vòng lặp (C0, C1, C0, C1...) để độ trôi của máy ảo ảnh hưởng đều; so sánh hai cấu hình chỉ hợp lệ trong cùng một phiên.

Warm-up ban đầu là 60 giây. Phiên `b12` cho thấy chừng đó chưa đủ: ở tốc độ đến cố định, CPU app phải gần như không đổi, nhưng nó giảm suốt pha đo vì JIT còn biên dịch (C1 ở 400 req/s: 0,93 rồi 0,64 rồi 0,56 nhân qua từng phút; ở W2, phần ba đầu pha đo cao hơn phần ba cuối 27-137%). Từ phiên `b3` warm-up 180 giây, và báo cáo cảnh báo khi độ chênh đó vượt 25% ([kết quả](../pha-2/ket-qua.md) mục 6.2). Ma trận `m0`, `b12` giữ 60 giây để lần chạy lại so được với phiên đã có.

Phiên `b3` cho thấy warm-up dài vẫn chưa đủ khi tốc độ cao: JVM nguội nhận ngay 700 req/s thì dùng hết 3 CPU, request xếp hàng, và hàng đợi cần 200-340 giây mới tan. Kết quả là cả 8 lần chạy 700 req/s hỏng, dù hệ thống đã ấm chịu được mức đó (các lần đo sức chứa tăng tải dần đều giữ SLO ở 700 req/s; [kết quả](../pha-2/ket-qua.md) mục 7.4). Từ ma trận `b4`, tốc độ cố định tăng tuyến tính từ 50 req/s (`RAMP_FROM`) lên mức yêu cầu trong `WARMUP_RAMP` giây đầu warm-up (90 trong 180 giây), rồi giữ nguyên tới hết pha đo. Pha đo vẫn bắt đầu sau `WARMUP` giây, nên đã chạy đủ tốc độ ít nhất `WARMUP - WARMUP_RAMP` giây. Ma trận cũ giữ `WARMUP_RAMP=0` (đủ tốc độ ngay từ đầu, như trước).

**Nhiễu của máy ảo.** Máy Kaggle có thể bị chiếm CPU trong những đợt ngắn: ở phiên `b3`, steal lên tới 10,6% trong một khoảng 15 giây, trong khi trung bình cả pha đo vẫn dưới ngưỡng cảnh báo 5%. Khi một chênh lệch nhỏ (dưới khoảng 10%) quan trọng, dùng k6 làm tiến trình đối chứng: k6 làm cùng một việc ở mọi lần chạy cùng mức tải, nên CPU của k6 cho mỗi request tăng nghĩa là máy chậm đi, không phải hệ thống được đo làm nhiều hơn. Cũng nên so thêm trên các khoảng 10 giây "yên" (steal dưới 1%) và trên số liệu phía server. Mục 7.3 của kết quả là ví dụ: một cấu hình có vẻ tốn CPU app hơn, nhưng ở đúng các lần chạy đó, CPU của k6 cho mỗi request cũng tăng 9-24%.

**Sức chứa.** Tốc độ đến tăng theo bậc (W2: 100 req/s mỗi bậc 30 giây), bỏ 1 giây chuyển bậc và 5 giây đầu mỗi bậc. Độ phân giải của kết quả là một bậc (100 req/s với W2). Một bậc vượt p95 1 giây hoặc lỗi 5% thì dừng sớm để không phí thời gian khi đã quá tải rõ ràng. Bảng p95 theo thao tác của báo cáo lấy mức tải cố định cao nhất mà mọi cấu hình còn giữ SLO: ở mức quá tải, mọi thao tác chỉ còn thời gian xếp hàng như nhau nên không so được.

**Nhiều instance (B4).** Nginx nhận ở cổng API như app khi chạy một mình, nên k6 và kịch bản không đổi. Instance i nghe cổng API + i, cổng quản trị + i. Heap (`JVM_HEAP`) và tổng số kết nối CSDL (`POOL_TOTAL`, 12) chia đều cho các instance, để 1, 2, 3 instance dùng cùng tài nguyên. Nginx chạy trên CPU của hệ thống được đo, cấu hình sinh từ đúng `deploy/nginx/http.conf` và `server.conf` của docker-compose. `procstat.py` tính CPU của mọi instance vào "app" (thêm từng instance riêng) và của Nginx vào "lb"; `prom_scrape.py` cào mọi instance trong cùng một vòng; báo cáo cộng gauge theo vòng cào và cộng counter theo từng instance, kể cả khi instance khởi động lại (counter đếm lại từ 0). W7 và W7r ghi từng sự kiện vào `incidents.csv`; `nginx_timeline.py` dựng dòng thời gian từng giây từ log truy cập của Nginx (request, lỗi, p99, request gửi lại, request mỗi instance), báo cáo tính lỗi trong cửa sổ sự cố và thời gian hồi phục từ đó ([ADR-0006](0006-mo-rong-ngang-san-sang.md)).

**Số liệu phía server.** App có Spring Boot Actuator và Micrometer Prometheus trên cổng quản trị riêng chỉ nghe `127.0.0.1` (mặc định 9090), chỉ mở `health` và `prometheus`; cổng API không lộ thêm gì (`ManagementEndpointsE2eTest`). Có histogram cho `http.server.requests` để tính p95 phía server và so với p95 phía client. Bộ thu số liệu chạy trên CPU của máy tạo tải. Thống kê PostgreSQL được đặt về 0 ngay trước k6 và xuất sau khi app đã dừng (backend chỉ đẩy hết thống kê khi kết nối đóng). Flame graph CPU (async-profiler, 60 giây giữa pha đo) chạy trong một lần đo riêng vì profiler làm nhiễu độ trễ.

**Số câu SQL mỗi request.** Số câu lệnh, transaction, lần mượn kết nối và số byte giữa app và CSDL của từng endpoint là đại lượng xác định, không phụ thuộc phần cứng, nên được đo bằng `SqlPerRequestE2eTest` (bọc DataSource bằng datasource-proxy, chỉ trong test). Test này đồng thời là mốc: cải tiến nào làm đổi số câu SQL thì sửa con số trong test cùng PR. Trong mỗi lần đo tải, báo cáo tính thêm số câu SQL mỗi request ở tầng CSDL để đối chiếu; ở lần chạy thử, W1 cho 3,34 câu/request ở tầng CSDL, khớp với 20 câu / 6 request mà test đặc tả tính ra.

**Mốc C0.** C0 là mã pha 1 cộng phần đo (Actuator, Micrometer, cổng quản trị), không đổi hành vi nghiệp vụ: toàn bộ test pha 1 vẫn xanh, `docs/openapi.yaml` không đổi. Đề xuất gắn tag `v1.0-phase1` cho commit `35503aa` (mã nộp pha 1, để tham khảo) và `v2.0-m0` cho commit M0 (mốc C0 của mọi phép so sánh pha 2).

## Hệ quả

- Mỗi kết quả pha 2 đi kèm: cấu hình máy, commit, tham số, dữ liệu (SHA-256), số liệu phía server để giải thích, và phần kiểm tra tính hợp lệ.
- Số liệu pha 1 (Locust, commit `ca41f27`) chỉ để tham khảo, không so trực tiếp với pha 2: khác công cụ, khác dữ liệu, khác bố trí CPU.
- Chi phí: Actuator và Micrometer chạy trong mọi cấu hình; hệ thống được đo chỉ có 3 vCPU thay vì 4; một phiên đo `m0` mất khoảng 3 giờ; độ phân giải của sức chứa là một bậc tải.
- Giới hạn còn lại: máy ảo Kaggle có thể bị chiếm CPU (`steal` được ghi lại, nhưng đợt ngắn không làm trung bình vượt ngưỡng cảnh báo); k6 và hệ thống được đo vẫn chung một nhân vật lý; đo trên localhost nên không thấy chi phí mạng; nhiều instance trên một máy chỉ chia nhau 3 CPU, nên phép đo B4 cho thấy chi phí và tính sẵn sàng của kiến trúc nhiều instance, không cho thấy sức chứa của nhiều máy.
