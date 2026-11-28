package com.diagramio.infrastructure.cache;

import com.diagramio.domain.diagram.Diagram;
import com.diagramio.domain.diagram.DiagramAccessInfo;
import com.github.benmanes.caffeine.cache.Cache;
import com.github.benmanes.caffeine.cache.Caffeine;
import io.micrometer.core.instrument.Counter;
import io.micrometer.core.instrument.Gauge;
import io.micrometer.core.instrument.MeterRegistry;
import io.micrometer.core.instrument.binder.cache.CaffeineCacheMetrics;
import java.time.Duration;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.UUID;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CompletionException;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicLong;
import java.util.function.Supplier;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * Cache sơ đồ hai tầng: L1 Caffeine trong JVM, L2 Redis dùng chung. Chế độ ({@link Mode}) quyết định có tầng nào;
 * {@link Mode#NONE} là đi thẳng CSDL, để đo A/B trên cùng một file jar.
 *
 * <p>Quy tắc nhất quán (ADR-0003):
 * <ul>
 *   <li><b>Chỉ nhận bản mới hơn.</b> Mọi lần ghi vào cache (write-through sau commit, nạp từ CSDL, chép từ L2 lên
 *       L1) chỉ thay mục đang có khi version lớn hơn.</li>
 *   <li><b>Sàn version thay cho xóa.</b> Khi biết các version dưới {@code v} đã cũ (vừa ghi {@code v}, instance khác
 *       báo đã có {@code v}, câu UPDATE không khớp và CSDL đang ở {@code v}), cache nhớ sàn {@code v} của sơ đồ trong
 *       một lúc, còn Redis giữ một dấu sàn thay cho mục bị bỏ. Nhờ vậy một lần nạp chậm (đọc CSDL trước lần ghi,
 *       ghi vào cache sau khi bản mới đã rời cache) bị từ chối thay vì đưa bản cũ quay lại. Xóa sơ đồ để lại bia mộ,
 *       sàn cao nhất.</li>
 *   <li><b>Nhiều instance</b>: sau khi ghi, gửi {@code (id, version)} qua Redis pub/sub; instance khác nâng sàn và
 *       bỏ bản L1 cũ hơn. Người vừa ghi luôn đọc được bản mới; instance khác có thể thấy bản cũ trong khoảng trễ
 *       pub/sub.</li>
 *   <li><b>Lệnh Redis lỗi</b> (hết thời gian chờ, mất kết nối): chỉ ngừng dùng L2 tới khi bộ theo dõi thấy Redis
 *       trả lời lại. L1 vẫn dùng được vì độ đúng của nó chỉ phụ thuộc pub/sub. Sơ đồ đổi trong lúc đó được ghi nhớ
 *       để dựng dấu sàn trên Redis và báo cho instance khác khi Redis dùng lại được. Lỗi của một lệnh gửi trước lần
 *       đổi trạng thái gần nhất bị bỏ qua, nên không làm hỏng một lần hồi phục vừa xong.</li>
 *   <li><b>Mất pub/sub</b> (heartbeat không về): L1 có thể đã lỡ thông báo hủy nên không dùng nữa; khi heartbeat về
 *       lại, tăng epoch (bỏ toàn bộ L1, kể cả mục đang được ghi dở) rồi mới dùng lại.</li>
 *   <li><b>Gộp lần nạp</b>: nhiều request cùng trượt một sơ đồ trong một instance chỉ đọc CSDL một lần; người chờ
 *       chung nhận được bản cũ hơn sàn (lần nạp bắt đầu trước một lần ghi) thì tự đọc lại.</li>
 * </ul>
 */
public final class DiagramCache {

    public enum Mode {
        NONE, LOCAL, REDIS, TWO_LEVEL;

        public boolean usesLocal() {
            return this == LOCAL || this == TWO_LEVEL;
        }

        public boolean usesRedis() {
            return this == REDIS || this == TWO_LEVEL;
        }
    }

    private static final Logger log = LoggerFactory.getLogger(DiagramCache.class);
    /** Số sơ đồ tối đa được nhớ sàn cùng lúc (mỗi mục vài chục byte). */
    private static final int MAX_FLOORS = 100_000;

    private final Mode mode;
    private final LocalDiagramTier local;
    private final RedisDiagramTier remote;
    private final int maxDataChars;
    private final String instanceId = UUID.randomUUID().toString();
    private final ConcurrentHashMap<UUID, CompletableFuture<Optional<CachedDiagram>>> loading =
            new ConcurrentHashMap<>();
    /** Sàn của các sơ đồ vừa đổi: version nhỏ hơn sàn chắc chắn đã cũ, không được vào cache. */
    private final Cache<UUID, Integer> floors;
    /** Sơ đồ đã đổi trong CSDL mà chưa ghi được lên Redis: id, version mới nhất (bia mộ: lớn nhất). */
    private final ConcurrentHashMap<UUID, Integer> missedRemoteWrites = new ConcurrentHashMap<>();
    /** Trạng thái L2: số lẻ là dùng được; tăng 1 mỗi lần đổi. Bắt đầu ở "chưa dùng được", chờ bộ theo dõi. */
    private final AtomicLong remoteState = new AtomicLong();
    /** L1 có được dùng không: luôn đúng khi không có Redis; hai tầng thì chỉ khi heartbeat pub/sub về đều. */
    private volatile boolean localTrusted;
    /** Tăng mỗi lần bỏ toàn bộ L1; mục L1 ghi ở epoch cũ coi như không có. */
    private volatile long epoch;
    private final Counter remoteHit;
    private final Counter remoteMiss;
    private final Counter remoteError;

    /**
     * @param floorTtl thời gian nhớ sàn của một sơ đồ vừa đổi: phải dài hơn mọi lần nạp chậm (bia mộ dùng cùng giá
     *                 trị)
     */
    public DiagramCache(Mode mode, LocalDiagramTier local, RedisDiagramTier remote, int maxDataChars,
                        Duration floorTtl, MeterRegistry registry) {
        if (mode.usesLocal() != (local != null) || mode.usesRedis() != (remote != null)) {
            throw new IllegalArgumentException("cache tiers do not match mode " + mode);
        }
        this.mode = mode;
        this.local = local;
        this.remote = remote;
        this.maxDataChars = maxDataChars;
        this.localTrusted = remote == null;
        this.floors = Caffeine.newBuilder().expireAfterWrite(floorTtl).maximumSize(MAX_FLOORS).build();
        // Bộ đếm L2 chỉ có khi chế độ có Redis (mọi chỗ dùng chúng đều nằm sau kiểm tra remote != null)
        this.remoteHit = remote == null ? null : l2Counter(registry, "hit");
        this.remoteMiss = remote == null ? null : l2Counter(registry, "miss");
        this.remoteError = remote == null ? null : l2Counter(registry, "error");
        if (local != null) {
            CaffeineCacheMetrics.monitor(registry, local.caffeine(), "diagrams");
        }
        if (remote != null) {
            Gauge.builder("diagramio.cache.l2.up", this, cache -> cache.remoteUp() ? 1 : 0)
                    .tag("cache", "diagrams").description("1 khi đang dùng được Redis (L2)").register(registry);
        }
        if (local != null && remote != null) {
            Gauge.builder("diagramio.cache.l1.trusted", this, cache -> cache.localTrusted ? 1 : 0)
                    .tag("cache", "diagrams").description("1 khi L1 đang nhận đủ thông báo hủy qua pub/sub")
                    .register(registry);
        }
    }

    /** Như constructor đầy đủ, nhớ sàn 60 giây. */
    public DiagramCache(Mode mode, LocalDiagramTier local, RedisDiagramTier remote, int maxDataChars,
                        MeterRegistry registry) {
        this(mode, local, remote, maxDataChars, Duration.ofSeconds(60), registry);
    }

    private static Counter l2Counter(MeterRegistry registry, String result) {
        return Counter.builder("diagramio.cache.l2").tag("cache", "diagrams").tag("result", result)
                .description("Số lần tra Redis (L2) cho sơ đồ").register(registry);
    }

    public Mode mode() {
        return mode;
    }

    /** Tầng Redis (null nếu chế độ không có Redis), cho bộ theo dõi kết nối. */
    public RedisDiagramTier remoteTier() {
        return remote;
    }

    /** Định danh của instance này trong các thông báo pub/sub (để bỏ qua thông báo của chính mình). */
    public String instanceId() {
        return instanceId;
    }

    public boolean enabled() {
        return mode != Mode.NONE;
    }

    /** Có đang dùng được Redis (L2) không (luôn false nếu chế độ không có Redis). */
    public boolean remoteUp() {
        return isUp(remoteState.get());
    }

    /** Có đang dùng L1 không (luôn false nếu chế độ không có L1). */
    public boolean localTrusted() {
        return local != null && localTrusted;
    }

    /**
     * Epoch hiện tại. Người ghi lấy nó trước khi ghi CSDL rồi truyền cho {@link #written(Diagram, long)}: nếu L1
     * bị bỏ toàn bộ trong lúc đó thì bản ghi trễ không vào L1 nữa.
     */
    public long epoch() {
        return epoch;
    }

    // ------------------------------------------------------------------ đọc

    /** Cả sơ đồ: L1, rồi L2, rồi {@code loader} (CSDL); bản nạp được ghi vào các tầng đang dùng. */
    public Optional<Diagram> find(UUID id, Supplier<Optional<Diagram>> loader) {
        if (mode == Mode.NONE) {
            return loader.get();
        }
        long startEpoch = epoch;
        if (localUsable()) {
            CachedDiagram hit = local.get(id, startEpoch);
            if (hit != null) {
                return hit.toDiagram();
            }
        }
        long state = remoteState.get();
        if (remote != null && isUp(state)) {
            Optional<CachedDiagram> hit = remoteGet(id, state);
            if (hit.isPresent()) {
                if (localUsable()) {
                    putLocal(hit.get(), startEpoch);
                }
                return hit.get().toDiagram();
            }
        }
        return load(id, loader, startEpoch).flatMap(CachedDiagram::toDiagram);
    }

    /** Chỉ thông tin kiểm tra quyền: L1, rồi phần đầu trên L2 (không kéo data), rồi {@code loader}. */
    public Optional<DiagramAccessInfo> findAccessInfo(UUID id, Supplier<Optional<DiagramAccessInfo>> loader) {
        if (mode == Mode.NONE) {
            return loader.get();
        }
        if (localUsable()) {
            CachedDiagram hit = local.get(id, epoch);
            if (hit != null) {
                return hit.accessInfo();
            }
        }
        long state = remoteState.get();
        if (remote != null && isUp(state)) {
            try {
                Optional<Optional<DiagramAccessInfo>> hit = remote.getAccessInfo(id);
                // Bia mộ (không có thông tin) luôn mới nhất; bản dưới sàn coi như trượt
                if (hit.isPresent() && hit.get().map(info -> info.version() >= floor(id)).orElse(true)) {
                    remoteHit.increment();
                    return hit.get();
                }
                remoteMiss.increment();
            } catch (RuntimeException e) {
                remoteFailed(e, state);
            }
        }
        return loader.get();
    }

    /** Mục trên L2 nếu có và không thấp hơn sàn. */
    private Optional<CachedDiagram> remoteGet(UUID id, long state) {
        try {
            Optional<CachedDiagram> hit = remote.get(id).filter(entry -> entry.version() >= floor(id));
            (hit.isPresent() ? remoteHit : remoteMiss).increment();
            return hit;
        } catch (RuntimeException e) {
            remoteFailed(e, state);
            return Optional.empty();
        }
    }

    /** Nạp từ CSDL; các request cùng trượt một sơ đồ chờ chung một lần nạp. */
    private Optional<CachedDiagram> load(UUID id, Supplier<Optional<Diagram>> loader, long startEpoch) {
        CompletableFuture<Optional<CachedDiagram>> mine = new CompletableFuture<>();
        CompletableFuture<Optional<CachedDiagram>> running = loading.putIfAbsent(id, mine);
        if (running != null) {
            Optional<CachedDiagram> shared;
            try {
                shared = running.join();
            } catch (CompletionException e) {
                throw e.getCause() instanceof RuntimeException cause ? cause : e;
            }
            // Lần nạp đang chạy có thể đã đọc CSDL trước một lần ghi mà người gọi này đã thấy (ví dụ chính nó vừa
            // ghi): cũ hơn sàn thì tự đọc lại
            int floor = floor(id);
            if (shared.map(entry -> entry.version() >= floor).orElse(floor == 0)) {
                return shared;
            }
            Optional<CachedDiagram> reloaded = loader.get().map(CachedDiagram::of);
            reloaded.ifPresent(entry -> fill(entry, startEpoch));
            return reloaded;
        }
        try {
            Optional<CachedDiagram> loaded = loader.get().map(CachedDiagram::of);
            loaded.ifPresent(entry -> fill(entry, startEpoch));
            mine.complete(loaded);
            return loaded;
        } catch (RuntimeException e) {
            mine.completeExceptionally(e);
            throw e;
        } finally {
            loading.remove(id, mine);
        }
    }

    /**
     * Ghi bản vừa đọc từ CSDL vào các tầng (không phải thay đổi nên không thông báo cho instance khác). Bản thấp hơn
     * sàn là bản đã bị một lần ghi khác vượt qua trong lúc đọc: bỏ.
     */
    private void fill(CachedDiagram entry, long startEpoch) {
        if (entry.dataLength() > maxDataChars || entry.version() < floor(entry.id())) {
            return;
        }
        if (localUsable()) {
            putLocal(entry, startEpoch);
        }
        long state = remoteState.get();
        if (remote != null && isUp(state)) {
            try {
                remote.putIfNewer(entry);
            } catch (RuntimeException e) {
                remoteFailed(e, state);
            }
        }
    }

    /**
     * Ghi L1 nếu, ngay lúc ghi (trong compute của khóa), epoch vẫn là epoch người gọi thấy lúc bắt đầu và version
     * không thấp hơn sàn.
     */
    private void putLocal(CachedDiagram entry, long startEpoch) {
        local.putIfNewer(entry, startEpoch, () -> epoch == startEpoch && entry.version() >= floor(entry.id()));
    }

    // ------------------------------------------------------------------ ghi (gọi sau commit)

    /** Như {@link #written(Diagram, long)} với epoch hiện tại. */
    public void written(Diagram diagram) {
        written(diagram, epoch);
    }

    /**
     * Sơ đồ vừa được thêm, sửa hoặc khôi phục: nâng sàn, ghi trạng thái mới vào các tầng và báo cho instance khác.
     * {@code startEpoch} là {@link #epoch()} lấy trước khi ghi CSDL.
     */
    public void written(Diagram diagram, long startEpoch) {
        if (mode == Mode.NONE) {
            return;
        }
        CachedDiagram entry = CachedDiagram.of(diagram);
        boolean cacheable = entry.dataLength() <= maxDataChars;
        raiseFloor(entry.id(), entry.version());
        if (localUsable()) {
            if (cacheable) {
                putLocal(entry, startEpoch);
            } else {
                local.evictIfOlder(entry.id(), entry.version());
            }
        }
        if (remote != null) {
            writeRemote(entry.id(), entry.version(), cacheable
                    ? () -> remote.putIfNewerAndPublish(entry, instanceId)
                    : () -> remote.fenceAndPublish(entry.id(), entry.version(), entry.version(), instanceId));
        }
    }

    /** Như {@link #deleted(UUID, long)} với epoch hiện tại. */
    public void deleted(UUID id) {
        deleted(id, epoch);
    }

    /** Sơ đồ vừa bị xóa: ghi bia mộ vào các tầng và báo cho instance khác. */
    public void deleted(UUID id, long startEpoch) {
        if (mode == Mode.NONE) {
            return;
        }
        CachedDiagram tombstone = CachedDiagram.tombstone(id);
        raiseFloor(id, CachedDiagram.TOMBSTONE_VERSION);
        if (localUsable()) {
            putLocal(tombstone, startEpoch);
        }
        if (remote != null) {
            writeRemote(id, CachedDiagram.TOMBSTONE_VERSION,
                    () -> remote.putIfNewerAndPublish(tombstone, instanceId));
        }
    }

    /**
     * Câu UPDATE có điều kiện không khớp và CSDL cho biết version hiện tại là {@code currentVersion}: bản cũ hơn
     * trong cache (nếu có) đã sai. Nâng sàn, bỏ bản L1 cũ hơn, đặt dấu sàn trên Redis và báo cho instance khác.
     */
    public void conflict(UUID id, int currentVersion) {
        if (mode == Mode.NONE) {
            return;
        }
        raiseFloor(id, currentVersion);
        if (local != null) {
            local.evictIfOlder(id, currentVersion);
        }
        if (remote != null) {
            writeRemote(id, currentVersion,
                    () -> remote.fenceAndPublish(id, currentVersion - 1, currentVersion, instanceId));
        }
    }

    /**
     * Ghi lên Redis; Redis không dùng được (hoặc lệnh lỗi) thì nhớ lại để dựng dấu sàn khi Redis trở lại.
     * {@code version}: version mới nhất của sơ đồ sau thay đổi này.
     */
    private void writeRemote(UUID id, int version, Runnable write) {
        long state = remoteState.get();
        if (!isUp(state)) {
            missedRemoteWrites.merge(id, version, Math::max);
            state = remoteState.get();
            if (!isUp(state)) {
                return;
            }
            // Redis vừa dùng lại được giữa hai lần đọc trạng thái: lần dọn dẹp có thể đã chạy qua, tự ghi luôn
        }
        try {
            write.run();
            missedRemoteWrites.remove(id, version);
        } catch (RuntimeException e) {
            missedRemoteWrites.merge(id, version, Math::max);
            remoteFailed(e, state);
        }
    }

    // ------------------------------------------------------------------ nhiều instance, Redis lỗi

    /** Thông báo thay đổi từ instance khác (pub/sub): nâng sàn, bỏ bản L1 cũ hơn. */
    public void onRemoteChange(RedisDiagramTier.Change change) {
        if (change == null || change.origin().equals(instanceId) || mode == Mode.NONE) {
            return;
        }
        raiseFloor(change.id(), change.version());
        if (local != null) {
            local.evictIfOlder(change.id(), change.version());
        }
    }

    /**
     * Kết quả một nhịp của bộ theo dõi: {@code commandsOk} khi lệnh tới Redis chạy được (heartbeat hoặc PING),
     * {@code subscribed} khi đang nhận được pub/sub (heartbeat của chính mình quay về kịp; chỉ có ý nghĩa khi có L1).
     */
    public synchronized void remoteStatus(boolean commandsOk, boolean subscribed) {
        if (remote == null) {
            return;
        }
        if (local != null && !subscribed && localTrusted) {
            localTrusted = false;
            resetLocal();
            log.warn("Mất pub/sub của cache sơ đồ: ngừng dùng L1 tới khi heartbeat về lại");
        }
        long state = remoteState.get();
        if (!commandsOk) {
            if (isUp(state) && remoteState.compareAndSet(state, state + 1)) {
                log.warn("Ngừng dùng Redis (L2) cho cache sơ đồ: không gửi được lệnh tới Redis");
            }
        } else if (!isUp(state) && drainMissedWrites() && remoteState.compareAndSet(state, state + 1)) {
            // Người ghi thấy "chưa dùng được" ngay trước lúc chuyển: dọn nốt
            drainMissedWrites();
            log.info("Redis (L2) dùng lại được, cache sơ đồ hoạt động lại ở chế độ {}", mode);
        }
        if (local != null && subscribed && !localTrusted) {
            resetLocal();
            localTrusted = true;
            log.info("Pub/sub của cache sơ đồ thông suốt: dùng L1 từ trạng thái rỗng");
        }
    }

    /** Bộ theo dõi đã dừng (app đang tắt): không còn ai nhận thông báo hủy, nên thôi dùng L1. */
    public synchronized void subscriptionStopped() {
        if (local != null && remote != null && localTrusted) {
            localTrusted = false;
            resetLocal();
            log.info("Đã dừng nhận pub/sub của cache sơ đồ: ngừng dùng L1");
        }
    }

    /** Lệnh Redis gửi ở trạng thái {@code state} bị lỗi: ngừng dùng L2, trừ khi trạng thái đã đổi kể từ đó. */
    private void remoteFailed(RuntimeException e, long state) {
        remoteError.increment();
        if (isUp(state) && remoteState.compareAndSet(state, state + 1)) {
            log.warn("Ngừng dùng Redis (L2) cho cache sơ đồ, vẫn dùng L1: {}: {}", e.getClass().getSimpleName(),
                    e.getMessage());
        }
    }

    /** Bỏ toàn bộ L1: tăng epoch trước (mục đang được ghi dở cũng vô hiệu), rồi giải phóng bộ nhớ. */
    private void resetLocal() {
        epoch++;
        local.clear();
    }

    /**
     * Dựng trên Redis dấu sàn cho các sơ đồ đã đổi trong lúc mất Redis (bia mộ nếu đã xóa) và báo cho instance khác
     * bỏ bản L1 cũ hơn; false nếu lỗi.
     */
    private boolean drainMissedWrites() {
        for (Map.Entry<UUID, Integer> missed : List.copyOf(missedRemoteWrites.entrySet())) {
            UUID id = missed.getKey();
            int version = missed.getValue();
            try {
                if (version == CachedDiagram.TOMBSTONE_VERSION) {
                    remote.putIfNewerAndPublish(CachedDiagram.tombstone(id), instanceId);
                } else {
                    remote.fenceAndPublish(id, version - 1, version, instanceId);
                }
                missedRemoteWrites.remove(id, version);
            } catch (RuntimeException e) {
                remoteError.increment();
                return false;
            }
        }
        return true;
    }

    private int floor(UUID id) {
        Integer floor = floors.getIfPresent(id);
        return floor == null ? 0 : floor;
    }

    private void raiseFloor(UUID id, int version) {
        floors.asMap().merge(id, version, Math::max);
    }

    private boolean localUsable() {
        return local != null && localTrusted;
    }

    private static boolean isUp(long state) {
        return (state & 1) == 1;
    }
}
