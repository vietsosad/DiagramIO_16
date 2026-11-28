# ADR-0004: Cache xác thực link chia sẻ, thu hồi có hiệu lực ngay (B3)

- Trạng thái: Đã triển khai, đã đo (phiên `b3`, [kết quả](../pha-2/ket-qua.md) mục 7.3)
- Mốc: B3 trong [kế hoạch pha 2](../ke-hoach-pha-2.md)
- Liên quan: [ADR-0003](0003-cache-hai-tang-etag.md) (cache sơ đồ, B2), [ADR-0001](0001-phuong-phap-do.md) (phương pháp đo)

## Bối cảnh

Người cầm link chia sẻ gửi token ở mọi request (`X-Share-Token`) hoặc mở `GET /shares/{token}`. Ở W2 đó là 22% số request (mở link 12%, đọc kèm `X-Share-Token` 10%). Trước B3, mỗi request như vậy tra `diagram_shares` thêm một câu SQL; `GET /shares/{token}` chạy 2 câu kể cả khi sơ đồ đã nằm trong cache của B2 (`SqlPerRequestE2eTest`).

Đặc tả bắt buộc: **thu hồi link phải mất hiệu lực ngay**, từ request tiếp theo. Cache nội dung của B2 chấp nhận vài mili giây trễ giữa các instance; với quyền truy cập thì không.

## Quyết định

Chiến thuật: cache, với mức nhất quán chọn theo yêu cầu nghiệp vụ. Quyền truy cập cần nhất quán chặt hơn nội dung sơ đồ.

1. **Decorator** `CachingShareRepository` bọc adapter của link chia sẻ, được `CacheConfig` đăng ký làm `DiagramShareRepository` chính. Domain không đổi, chỉ thêm việc cổng thu hồi trả link vừa xóa (`DELETE ... RETURNING`, vẫn một câu SQL) để cache biết token cần bỏ.
2. **Khóa là SHA-256 của token**, không bao giờ là token gốc (Redis có bị đọc trộm cũng không lấy được link). Giá trị là `(shareId, diagramId, role, createdAt)`.
3. **Nơi lưu theo chế độ** `diagramio.cache.mode`:
   - `local` (một instance): Caffeine trong JVM.
   - `redis`, `two-level` (nhiều instance): **chỉ Redis, không có bản trong JVM**. Mọi instance đọc cùng một chỗ, nên thay đổi có hiệu lực ngay ở request tiếp theo của bất kỳ instance nào. Đây là chỗ cố ý khác B2: mỗi request có token tốn một lệnh Redis (một script Lua) thay vì đọc RAM, đổi lại không có cửa sổ trễ cho quyền.
   - `none`, hoặc `diagramio.cache.share-tokens=false`: không cache (để đo riêng phần của B2 trên cùng một jar).
4. **Cache âm** cho token không tồn tại, sống 30 giây: một link đã thu hồi mà client còn dùng liên tục không làm ngập CSDL. Tạo link mới thì ghi đè mục của token đó.
5. **Dấu "đang đổi" trước khi đổi CSDL.** Cache-aside chỉ xóa khóa sau khi ghi có một lỗ hổng kinh điển: một request tra token đọc CSDL *trước* lúc thu hồi, nhưng ghi vào cache *sau* lúc khóa đã bị xóa, thế là token đã thu hồi sống lại trong cache tới hết thời gian sống. Với quyền truy cập, đó là lỗ hổng bảo mật. Cách chặn:
   - Trước khi đổi quyền hay thu hồi, đặt dấu `share-changing:<shareId>`, sống **đúng bằng** thời gian sống của mục dương (mặc định 5 phút).
   - Trong lúc có dấu, tra cứu link đó đi thẳng CSDL, và **lần nạp nào của link đó cũng bị từ chối** (trên Redis, kiểm tra dấu và ghi nằm trong một script Lua; trong JVM, cùng giữ một khóa).
   - Mục dương nào có từ trước lúc đặt dấu đều hết hạn trước khi dấu hết hạn. Vì vậy sau khi dấu hết hạn không thể còn bản cũ, kể cả khi bước xóa khóa sau commit thất bại. Bước xóa khóa chỉ để hết hiệu lực sớm hơn.
6. **Fail-closed khi Redis lỗi.**
   - Tra cứu: Redis lỗi hoặc bộ theo dõi của B2 báo Redis không dùng được thì đọc thẳng CSDL. Không bao giờ cho qua khi không kiểm tra được.
   - Đổi quyền, thu hồi: **không đặt được dấu thì không đổi CSDL mà trả 503** (kèm `Retry-After: 1`). Nếu vẫn thu hồi, một instance khác còn nối được Redis có thể tiếp tục cho token cũ đi qua. Thà báo rõ "chưa thu hồi được, thử lại" còn hơn báo đã thu hồi mà sai.
   - Tạo link không cần dấu (token mới chưa ai dùng); ghi cache thất bại thì chỉ mất lần trúng đầu tiên.
7. **Xóa sơ đồ**: link bị xóa theo nhờ khóa ngoại. Decorator của B2 gọi một móc trước khi xóa: đọc các link của sơ đồ (một câu SQL, chỉ khi bật cache token), rồi bỏ khóa của chúng sau commit. Token cũ có sót lại cũng không mở được gì (sơ đồ không còn, mọi thao tác trả 404), nhưng bỏ đi để cache sạch và thông báo lỗi giữ nguyên như khi không có cache.
8. **Kiểm tra quyền dùng access info từ cache của B2.** Cùng với cache token, đọc sơ đồ qua link khi trúng cả hai cache không còn câu SQL nào.
9. **Số liệu**: `diagramio_cache_share_tokens_total{result="hit"|"absent"|"miss"|"bypass"|"error"}`. Bộ đo tính tỉ lệ trúng gồm cả cache âm (`report.py`, cột "Trúng cache", phần "token").

Mô hình nhất quán:

- **Thu hồi, hạ quyền, nâng quyền**: có hiệu lực ở request tiếp theo, trên mọi instance.
- **Khi Redis lỗi**: tra cứu vẫn đúng (đọc CSDL, chậm hơn). Đổi quyền và thu hồi tạm thời bị từ chối (503) thay vì có hiệu lực trễ.

## Các phương án đã cân nhắc

| Vấn đề | Phương án | Chọn và lý do |
|---|---|---|
| Nhiều instance | L1 trong JVM + pub/sub hủy như B2; chỉ Redis | **Chỉ Redis**: pub/sub có độ trễ và có thể lỡ tin, tức là có cửa sổ trong đó token đã thu hồi vẫn dùng được; với quyền thì không chấp nhận |
| Đua giữa lần nạp chậm và thu hồi | Chỉ xóa khóa sau commit; thời gian sống ngắn; cột version trên `diagram_shares`; dấu "đang đổi" | **Dấu**: xóa khóa không chặn được lần nạp chậm; thời gian sống ngắn chỉ thu hẹp cửa sổ chứ không bỏ được; cột version cần migration và so version ở mọi nơi ghi, trong khi dấu đủ dùng mà không đổi schema |
| Redis lỗi khi thu hồi | Vẫn thu hồi, rồi xóa sạch cache token khi Redis trở lại (tăng "thế hệ" khóa); từ chối (503) | **Từ chối**: xóa sạch khi hồi phục vẫn hở khi chỉ một instance mất kết nối (instance khác vẫn dùng bản cũ trong lúc đó); từ chối đúng trong mọi trường hợp, chỉ mất khả năng đổi link trong lúc Redis lỗi |
| Cache âm chống dò token | Cache âm; giới hạn tần suất | **Cả hai, ở hai bài toán**: cache âm chỉ giúp khi cùng một token sai được gửi lại nhiều lần (link đã thu hồi). Dò bằng token ngẫu nhiên thì mỗi lần một token khác, cache âm vô ích; việc đó thuộc rate limiting (B5) |

## Hệ quả

- Redis nằm trên đường xác thực ở chế độ nhiều instance: mỗi request có token tốn một lượt Redis (localhost, dưới 1 ms) thay vì một câu SQL.
- Trong lúc Redis lỗi, chủ sơ đồ không đổi quyền hay thu hồi link được (503, thử lại); xem, sửa sơ đồ và mọi tra cứu khác vẫn chạy bằng CSDL.
- Script tra cứu đọc khóa dấu suy ra từ giá trị nên không chạy trên Redis Cluster; dự án dùng Redis đơn lẻ (docker-compose, Kaggle).
- Sau mỗi lần đổi quyền hay thu hồi, các tra cứu của đúng link đó đi CSDL trong 5 phút (thời gian sống của dấu). Thao tác này hiếm nên không đáng kể.
- Xóa sơ đồ tốn thêm một câu SQL (đọc link trước khi xóa) khi bật cache token. Đây là thao tác hiếm.

## Kiểm chứng

- `CachingShareRepositoryTest`: trúng cache, cache âm, thu hồi và đổi quyền có hiệu lực ngay, **hai cuộc đua** (lần tra cứu chậm đọc link trước khi thu hồi hoặc hạ quyền, điều phối bằng latch, không được đưa quyền cũ trở lại), link mới đè cache âm, xóa sơ đồ, fail-closed, đọc CSDL khi kho lỗi, trong transaction, khi tắt cache. Bỏ dấu "đang đổi" đi thì 3 test đỏ (đã thử), nên test thật sự bắt được lỗ hổng.
- `RedisShareTokenStoreTest` (Redis thật, Testcontainers): Lua chỉ ghi khi trống và không có dấu; dấu che mục và chặn lần nạp; cache âm sống 30 giây; token gốc không có trên Redis; Redis bị treo thì thu hồi trả 503 và không đổi CSDL, tra cứu vẫn trả lời từ CSDL.
- `ShareTokenCacheE2eTest`: đọc qua `X-Share-Token` và `GET /shares/{token}` khi trúng cache chạy **0 câu SQL, 0 lần mượn kết nối** (trước là 2 câu); thu hồi, hạ quyền, nâng quyền, xóa sơ đồ có hiệu lực ngay request tiếp theo.
- `TwoInstanceCacheE2eTest` (QA-SEC1): hai instance thật dùng chung PostgreSQL và Redis. Thu hồi hoặc hạ quyền ở A thì **request đầu tiên** ở B bị từ chối, không chờ; Redis treo thì thu hồi ở A trả 503 và token vẫn dùng được ở B, Redis trở lại thì thu hồi được và B từ chối ngay.
- Toàn bộ `./mvnw test` xanh; `SqlPerRequestE2eTest` (cache tắt) không đổi.
- Số đo Kaggle: phiên [`20261127-152353-b3`](../../bench/results/20261127-152353-b3/report.md) (C2, C3 không có cache token so với C2b3, C3b3, cùng jar, cùng phiên), mục kết quả bên dưới.

## Kết quả đo (phiên `b3`)

W2, 400 req/s, trung vị của 2 lần lặp ([chi tiết](../pha-2/ket-qua.md) mục 7.3):

| | C2 → C2b3 (trong JVM) | C3 → C3b3 (Redis) |
|---|---|---|
| SQL mỗi request | 0,84 → 0,68 (−19%) | 0,82 → 0,66 (−20%) |
| Transaction CSDL mỗi request | 0,75 → 0,59 (−21%) | 0,73 → 0,57 (−22%) |
| Trúng cache token (gồm cache âm) | 80% | 80% |
| p50 mở link, đọc kèm token (ms) | 2,0 → 1,1 ở lần lặp không bị nhiễu | 2,1 → 2,1 |
| Sức chứa W2 (p95 ≤ 100 ms) | 700 → 700 req/s | 700 → 750 req/s |

- **Đạt chỉ tiêu của kế hoạch về CSDL.** Request qua link trúng cache không chạy câu SQL nào (trước là 2 câu), và cả W2 bớt một phần năm số câu lệnh. Kết quả ổn định ở mọi lần lặp.
- **Độ trễ:** cache trong JVM giảm p50 của đường qua link gần một nửa. Với Redis, một lượt Redis thay cho một câu SQL, tốn gần như bằng nhau; đúng với hệ quả đã ghi ở trên. Lợi ích ở nhiều instance là bớt việc cho PostgreSQL, nơi mọi instance dùng chung.
- **CPU app và p95:** không kết luận được trên máy này. Hai trong ba lần chạy C2b3 trùng lúc máy ảo bị chiếm CPU (steal tới 10,6% trong khoảng 15 giây, CPU của k6 cho mỗi request tăng 9-24% dù k6 làm cùng một việc). Tùy lát cắt, chênh lệch CPU app nằm trong khoảng −7% (các lần đo sức chứa) đến +9% (chỉ các khoảng 10 giây không bị nhiễu). Flame graph cho thấy phần xác thực link rẻ đi (`ShareService` 7,6% → 3,7% số mẫu); không có điểm nóng mới.
- **Quyết định giữ nguyên:** cache token bật mặc định (`diagramio.cache.share-tokens=true`). Phiên `b4` chạy mọi cấu hình với cache token.
- **QA-SEC1: đạt**, bằng `TwoInstanceCacheE2eTest` (hai instance thật, request đầu tiên ở B sau khi thu hồi ở A bị từ chối).
