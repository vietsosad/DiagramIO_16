package com.diagramio.config;

import java.time.Duration;
import org.apache.catalina.connector.Connector;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.web.context.WebServerApplicationContext;
import org.springframework.boot.web.context.WebServerGracefulShutdownLifecycle;
import org.springframework.boot.web.embedded.tomcat.TomcatWebServer;
import org.springframework.context.ApplicationContext;
import org.springframework.context.SmartLifecycle;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

/**
 * Tắt êm sau load balancer (B4, ADR-0006). Spring Boot tắt êm Tomcat bằng cách tạm dừng connector rồi mới đóng cổng
 * nghe. Kết nối đã nhận mà chưa kịp đọc request bị Tomcat đóng mà không xử lý; với POST, PATCH, Nginx không được phép
 * gửi lại sang instance khác, nên client nhận 502.
 *
 * <p>Bean dưới đây dừng ngay trước bước tắt êm của Spring Boot: đóng cổng nghe trước (kết nối mới bị từ chối ở bước kết
 * nối, Nginx chuyển sang instance khác an toàn với mọi phương thức), rồi chờ {@code accept-grace} để các kết nối đã
 * nhận đọc xong request và được xử lý như bình thường. Sau đó Spring Boot mới tạm dừng connector và chờ các request
 * đang chạy.
 *
 * <p>Đo bằng NginxFailoverE2eTest (4 client liên tục GET, PUT, POST qua Nginx, tắt một trong hai instance), 10 lần tắt
 * mỗi cách: mặc định của Spring Boot 7 POST nhận 502, có bean này 3. Phần còn lại là kết nối đã bắt tay xong nhưng còn
 * nằm trong hàng đợi của nhân hệ điều hành, bị reset khi cổng đóng: chỉ hết hẳn khi load balancer ngừng gửi tới instance
 * trước khi nó tắt (đánh dấu {@code down}, deploy/rolling-restart.sh): 0 lỗi trong 18 lần. GET, PUT không lỗi lần nào.
 *
 * <p>Context không có web server thật (test {@code @SpringBootTest} mặc định chạy trên servlet giả) thì bean không làm
 * gì: nó nhận {@link ApplicationContext} chứ không đòi một {@code ServletWebServerApplicationContext}, vì kiểu đó không
 * có trong các context như vậy và context sẽ không khởi động được.
 */
@Configuration
public class ShutdownConfig {

    @Bean
    public SmartLifecycle stopAcceptingBeforeDrain(ApplicationContext context,
            @Value("${diagramio.http.shutdown-accept-grace:500ms}") Duration acceptGrace) {
        return new StopAcceptingBeforeDrain(context, acceptGrace);
    }

    /** Đóng cổng nghe của API trước khi connector bị tạm dừng. */
    static final class StopAcceptingBeforeDrain implements SmartLifecycle {

        private static final Logger log = LoggerFactory.getLogger(StopAcceptingBeforeDrain.class);
        /** Dừng ngay trước bước tắt êm của Spring Boot (phase lớn hơn dừng trước). */
        static final int PHASE = WebServerGracefulShutdownLifecycle.SMART_LIFECYCLE_PHASE + 1;

        private final ApplicationContext context;
        private final Duration acceptGrace;
        private volatile boolean running;

        StopAcceptingBeforeDrain(ApplicationContext context, Duration acceptGrace) {
            this.context = context;
            this.acceptGrace = acceptGrace;
        }

        @Override
        public void start() {
            running = true;
        }

        @Override
        public void stop() {
            if (!running) {
                return;
            }
            running = false;
            if (!(context instanceof WebServerApplicationContext web)
                    || !(web.getWebServer() instanceof TomcatWebServer tomcat)) {
                return;
            }
            for (Connector connector : tomcat.getTomcat().getService().findConnectors()) {
                connector.getProtocolHandler().closeServerSocketGraceful();
            }
            log.info("Đã đóng cổng nghe của API, chờ {} ms cho các kết nối đã nhận rồi mới tắt êm",
                    acceptGrace.toMillis());
            try {
                Thread.sleep(acceptGrace.toMillis());
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
        }

        @Override
        public boolean isRunning() {
            return running;
        }

        @Override
        public int getPhase() {
            return PHASE;
        }
    }
}
