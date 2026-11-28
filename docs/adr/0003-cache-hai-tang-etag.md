# ADR-0003: Cache hai tầng cho đường đọc và HTTP conditional request (B2)

- Trạng thái: Đã triển khai và đo (phiên `b12`, [kết quả](../pha-2/ket-qua.md)); sửa hai lỗi phiên đo phát hiện (mục 8), đo lại ở phiên `b3` ([kết quả](../pha-2/ket-qua.md) mục 7.2): không còn 409 sai
- Mốc: B2 trong [kế hoạch pha 2](../ke-hoach-pha-2.md)
- Liên quan: [ADR-0001](0001-phuong-phap-do.md) (phương pháp đo), [ADR-0002](0002-toi-uu-truy-cap-du-lieu.md) (B1)

## Bối cảnh

Sau B1 (C1), mỗi request chỉ đọc đúng cột cần thiết, nhưng đường đọc vẫn luôn chạm CSDL. Ở W2 (85% đọc), các thao tác đọc sơ đồ (mở của chủ, mở lại, qua link, sơ đồ công khai) chiếm hơn một nửa số request, và chúng đọc cùng một nhóm sơ đồ "nóng" (phân bố Zipf). Ở C0, đọc theo id là 7% thời gian CSDL của W2 và 57% của W3 (sơ đồ 0,5-1,5 MB: 92 ms mỗi lần, phần lớn là giải nén TOAST và chuyển `jsonb` sang text). Ngoài ra:

- 10% request của W2 là client mở lại sơ đồ đã có (kèm `If-None-Match`), nhưng lần nào server cũng gửi lại toàn bộ nội dung.
- Phiên bản là snapshot bất biến nhưng vẫn đọc CSDL mỗi lần.
- Bộ đo M0 cho thấy W3 bị giới hạn bởi CPU của app khi serialize `data` (B1 đã xử lý phần serialize; phần đọc còn lại là của B2).

## Quyết định

Chiến thuật: **giữ nhiều bản sao dữ liệu** (cache) và **không gửi lại thứ client đã có** (304 Not Modified). Domain không đổi: cache là decorator trong `infrastructure`, ETag nằm ở `api`.

### 1. Decorator và các chế độ

`CachingDiagramRepository` và `CachingDiagramVersionRepository` bọc adapter SQL của B1 và được `CacheConfig` đăng ký làm bean chính của `DiagramRepository`, `DiagramVersionRepository`. `diagramio.cache.mode` chọn tầng:

| Chế độ | L1 (Caffeine, trong JVM) | L2 (Redis, dùng chung) | Dùng khi |
|---|---|---|---|
| `none` | không | không | so A/B với B1 trên cùng jar; `SqlPerRequestE2eTest` |
| `local` (mặc định) | có | không | một instance |
| `redis` | không | có | đo riêng chi phí của Redis |
| `two-level` | có | có | nhiều instance sau load balancer (B4) |

Redis chỉ được kết nối khi chế độ có Redis. L1 giới hạn theo dung lượng (mặc định 256 MB, trọng số là độ dài `data`), sống 10 phút; L2 sống 30 phút. Sơ đồ có `data` lớn hơn `max-entry-size` (mặc định 2 MB, tức là mọi sơ đồ hợp lệ) thì không cache.

### 2. Nhất quán: ghi luôn đi qua CSDL, cache chỉ nhận trạng thái mới hơn

1. **Write-through có so version.** B1 đã cho trạng thái mới ngay sau câu ghi (`UPDATE ... RETURNING`, `INSERT`, `UPDATE ... FROM` khi khôi phục). Decorator ghi trạng thái đó vào cache **sau khi transaction commit** (rollback thì bỏ), và mỗi tầng chỉ nhận nếu version lớn hơn version đang có: Caffeine so và thay trong `asMap().compute`, Redis so và thay trong một Lua script. Lần nạp từ CSDL khi trượt cache cũng đi qua phép so này, nên **một request đọc chậm (đọc CSDL trước khi người khác ghi) không thể đè bản mới**, lỗi kinh điển của cache-aside "đọc rồi put, ghi thì xóa key".
2. **Mục cache bất biến.** `Diagram` của domain sửa được (service gọi `updateMetadata`, `replaceContent`), nên cache giữ `CachedDiagram` (record bất biến) và mỗi lần đọc trả một `Diagram` mới. `DiagramData` vốn bất biến nên dùng chung được.
3. **Bia mộ khi xóa.** Xóa sơ đồ ghi một mục "đã xóa" có version lớn nhất, sống 60 giây: đọc ngay sau đó trả 404 mà không hỏi CSDL, và không lần nạp chậm nào đưa sơ đồ trở lại.
4. **UPDATE có điều kiện không khớp** (409): đọc version hiện tại từ CSDL (4 cột) và dựng sàn ở version đó (mục 8), để bản cũ hơn trong cache bị bỏ và version trả trong 409 lấy từ CSDL.
5. **Trong một transaction đang chạy, đọc thẳng CSDL**, để thấy cả những gì chính transaction đó vừa ghi (cache chỉ có trạng thái đã commit). Khóa dòng (`lockAccessInfo`) luôn đi CSDL.
6. **Gộp lần nạp**: nhiều request cùng trượt một sơ đồ trong một instance chờ chung một lần đọc CSDL (chống dồn tải khi một sơ đồ nóng hết hạn).
7. **Kiểm tra quyền không kéo `data`.** Trên Redis mỗi sơ đồ là một hash ba trường: `v` (version), `h` (chủ, công khai, tiêu đề, thời điểm), `d` (data). `findAccessInfo` chỉ đọc `v`, `h`.

Mô hình nhất quán:

- **Người vừa ghi luôn đọc được bản mới** (read-your-writes) trên instance đã ghi: cache được cập nhật trước khi response trả về.
- **Instance khác** có thể thấy bản cũ trong khoảng trễ pub/sub (mili giây). Điều này áp dụng cả cho thay đổi quyền (chuyển sơ đồ sang riêng tư): khách có thể còn đọc được sơ đồ ở instance khác trong khoảng đó. Thu hồi link chia sẻ không phụ thuộc cache này (token được tra CSDL ở mỗi request; B3 sẽ cache token với yêu cầu thu hồi có hiệu lực ngay).
- **Ghi luôn an toàn**: version được kiểm tra ở CSDL, cache sai không bao giờ làm mất một lần ghi.

### 3. Nhiều instance và khi Redis gặp sự cố

- Sau mỗi lần ghi, instance gửi `(id, version)` lên kênh pub/sub; instance khác nâng sàn của sơ đồ đó và bỏ bản L1 cũ hơn.
- **Bộ theo dõi** (`RedisCacheMonitor`) mỗi giây gửi một heartbeat qua chính kênh đó. Lệnh gửi được nghĩa là L2 dùng được; nhận lại được heartbeat gần đây của chính mình nghĩa là thông báo hủy L1 tới được nơi. Đây là kiểm tra đầu cuối: `isListening()` của Spring hay việc client Lettuce tự nối lại không đủ để biết điều đó.
- **Lệnh Redis lỗi** (hết thời gian chờ 250 ms, mất kết nối): chỉ ngừng dùng L2 tới khi bộ theo dõi thấy lệnh chạy lại được. L1 vẫn dùng, vì độ đúng của L1 chỉ phụ thuộc pub/sub. Client Redis được cấu hình từ chối lệnh ngay khi mất kết nối thay vì xếp hàng chờ. Lỗi của một lệnh gửi *trước* lần đổi trạng thái gần nhất bị bỏ qua (mục 8).
- **Mất pub/sub** (heartbeat không về quá 2 nhịp): L1 có thể đã lỡ thông báo hủy, nên ngừng dùng L1. Khi heartbeat về lại, tăng epoch của L1 (bỏ toàn bộ, kể cả mục đang được ghi dở) rồi mới dùng lại.
- **Khi L2 dùng lại được:** với mỗi sơ đồ đã đổi trong lúc mất Redis, dựng dấu sàn trên Redis (hoặc bia mộ nếu đã xóa) và báo các instance khác. Xong hết mới dùng lại L2. Như vậy không instance nào đọc lại bản cũ còn trên Redis.

### 4. Dữ liệu bất biến và micro-cache

- **Phiên bản** được cache trong JVM, không hết hạn theo thời gian, chỉ bị đẩy ra khi đầy (mặc định 128 MB). Không cần hủy khi xóa sơ đồ: service luôn kiểm tra sơ đồ còn tồn tại và quyền trước khi đọc phiên bản.
- **Danh sách công khai**: micro-cache theo trang có sẵn (`diagramio.cache.public-list-ttl`) nhưng **tắt mặc định**. Kế hoạch đề xuất 5 giây, nhưng khi bật, một sơ đồ vừa chuyển sang riêng tư vẫn còn trong danh sách công khai (tiêu đề, chủ) tới hết thời gian đó: test của pha 1 (`listPublicDiagramsAfterMadePrivateNoLongerListed`) và AGENTS.md ("không đổi hành vi một cách lặng lẽ", "không lộ dữ liệu sơ đồ riêng tư") không cho phép. Sau B1, câu danh sách công khai đã là quét index một phần, nên lợi ích còn lại không đáng đánh đổi. Bật lại khi số đo cho thấy nó là điểm nghẽn và nhóm chấp nhận độ trễ đó.

### 5. ETag và 304

- `GET /diagrams/{id}` và `PUT` trả `ETag: W/"<version>"`, `Cache-Control: no-cache, private`. Khi có `If-None-Match`, controller gọi `DiagramService.getAccessInfo` (kiểm tra quyền xem, chỉ đọc access info từ cache hoặc 4 cột từ CSDL); version khớp thì trả **304 không body**, không đọc `data`. **Quyền luôn được kiểm tra trước**: người không có quyền nhận 401/403/404 như GET bình thường, không bao giờ nhận 304, nên không dò được version của sơ đồ riêng tư.
- `GET /shares/{token}`: `ETag: W/"<version>-<role>"`, đổi quyền của link thì ETag đổi; link đã thu hồi luôn 404.
- `GET /diagrams/{id}/versions/{vid}`: `Cache-Control: private, max-age=31536000, immutable`.
- ETag là loại **yếu**: cùng một version có thể được gửi ở hai dạng byte khác nhau mà nội dung như nhau (JSON gọn như client gửi lên ngay sau PUT, hoặc dạng text đã chuẩn hóa của `jsonb` khi đọc từ CSDL). `If-None-Match` so yếu theo RFC 9110 mục 13.1.2 (bỏ `W/`, chấp nhận danh sách và `*`).

### 6. Số liệu

Micrometer: `cache_gets_total{cache="diagrams"|"versions",result="hit"|"miss"}` (Caffeine), `cache_evictions_total`, `cache_size`, `diagramio_cache_l2_total{cache="diagrams",result="hit"|"miss"|"error"}`, `diagramio_cache_l2_up`. Bộ đo đọc tỉ lệ trúng L1, L2, tỉ lệ 304 và CPU của Redis (`bench/analysis/report.py`).

### 7. Cập nhật sau rà soát ([ADR-0005](0005-ra-soat-gia-co-sau-b3.md))

- Bộ theo dõi đăng ký kênh pub/sub trong luồng của nó và thử lại ở mỗi nhịp: Redis chưa lên lúc khởi động thì app vẫn chạy bằng CSDL (trước đây app dừng hẳn).
- Một lần ghi sơ đồ chỉ tốn một lượt tới Redis: publish nằm trong script Lua ghi có so version.
- Dung lượng cache đếm đúng byte trong heap (chuỗi có ký tự ngoài Latin-1 chiếm 2 byte mỗi ký tự) và không quá 25% heap (sơ đồ), 10% heap (phiên bản).

### 8. Sửa sau phiên đo `b12`: sàn version, lỗi Redis chỉ tắt L2

Phiên `b12` có 6 lần 409 không thể xảy ra ở W2, đều ở C3 ([kết quả](../pha-2/ket-qua.md), mục 6.1). Khi CPU bão hòa lúc khởi động, lệnh Redis hết thời gian chờ. Mỗi lần như vậy cache bỏ cả L1. Lỗi của lệnh gửi trước lần hồi phục lại tắt Redis ngay sau khi vừa dùng lại (5 lần trong 25 giây). Trong lúc đó, một lần nạp chậm (đọc CSDL thấy N, trước lần ghi N+1) ghi vào L1 vừa bị bỏ: quy tắc 1 ở mục 2 không còn mục mới hơn để so, nên bản N quay lại. Sau đó PUT đúng version N+1 bị bước kiểm tra trong Java so với N và trả 409.

Bài học: "chỉ nhận bản mới hơn" chỉ an toàn khi bản mới còn nằm trong cache. Mọi chỗ bỏ mục (đầy bộ nhớ, hủy theo thông báo, 409, Redis hồi phục) đều mở lại đúng lỗi kinh điển mà quy tắc 1 định chặn. Quyết định bổ sung:

1. **Sàn version thay cho xóa.** Khi biết các version dưới `v` đã cũ (vừa ghi `v`, instance khác báo đã có `v`, câu UPDATE không khớp và CSDL đang ở `v`), cache nhớ sàn `v` của sơ đồ trong 60 giây (bằng bia mộ, dài hơn mọi lần nạp chậm). Mọi lần ghi vào L1 (nạp, chép từ L2, write-through) chỉ được nhận khi version không thấp hơn sàn. Phép kiểm tra nằm trong `compute` của khóa, nên nguyên tử với mọi thay đổi khác của khóa đó; bỏ mục cũng dùng `compute`. Bản trên L2 thấp hơn sàn coi như trượt. Người chờ chung một lần nạp mà nhận bản thấp hơn sàn thì tự đọc lại CSDL, vì lần nạp đã bắt đầu trước một lần ghi mà người đó đã thấy.
2. **Trên Redis không bao giờ xóa hẳn.** Mục bị thay bằng dấu sàn: một hash chỉ có trường `v`, đọc như trượt (`FENCE_AND_PUBLISH`, cùng script với publish). Dấu sàn chặn cả lần nạp chậm của instance khác, thứ mà sàn trong JVM không thấy.
3. **Epoch cho L1.** Bỏ toàn bộ L1 (chỉ còn xảy ra khi mất pub/sub) thì tăng epoch trước. Mỗi mục mang epoch lúc nó được ghi, và mục của epoch cũ coi như không có. Nhờ vậy một mục được ghi xong *sau* khi `clear()` đã đi qua khóa của nó cũng vô hiệu. Người ghi lấy epoch trước khi ghi CSDL, nên bản ghi trễ không vào L1 mới.
4. **Lỗi lệnh chỉ tắt L2** (mục 3). Trạng thái của L2 là một bộ đếm tăng mỗi lần đổi (lẻ là dùng được). Lỗi chỉ tắt L2 khi bộ đếm chưa đổi kể từ lúc lệnh được gửi.
5. **409 không bao giờ do cache.** `DiagramService.updateDiagram` chỉ từ chối ngay khi version gửi lên *nhỏ hơn* version đọc lúc kiểm tra quyền. Version đó có thể chậm hơn CSDL (cache, instance khác vừa ghi) nhưng không bao giờ đi trước. Mọi trường hợp khác do câu UPDATE có điều kiện quyết định. Câu UPDATE không ghi `is_public` khi người sửa không phải chủ (`CASE WHEN :changePrivacy ...`, `RETURNING is_public`), nên không còn cột nào được ghi từ thông tin có thể đã cũ. Điều này cũng cần cho B4: sau Nginx, hai lần PUT liên tiếp của một người có thể tới hai instance khác nhau trong khoảng trễ pub/sub.

Chi phí: một câu đọc 4 cột thêm cho mỗi lần UPDATE không khớp, khi có cache (chế độ `none` không đổi, nên `SqlPerRequestE2eTest` giữ nguyên). Mỗi sơ đồ vừa đổi tốn vài chục byte trong 60 giây (tối đa 100.000 sơ đồ), và mỗi dấu sàn trên Redis cũng sống 60 giây.

## Các phương án đã cân nhắc

| Vấn đề | Phương án | Chọn và lý do |
|---|---|---|
| Cập nhật cache khi ghi | Xóa key (cache-aside); ghi đè vô điều kiện; write-through có so version | **So version**: xóa key vẫn để một lần đọc chậm đưa bản cũ trở lại; ghi đè vô điều kiện để hai lần ghi chen nhau làm bản cũ thắng |
| Cách gắn cache | Spring `@Cacheable`; decorator của port | **Decorator**: `@Cacheable` không diễn đạt được phép so version, không có bia mộ, và phải đặt annotation Spring trong domain (bị `ArchitectureTest` cấm) |
| Chỉ Redis hay hai tầng | Chỉ Caffeine; chỉ Redis; hai tầng | **Hai tầng, chọn bằng cờ**: chỉ Caffeine nhanh nhất nhưng không dùng chung giữa instance; chỉ Redis tốn một lượt mạng và chép tới 2 MB mỗi lần đọc. C2 (local) và C3 (two-level) đo riêng hai chi phí |
| Hủy L1 ở instance khác | Redis keyspace notifications; client-side caching (RESP3 tracking); pub/sub tường minh | **Pub/sub tường minh** mang version: chỉ bỏ bản cũ hơn, không phụ thuộc cấu hình `notify-keyspace-events` của server, dễ kiểm thử; client tracking của Lettuce mạnh hơn nhưng phức tạp và khó quan sát |
| Phát hiện Redis hỏng | Chỉ dựa vào lỗi lệnh; `isListening()`; heartbeat qua kênh | **Heartbeat**: L1 có thể phục vụ mà không gửi lệnh Redis nào, nên lỗi lệnh không đủ; trạng thái của listener không cho biết kênh có thật sự nhận tin |
| ETag | `ShallowEtagHeaderFilter`; ETag mạnh theo version; ETag yếu theo version | **Yếu theo version**: filter vẫn phải dựng body rồi băm (chỉ tiết kiệm băng thông, không tiết kiệm CPU, và không bỏ được lần đọc `data`); ETag mạnh hứa giống từng byte, không đúng ở đây |
| Micro-cache danh sách công khai | Bật 5 giây; tắt | **Tắt mặc định**, lý do ở mục 4 |
| Chặn lần nạp chậm sau khi mục bị bỏ (mục 8) | Đếm theo nhóm khóa (striped counter) chỉ chặn lần nạp; dấu sàn ngay trong Caffeine; bảng sàn riêng | **Bảng sàn riêng** (Caffeine nhỏ, hết hạn 60 giây): chặn cả lần write-through trễ của một version cũ hơn mà bộ đếm không phân biệt được; không lẫn vào trọng số và số liệu hit/miss của L1 |
| Khi lệnh Redis lỗi (mục 8) | Bỏ cả L1 (cách cũ); chỉ tắt L2 | **Chỉ tắt L2**: độ đúng của L1 phụ thuộc pub/sub, đã có heartbeat canh. Bỏ L1 vì một lần chờ quá hạn dồn mọi lần đọc xuống CSDL đúng lúc hệ thống đang quá tải, làm lỗi tiếp diễn |

## Hệ quả

- Thêm một thành phần (Redis) cho chế độ nhiều instance và thêm độ phức tạp về nhất quán; phần phức tạp nằm gọn trong `infrastructure/cache` và có test cho từng quy tắc.
- Bộ nhớ: tới 256 MB cho sơ đồ và 128 MB cho phiên bản trong heap (bộ đo dùng heap 2 GB). Chỉnh bằng `diagramio.cache.local-max-size`, `version-max-size`.
- Khoảng trễ giữa các instance (mục 2) là đánh đổi có chủ đích giữa nhất quán và hiệu năng cho *nội dung*; với *quyền truy cập qua link* thì B3 không chấp nhận độ trễ này.
- Khi lệnh Redis lỗi, hệ thống chạy tiếp bằng L1 và CSDL; chỉ khi mất cả pub/sub mới mất L1 (chậm hơn chế độ `local`) cho tới khi heartbeat về lại: chọn đúng đắn trước, hiệu năng sau.
- Mọi đường ghi bảng `diagrams` phải đi qua `DiagramRepository` để decorator cập nhật cache (ghi trong AGENTS.md).

## Kiểm chứng

- Unit: `DiagramCacheTest` (đọc chậm không đè bản mới, điều phối bằng latch; bia mộ chặn lần nạp chậm; gộp lần nạp; mục quá lớn; 409 bỏ mục; thông báo từ instance khác), `CachingRepositoriesTest` (ghi cache chỉ sau commit, rollback không ghi, đọc trong transaction đi CSDL, khóa luôn đi CSDL, cache phiên bản), `ConditionalRequestsTest`.
- Redis thật (Testcontainers): `RedisDiagramCacheTest`: Lua giữ bản mới nhất, phần đầu không kéo data, hai cache dùng chung Redis hủy L1 của nhau, xóa và đổi quyền lan sang instance kia, **Redis bị treo** (`docker pause`) thì cả hai đọc CSDL và sau khi hồi phục không ai trả bản cũ.
- E2E: `CacheE2eTest` (ETag, 304 không body, 304 không thay cho 401/403/404, đổi quyền và xóa có hiệu lực ngay, khôi phục, link chia sẻ, phiên bản bất biến), `TwoInstanceCacheE2eTest` (hai instance thật của app dùng chung PostgreSQL và Redis: sửa, đổi quyền, xóa ở A thì B thấy; Redis treo thì cả hai vẫn trả bản mới nhất).
- Toàn bộ `./mvnw test` xanh; `SqlPerRequestE2eTest` chạy với `mode=none` nên bảng của B1 không đổi.
- Sau mục 8: `DiagramCacheTest` thêm bốn test tái hiện lỗi của phiên `b12` (đỏ trên mã cũ): lần nạp chậm sau khi bản mới rời cache, người chờ chung lần nạp cũ, write-through trễ của version cũ, write-through cũ sau thông báo của instance khác. `TwoLevelCacheStateTest` (Redis giả) có 9 test: lỗi lệnh không bỏ L1, lỗi muộn không làm hỏng lần hồi phục, mất pub/sub thì L1 làm lại từ rỗng, bản L2 dưới sàn là trượt, thay đổi trong lúc mất Redis được dựng sàn khi Redis trở lại. `RedisDiagramCacheTest` thêm Lua dấu sàn trên Redis thật và 409 ở instance A dựng sàn cho cả instance B. Gây lỗi lại (mutation) ba chỗ chính thì lần nào cũng có test đỏ.
- Số đo Kaggle (phiên `b12`, [kết quả](../pha-2/ket-qua.md) mục 4): tỉ lệ trúng L1 80%; SQL mỗi request của W2 giảm 42%; ở trạng thái gần ổn định CPU PostgreSQL giảm 18% (W2) và 53% (W3), thời gian xử lý của W3 giảm 44%; p95 W2 gần như không đổi; sức chứa theo SLO ít nhất 800 req/s (k6 là nút thắt). C3 (một instance) tốn thêm 15% CPU app mà CSDL không nhẹ thêm. Tỉ lệ 304 chưa đo đúng (bộ đo, đã sửa). C2, C3 đo lại ở phiên `b3` với mã của mục 8 ([kết quả](../pha-2/ket-qua.md) mục 7.2): không có lần 409 nào trong 28 lần chạy, dù ở 10 lần chạy có Redis vẫn có 71-268 lệnh Redis hết thời gian chờ lúc warm-up; L1 trúng 83-84%; 304 chiếm 2,5-3,0% số request (khoảng một phần tư số lần mở lại); hai tầng trên một instance vẫn tốn hơn C2 khoảng 15% CPU app.
