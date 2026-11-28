package com.diagramio.infrastructure.cache;

import java.time.Duration;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicLong;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.web.context.WebServerGracefulShutdownLifecycle;
import org.springframework.context.SmartLifecycle;
import org.springframework.data.redis.connection.Message;
import org.springframework.data.redis.connection.RedisConnectionFactory;
import org.springframework.data.redis.listener.ChannelTopic;
import org.springframework.data.redis.listener.RedisMessageListenerContainer;

/**
 * Theo dõi Redis cho {@link DiagramCache} và nhận thông báo thay đổi từ instance khác.
 *
 * <p>Mỗi {@code interval}: có L1 thì gửi một heartbeat qua kênh pub/sub; lệnh gửi được nghĩa là L2 dùng được, nhận
 * lại được heartbeat gần đây của chính mình nghĩa là thông báo hủy L1 tới được nơi. Chỉ có L2 thì PING. Hai kết quả
 * được báo riêng cho cache ({@link DiagramCache#remoteStatus}): lệnh lỗi thì ngừng dùng L2, mất pub/sub thì ngừng dùng
 * L1, dùng lại được thì cache dọn dẹp trước khi dùng lại. Như vậy khoảng thời gian một instance có thể dùng L1 đã lỡ
 * thông báo hủy bị chặn ở vài nhịp theo dõi.
 *
 * <p>Việc đăng ký kênh pub/sub cũng chạy trong luồng theo dõi, không chạy lúc khởi động app: Redis chưa lên (hay đang
 * khởi động lại) thì app vẫn khởi động và phục vụ bằng CSDL, đăng ký được thử lại ở mỗi nhịp cho tới khi thành công.
 *
 * <p>Thứ tự khi tắt (B4): bộ theo dõi dừng <b>sau</b> web server, tức là sau cả lúc tắt êm chờ các request đang chạy,
 * và <b>trước</b> kết nối Redis (phase 0 của {@code LettuceConnectionFactory}). Với phase mặc định nó dừng đầu tiên: suốt
 * thời gian tắt êm (tới 20 giây) instance vẫn phục vụ từ L1 mà không còn nhận thông báo hủy. Dừng rồi thì cache
 * không dùng L1 nữa.
 */
public final class RedisCacheMonitor implements SmartLifecycle {

    /** Thấp hơn phase dừng web server (dừng sau nó), cao hơn phase 0 của kết nối Redis (dừng trước nó). */
    public static final int PHASE = WebServerGracefulShutdownLifecycle.SMART_LIFECYCLE_PHASE - 2048;

    private static final Logger log = LoggerFactory.getLogger(RedisCacheMonitor.class);
    /** Heartbeat nhận được phải không cũ hơn chừng này nhịp. */
    private static final long MAX_HEARTBEAT_LAG = 2;

    private final DiagramCache cache;
    private final RedisDiagramTier tier;
    private final RedisConnectionFactory connectionFactory;
    private final boolean listenForChanges;
    private final Duration interval;
    private final AtomicLong sent = new AtomicLong();
    private volatile long received;
    private volatile boolean running;
    /** Kênh pub/sub đã đăng ký được (null: chưa); sau đó nó tự đăng ký lại khi mất kết nối. */
    private volatile RedisMessageListenerContainer container;
    private ScheduledExecutorService scheduler;

    public RedisCacheMonitor(DiagramCache cache, RedisDiagramTier tier, RedisConnectionFactory connectionFactory,
                             boolean listenForChanges, Duration interval) {
        this.cache = cache;
        this.tier = tier;
        this.connectionFactory = connectionFactory;
        this.listenForChanges = listenForChanges;
        this.interval = interval;
    }

    @Override
    public synchronized void start() {
        if (running) {
            return;
        }
        scheduler = Executors.newSingleThreadScheduledExecutor(runnable -> {
            Thread thread = new Thread(runnable, "diagram-cache-monitor");
            thread.setDaemon(true);
            return thread;
        });
        scheduler.scheduleWithFixedDelay(this::check, 0, interval.toMillis(), TimeUnit.MILLISECONDS);
        running = true;
    }

    void onMessage(byte[] body) {
        Object message = RedisDiagramTier.parseMessage(body);
        if (message instanceof RedisDiagramTier.Change change) {
            cache.onRemoteChange(change);
        } else if (message instanceof RedisDiagramTier.Heartbeat heartbeat
                && heartbeat.origin().equals(cache.instanceId()) && heartbeat.sequence() > received) {
            received = heartbeat.sequence();
        }
    }

    void check() {
        if (listenForChanges && container == null) {
            subscribe();
        }
        boolean commandsOk;
        boolean subscribed = true;
        if (listenForChanges) {
            long sequence = sent.incrementAndGet();
            try {
                tier.publishHeartbeat(cache.instanceId(), sequence);
                commandsOk = true;
            } catch (RuntimeException e) {
                commandsOk = false;
            }
            subscribed = received > 0 && received >= sequence - MAX_HEARTBEAT_LAG;
        } else {
            try {
                commandsOk = tier.ping();
            } catch (RuntimeException e) {
                commandsOk = false;
            }
        }
        try {
            cache.remoteStatus(commandsOk, subscribed);
        } catch (RuntimeException e) {
            log.warn("Lỗi khi cập nhật trạng thái Redis của cache sơ đồ", e);
        }
    }

    /**
     * Đăng ký kênh thông báo thay đổi. Thất bại (Redis chưa lên, đang treo) thì bỏ container vừa tạo và để nhịp sau
     * thử lại; trong lúc đó heartbeat không về nên cache vẫn đọc CSDL.
     */
    private void subscribe() {
        RedisMessageListenerContainer candidate = new RedisMessageListenerContainer();
        candidate.setConnectionFactory(connectionFactory);
        candidate.setRecoveryInterval(interval.toMillis());
        candidate.setErrorHandler(e -> log.debug("Lỗi kênh pub/sub của cache sơ đồ: {}", e.toString()));
        candidate.addMessageListener((Message message, byte[] pattern) -> onMessage(message.getBody()),
                new ChannelTopic(RedisDiagramTier.CHANGES_CHANNEL));
        candidate.afterPropertiesSet();
        try {
            candidate.start();
            container = candidate;
            log.info("Đã đăng ký kênh thông báo thay đổi của cache sơ đồ trên Redis");
        } catch (RuntimeException e) {
            log.debug("Chưa đăng ký được kênh pub/sub của cache sơ đồ, sẽ thử lại: {}", e.toString());
            destroy(candidate);
        }
    }

    @Override
    public synchronized void stop() {
        if (!running) {
            return;
        }
        running = false;
        scheduler.shutdownNow();
        RedisMessageListenerContainer current = container;
        container = null;
        if (current != null) {
            destroy(current);
        }
        // Không còn nhận thông báo hủy: L1 không được dùng nữa (phòng khi còn request nào chạy sau lúc này)
        cache.subscriptionStopped();
    }

    @Override
    public int getPhase() {
        return PHASE;
    }

    private static void destroy(RedisMessageListenerContainer listenerContainer) {
        try {
            listenerContainer.stop();
            listenerContainer.destroy();
        } catch (Exception e) {
            log.debug("Dừng kênh pub/sub của cache sơ đồ: {}", e.toString());
        }
    }

    @Override
    public boolean isRunning() {
        return running;
    }
}
