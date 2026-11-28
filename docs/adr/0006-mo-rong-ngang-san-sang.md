# ADR-0006: Mở rộng ngang và tính sẵn sàng: nhiều instance sau Nginx, tắt máy êm (B4)

- Trạng thái: Đã triển khai, chờ số đo Kaggle (ma trận `b4`)
- Mốc: B4 trong [kế hoạch pha 2](../ke-hoach-pha-2.md)
- Liên quan: [ADR-0003](0003-cache-hai-tang-etag.md) (cache hai tầng, pub/sub, sàn version), [ADR-0004](0004-cache-token-chia-se.md) (token chỉ trên Redis khi nhiều instance), [ADR-0005](0005-ra-soat-gia-co-sau-b3.md) (giới hạn body), [ADR-0001](0001-phuong-phap-do.md) (phương pháp đo)

## Bối cảnh

Trước B4 chỉ có một tiến trình app. Thêm bản để tăng năng lực thì không được. Tiến trình chết là ngừng dịch vụ. Triển khai bản mới phải dừng hệ thống, và request đang chạy lúc dừng bị cắt ngang.

Phần khó của việc chạy nhiều instance đã được giải quyết trước:

- App không giữ phiên: xác thực bằng JWT, mọi trạng thái nằm ở PostgreSQL.
- Cache sơ đồ ở mỗi instance (L1) được giữ đúng bằng Redis pub/sub, heartbeat và sàn version ([ADR-0003](0003-cache-hai-tang-etag.md) mục 8). Token chia sẻ chỉ cache trên Redis, nên thu hồi có hiệu lực ngay ở mọi instance ([ADR-0004](0004-cache-token-chia-se.md), kiểm bằng `TwoInstanceCacheE2eTest`).

Việc còn lại của B4 là hạ tầng và vòng đời: chia tải cho N instance; tắt một instance (khi triển khai) mà không rơi request; một instance chết đột ngột thì mất ít nhất có thể, và biết chính xác mất gì.

## Quyết định

Chiến thuật: dư thừa chủ động (active-active), cân bằng tải, theo dõi sức khỏe, tắt êm.

1. **Nginx làm load balancer** (`deploy/nginx/`, dùng chung cho docker-compose, bộ đo Kaggle và test):
   - `least_conn`: instance đang có ít request dở nhất nhận request mới. Instance chậm (vừa khởi động, JIT chưa ấm, đang dọn rác) tự nhận ít đi.
   - Kiểm tra sức khỏe thụ động (`max_fails=1 fail_timeout=5s`): một lần lỗi kết nối thì bỏ qua instance đó 5 giây, rồi thử lại bằng chính request thật. Nginx bản mã nguồn mở không có kiểm tra chủ động. Với khởi động lại có kế hoạch, việc này không quan trọng: Nginx được báo trước (mục 4).
   - Gửi lại sang instance khác khi lỗi (`proxy_next_upstream error timeout http_502 http_503`, tối đa 2 lần, trong 10 giây). Nginx không bao giờ gửi lại POST, PATCH đã tới app, vì app có thể đã xử lý. GET, PUT, DELETE thì gửi lại. PUT an toàn khi gửi lại nhờ optimistic locking: nếu lần đầu đã ghi xong, lần gửi lại nhận 409 và client đọc lại như với mọi lần 409, không ghi đè được gì.
   - Hai nhóm kết nối tới cùng các instance. GET, PUT, DELETE dùng nhóm giữ kết nối (keepalive 32, đóng kết nối rảnh sau 30 giây, sớm hơn 60 giây của Tomcat, nên không bao giờ gửi vào một kết nối app vừa đóng). POST, PATCH luôn mở kết nối mới: instance đang tắt từ chối ngay ở bước kết nối, lúc request chưa được gửi, nên Nginx chuyển nó sang instance khác an toàn.
   - `keepalive_min_timeout 2s` (từ Nginx 1.27.4): khi nạp lại cấu hình, worker cũ không đóng ngay kết nối keep-alive rảnh của client. Thiếu nó, một request client vừa gửi vào kết nối đó bị reset; nếu là POST thì client nhận lỗi.
   - Header `X-Forwarded-*` và `Host` giữ địa chỉ client đã dùng, để `Location` và `shareUrl` trỏ đúng ra ngoài (`server.forward-headers-strategy=framework`). Lỗi do chính Nginx sinh (502, 504, 413) theo khung JSON của API. Body tối đa 8 MB, lớn hơn giới hạn 4 MB của app, để app tự trả 413 theo khung của nó.
2. **App tắt êm và có probe:**
   - `server.shutdown=graceful`, `spring.lifecycle.timeout-per-shutdown-phase=${SHUTDOWN_TIMEOUT:20s}`.
   - Probe `liveness` và `readiness` trên cổng quản trị (`/actuator/health/liveness`, `/actuator/health/readiness`; cổng API vẫn không có Actuator). Readiness không gồm PostgreSQL và Redis. Đó là tài nguyên dùng chung: nếu readiness phụ thuộc chúng, Redis chết sẽ kéo mọi instance ra khỏi load balancer cùng lúc, trong khi app vẫn chạy tiếp được bằng CSDL (ADR-0003, ADR-0004).
   - Thứ tự khi nhận SIGTERM (các SmartLifecycle dừng theo phase, phase lớn dừng trước):
     1. Readiness chuyển sang `REFUSING_TRAFFIC`.
     2. `ShutdownConfig` đóng cổng nghe: kết nối mới bị từ chối ở bước kết nối, Nginx chuyển sang instance khác với mọi phương thức. Sau đó chờ 500 ms (`diagramio.http.shutdown-accept-grace`) để các kết nối đã nhận đọc xong request.
     3. Spring Boot tạm dừng connector và chờ các request đang chạy, tối đa `SHUTDOWN_TIMEOUT`.
     4. `RedisCacheMonitor` dừng sau web server (phase thấp hơn): request đang được làm nốt vẫn có L2 và pub/sub. Khi nó dừng, L1 thôi được tin (`subscriptionStopped`), vì không còn nhận được tin hủy từ instance khác.
   - Lý do của bước 2: Spring Boot tạm dừng connector trước rồi mới đóng cổng nghe. Kết nối đã nhận mà chưa kịp đọc request bị Tomcat đóng mà không xử lý; với POST, Nginx không được gửi lại, nên client nhận 502 (mục Kiểm chứng có số đo).
3. **Docker Compose** (`docker-compose.scale.yml`, ghi đè file gốc): `up --scale app=N`. App không có `container_name` và cổng ra máy; chỉ Nginx mở cổng. Nginx dùng DNS của Docker (`resolver 127.0.0.11 valid=5s` và tham số `resolve` của `server`, từ Nginx 1.27.3), nên thêm, bớt hay khởi động lại bản sao không cần nạp lại Nginx. Mỗi bản sao: cache `two-level`, `APP_POOL_SIZE` kết nối CSDL (tổng chia số bản sao), `mem_limit` (heap 75% của nó), healthcheck readiness, `stop_grace_period` 30 giây (lâu hơn `SHUTDOWN_TIMEOUT`).
4. **Khởi động lại cuốn chiếu** (`deploy/rolling-restart.sh`). Với từng bản sao:
   1. Ghi danh sách instance có bản sao đó là `down`, `nginx -t`, nạp lại.
   2. Chờ tới khi không còn worker nào của cấu hình cũ. `nginx -s reload` trả về ngay, và worker cũ vẫn phục vụ nốt kết nối theo cấu hình cũ, tức là vẫn có thể gửi tới bản sao vừa bị đánh dấu.
   3. `docker restart -t 30` (SIGTERM, tắt êm như mục 2), chờ healthcheck báo sẵn sàng.
   4. Trả lại danh sách mặc định. Script lỗi giữa chừng thì `trap` vẫn trả lại.
5. **Bộ đo Kaggle** chạy N tiến trình `java -jar` trên cổng API + i, Nginx sinh cấu hình từ đúng `http.conf` và `server.conf` ở trên, cùng bố trí CPU. Heap và tổng số kết nối CSDL (12) chia đều cho các instance (12 = 1×12 = 2×6 = 3×4). W7 giết instance 1 bằng `kill -9` ở giây 60 của pha đo và bật lại ở giây 120. W7r khởi động lại cuốn chiếu cả 3 instance theo đúng trình tự của mục 4 ([ADR-0001](0001-phuong-phap-do.md), mục "Nhiều instance").

## Các phương án đã cân nhắc

| Vấn đề | Phương án | Chọn và lý do |
|---|---|---|
| Load balancer | Nginx; HAProxy; Spring Cloud Gateway; DNS round-robin | **Nginx**: kế hoạch đã chọn, cấu hình ngắn, có sẵn ở mọi môi trường (image chính thức, gói nginx.org cho Kaggle). HAProxy có kiểm tra chủ động nhưng thêm một công cụ, trong khi kiểm tra thụ động cộng đánh dấu `down` trước khi tắt đã đủ. Gateway viết bằng Java là thêm một JVM cần ấm. DNS round-robin không bỏ được instance chết |
| Chia tải | round-robin; `least_conn`; băm theo id sơ đồ | **`least_conn`**: tự tránh instance chậm. Băm theo id làm L1 trúng nhiều hơn và ít phải hủy, nhưng sơ đồ nóng dồn hết vào một instance (W2 chọn sơ đồ theo Zipf); để làm thí nghiệm tùy chọn |
| Phát hiện instance hỏng | Kiểm tra chủ động (Nginx Plus, HAProxy, module ngoài); thụ động | **Thụ động**: instance chết bị phát hiện ở lần kết nối lỗi đầu tiên, và request đó được gửi lại. Khởi động lại có kế hoạch thì không dựa vào phát hiện lỗi: Nginx được báo trước (`down`) |
| POST, PATCH khi instance tắt | Cho phép gửi lại (`non_idempotent`); kết nối riêng không keepalive; khóa idempotency | **Kết nối riêng**: gửi lại POST có thể tạo trùng (hai phiên bản, hai link). Khóa idempotency đúng nhất nhưng đổi API và cần bảng lưu khóa, ngoài phạm vi. Kết nối mới cho mỗi POST, PATCH tốn một lần bắt tay TCP trên localhost, đổi lại instance đang tắt luôn từ chối ở bước kết nối |
| Thứ tự tắt | Mặc định của Spring Boot; đóng cổng nghe trước rồi tắt êm; thêm đánh dấu `down` ở Nginx | **Cả hai lớp**: đóng cổng nghe trước giảm lỗi khi bị tắt mà không báo trước (`docker stop`, hệ điều hành tắt); đánh dấu `down` trước khi tắt bỏ hẳn lỗi khi triển khai có kế hoạch (số đo ở Kiểm chứng) |
| Readiness | Gồm PostgreSQL, Redis; chỉ trạng thái của chính instance | **Chỉ instance**: lỗi của tài nguyên dùng chung không được kéo cả cụm ra khỏi load balancer |
| Cập nhật sang image mới | `docker compose up` (tạo lại mọi bản sao cùng lúc); blue-green bằng hai service; Swarm, Kubernetes | **Ngoài phạm vi, ghi là hạn chế**: Compose không có cập nhật cuốn chiếu. Script chỉ khởi động lại container đang chạy (đủ để chứng minh tắt êm và đổi instance không rơi request). Triển khai thật dùng bộ điều phối có rolling update và readiness probe, cùng các probe đã có |

## Hệ quả

- **Sẵn sàng.** Khởi động lại có kế hoạch không rơi request nào. Instance chết đột ngột thì chỉ mất các POST, PATCH đang chạy trên đúng instance đó (client nhận 502, không biết thao tác đã xong chưa), cộng các lần 409 của PUT đã ghi xong rồi được gửi lại. GET không lỗi: Nginx gửi lại sang instance khác.
- **Chi phí.** Thêm một chặng Nginx cho mọi request, và mỗi instance có L1 riêng (nhỏ hơn khi chia heap, phải hủy lẫn nhau qua pub/sub). Trên một máy 4 vCPU, thêm instance không thêm CPU. Throughput chỉ tăng nếu nút thắt nằm bên trong một JVM (GC, khóa, pool); nếu không sẽ đi ngang hoặc giảm nhẹ. Phiên `b4` đo chính xác hai chi phí này (C3 so với C4x1, C4x1 so với C4x2, C4x3).
- **Rủi ro còn lại**, ghi rõ để không bị hiểu nhầm là đã giải quyết:
  - *Cửa sổ giữa commit và cập nhật cache.* App ghi CSDL rồi mới ghi trạng thái mới lên Redis và báo các instance khác (sau commit, ADR-0003). Instance chết đúng giữa hai bước thì Redis và L1 của instance khác giữ bản cũ. Bản cũ đó bị sửa ở lần ghi kế tiếp vào sơ đồ: UPDATE có điều kiện không khớp, app đọc version thật từ CSDL và dựng sàn cho cả cụm (ADR-0003 mục 8). Nếu không có lần ghi nào, bản cũ sống tới hết thời gian sống (L1 10 phút, Redis 30 phút). Dữ liệu trong CSDL không bao giờ sai. Muốn đóng hẳn cửa sổ thì phải đặt dấu "đang đổi" trước khi ghi như B3, tốn thêm một lượt Redis cho mọi lần ghi sơ đồ; chưa làm.
  - *Kết nối nằm trong hàng đợi của nhân hệ điều hành.* Kết nối đã bắt tay xong nhưng app chưa nhận bị reset khi cổng nghe đóng. Với POST, client nhận 502. Chỉ xảy ra khi instance bị tắt mà Nginx không được báo trước.
  - *Instance vừa khởi động nhận traffic khi JVM còn nguội.* Chạy thử bộ đo trên máy dev (2 instance, 50 req/s): mỗi lần một instance quay lại, p99 vọt lên 370-650 ms trong 1-2 giây rồi trở lại bình thường, không có lỗi. Nginx bản mã nguồn mở không có `slow_start` (tăng dần lượng request cho instance mới). Có thể giảm bằng một bước làm ấm trước khi readiness báo sẵn sàng, nhưng W7 vẫn không tránh được: sau `kill -9`, Nginx gửi lại request ngay khi cổng mở, không chờ readiness. Chưa làm; phiên `b4` đo mức ảnh hưởng ở 400 req/s.
  - *Nginx là điểm hỏng duy nhất* trong cấu hình này. Triển khai thật cần hai Nginx với địa chỉ IP chuyển được (keepalived), hoặc load balancer của nhà cung cấp đám mây.
  - *Cập nhật sang image mới* bằng Compose làm gián đoạn ngắn (xem bảng trên).
- Quy tắc cho người sửa code ở [AGENTS.md](../../AGENTS.md): mọi thay đổi cấu hình Nginx sửa ở `deploy/nginx/` (cả ba nơi dùng chung), giữ thứ tự tắt, không đưa tài nguyên dùng chung vào readiness.

## Kiểm chứng

- `GracefulShutdownE2eTest`: giữ khóa dòng (`SELECT ... FOR UPDATE`) để một PUT đang chạy dở, rồi đóng app. Test theo trạng thái thật của connector (`isPaused` của Tomcat), không đoán theo thời gian:
  - Giai đoạn 1: readiness là `REFUSING_TRAFFIC`, liveness vẫn `CORRECT`, cổng API không nhận kết nối mới, nhưng connector chưa tạm dừng, và request trên kết nối đã nhận vẫn trả 200.
  - Giai đoạn 2: connector tạm dừng; request mới trên kết nối cũ không được xử lý (version trong CSDL không đổi); bộ theo dõi Redis còn chạy và L1 còn được tin.
  - Nhả khóa thì PUT dở trả 200 version 2, và Redis có version 2. Sau đó bộ theo dõi mới dừng và L1 thôi được tin.

  Đã thử gây lỗi hai chỗ, lần nào test cũng đỏ: đặt `RedisCacheMonitor` về phase mặc định; cho `ShutdownConfig` dừng sau web server.
- `NginxFailoverE2eTest`: Nginx thật (image `nginx:1.28-alpine`, đúng các file của `deploy/nginx/`) trước hai instance thật.
  1. `Location`, `shareUrl` mang địa chỉ client đã dùng, không phải cổng của instance.
  2. Body quá lớn nhận 413 dạng JSON từ app hoặc từ Nginx.
  3. Đánh dấu `down` một instance, chờ worker cũ thoát, rồi tắt nó giữa lúc 4 luồng liên tục GET, PUT, POST: không request nào lỗi.
  4. Không còn instance nào: Nginx trả 502 dạng JSON của API.

  Ổn định 8/8 lần chạy cả lớp.
- Thứ tự tắt, đo bằng cùng test (4 client, 10 lần tắt một trong hai instance mà không đánh dấu `down` trước):

  | Cách tắt | POST nhận 502 | GET, PUT lỗi |
  |---|---|---|
  | Mặc định của Spring Boot | 7 | 0 |
  | Có `ShutdownConfig` | 3 | 0 |
  | Có `ShutdownConfig`, đánh dấu `down` trước khi tắt | 0 (18 lần tắt) | 0 |

- `ManagementEndpointsE2eTest`: probe trả `UP` trên cổng quản trị; trên cổng API chúng không tồn tại (401 cho khách như mọi đường dẫn không công khai, 404 khi đã đăng nhập).
- Compose 3 bản sao ở máy dev, dưới tải hỗn hợp: `rolling-restart.sh` trọn một lượt, 0 request lỗi trong 15.309.
- CI: `nginx -t` với đúng các file và đường dẫn của `docker-compose.scale.yml`, `docker compose config` cho file override, `shellcheck` cho `deploy/`.
- Bộ đo nhiều instance chạy thử trọn vẹn bằng ma trận `quick` trong container Ubuntu 24.04 trắng (cài từ đầu, Nginx 1.30.8 của nginx.org, 2 instance, 50 req/s). Số đo trên máy dev chỉ để kiểm tra bộ đo, không dùng để kết luận. W7: 0 lỗi trên 2.978 request; Nginx chuyển 22 request sang instance còn sống. W7r: 0 lỗi, 0 request phải gửi lại trên 4.455 request.
- Số đo Kaggle: ma trận `b4` (C3 so với C4x1, C4x2, C4x3; W2 400, 700 req/s và sức chứa; W7, W7r ở C4x3), chờ phiên đo.
