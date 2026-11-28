package com.diagramio.e2e;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.diagramio.DiagramioApplication;
import com.diagramio.TestDatabase;
import com.diagramio.infrastructure.cache.DiagramCache;
import com.diagramio.infrastructure.cache.RedisCacheMonitor;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import java.io.IOException;
import java.net.ConnectException;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;
import java.util.function.BooleanSupplier;
import org.apache.coyote.AbstractProtocol;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.springframework.boot.availability.ApplicationAvailability;
import org.springframework.boot.availability.LivenessState;
import org.springframework.boot.availability.ReadinessState;
import org.springframework.boot.builder.SpringApplicationBuilder;
import org.springframework.boot.web.embedded.tomcat.TomcatWebServer;
import org.springframework.boot.web.servlet.context.ServletWebServerApplicationContext;
import org.springframework.context.ConfigurableApplicationContext;
import org.testcontainers.containers.GenericContainer;

/**
 * Tắt êm (B4, ADR-0006) trên một instance thật, cache {@code two-level}: SIGTERM (ở đây là {@code close()}) trong lúc
 * một PUT đang chạy. Request đó phải xong bình thường và trạng thái mới phải tới được Redis; trong lúc chờ, instance
 * báo không nhận traffic (readiness), không nhận kết nối mới, và vẫn nhận thông báo hủy L1 (bộ theo dõi Redis dừng sau
 * web server, không phải trước).
 *
 * <p>PUT được giữ lại một cách tất định: test khóa dòng của sơ đồ trong một transaction riêng, câu UPDATE có điều kiện
 * của PUT phải chờ khóa đó.
 *
 * <p>Tắt có hai giai đoạn, test kiểm cả hai theo trạng thái thật của connector chứ không theo thời gian: cổng nghe đóng
 * trước ({@code ShutdownConfig}) và request trên kết nối đã nhận vẫn được phục vụ trong {@code accept-grace}; sau đó
 * Spring Boot tạm dừng connector và request mới trên kết nối cũ không được xử lý.
 */
class GracefulShutdownE2eTest {

    private static final ObjectMapper JSON = new ObjectMapper();
    private static final HttpClient HTTP = HttpClient.newBuilder().version(HttpClient.Version.HTTP_1_1)
            .connectTimeout(Duration.ofSeconds(5)).build();

    static final GenericContainer<?> redis = new GenericContainer<>(TestDatabase.REDIS_IMAGE).withExposedPorts(6379);

    @BeforeAll
    static void startRedis() {
        redis.start();
    }

    @AfterAll
    static void stopRedis() {
        redis.stop();
    }

    @Test
    void requestInFlightFinishesAndReachesRedisWhileTheInstanceStopsTakingTraffic() throws Exception {
        // accept-grace dài để giai đoạn đầu của lúc tắt không phụ thuộc tốc độ máy (kết nối tới cổng đã đóng trên
        // Windows mất tới vài trăm ms mới báo lỗi)
        ConfigurableApplicationContext app = instance("--diagramio.http.shutdown-accept-grace=3s");
        try {
            drainWithARequestInFlight(app);
        } finally {
            app.close();   // không làm gì nếu đã đóng; lần lỗi giữa chừng không để lại context chạy tiếp
        }
    }

    private void drainWithARequestInFlight(ConfigurableApplicationContext app) throws Exception {
        int port = ((ServletWebServerApplicationContext) app).getWebServer().getPort();
        AbstractProtocol<?> api = (AbstractProtocol<?>) ((TomcatWebServer) ((ServletWebServerApplicationContext) app)
                .getWebServer()).getTomcat().getService().findConnectors()[0].getProtocolHandler();
        DiagramCache cache = app.getBean(DiagramCache.class);
        RedisCacheMonitor monitor = app.getBean(RedisCacheMonitor.class);
        ApplicationAvailability availability = app.getBean(ApplicationAvailability.class);
        await(() -> cache.remoteUp() && cache.localTrusted(), "the cache uses Redis and L1");
        assertThat(availability.getReadinessState()).isEqualTo(ReadinessState.ACCEPTING_TRAFFIC);

        String token = login(port);
        String id = createDiagram(port, token);
        String other = createDiagram(port, token);
        assertThat(send(port, "GET", "/diagrams/" + id, token, null).statusCode()).isEqualTo(200);   // vào L1

        var postgres = E2eTestBase.postgres;
        try (Connection lock = DriverManager.getConnection(postgres.getJdbcUrl(), postgres.getUsername(),
                postgres.getPassword())) {
            lock.setAutoCommit(false);
            try (PreparedStatement select = lock.prepareStatement(
                    "SELECT id FROM diagrams WHERE id = ?::uuid FOR UPDATE")) {
                select.setString(1, id);
                select.executeQuery().close();
            }
            CompletableFuture<HttpResponse<String>> put = sendAsync(port, "PUT", "/diagrams/" + id, token,
                    updateBody(1, "Saved while shutting down"));
            awaitUpdateWaitingForTheLock();

            HttpClient keepAlive = HttpClient.newBuilder().version(HttpClient.Version.HTTP_1_1).build();
            assertThat(keepAlive.send(request(port, "GET", "/diagrams/" + other, token, null),
                    HttpResponse.BodyHandlers.ofString()).statusCode()).isEqualTo(200);   // kết nối được giữ lại

            CompletableFuture<Void> shutdown = CompletableFuture.runAsync(app::close);

            // Trong lúc chờ request: không nhận traffic mới, không nhận kết nối mới, vẫn nhận thông báo hủy L1
            await(() -> availability.getReadinessState() == ReadinessState.REFUSING_TRAFFIC, "readiness turns off");
            assertThat(availability.getLivenessState()).isEqualTo(LivenessState.CORRECT);
            await(() -> !accepts(port), "the API port stops accepting new connections");
            // Giai đoạn 1 (ShutdownConfig): cổng nghe đã đóng nhưng connector chưa tạm dừng. Kết nối đã nhận vẫn được
            // phục vụ như bình thường, để request đã tới instance không bị đóng mà không xử lý.
            assertThat(api.isPaused()).as("connector paused before the accept grace ends").isFalse();
            assertThat(keepAlive.send(request(port, "GET", "/diagrams/" + other, token, null),
                    HttpResponse.BodyHandlers.ofString()).statusCode()).isEqualTo(200);
            // Giai đoạn 2: Spring Boot tạm dừng connector và chờ các request đang chạy. Request mới gửi vào một kết nối
            // đã mở: Tomcat đóng kết nối (hoặc trả 503) mà không xử lý. Gửi lại sang instance khác vì vậy luôn an toàn,
            // nhưng Nginx không gửi lại POST, PATCH đã gửi đi: hai phương thức đó đi kết nối mới (deploy/nginx/http.conf),
            // bị từ chối ngay ở bước kết nối, trước khi gửi request.
            await(api::isPaused, "Spring Boot pauses the connector");
            assertThat(notServed(keepAlive, request(port, "PUT", "/diagrams/" + other, token,
                    updateBody(1, "Sent on a kept connection while draining")))).isTrue();
            assertThat(shutdown).isNotDone();
            assertThat(monitor.isRunning()).as("Redis monitor still running while requests drain").isTrue();
            assertThat(cache.localTrusted()).isTrue();

            lock.commit();   // nhả khóa: câu UPDATE chạy tiếp

            HttpResponse<String> response = put.get(20, TimeUnit.SECONDS);
            assertThat(response.statusCode()).as(response.body()).isEqualTo(200);
            assertThat(JSON.readTree(response.body()).path("version").asInt()).isEqualTo(2);
            shutdown.get(20, TimeUnit.SECONDS);
        }

        // Ghi xong trong lúc tắt vẫn tới được Redis: instance khác sẽ không đọc bản cũ còn trên đó
        assertThat(redisField(id, "v")).isEqualTo("2");
        assertThat(versionInDatabase(other)).isEqualTo(1);   // PUT gửi vào kết nối cũ không được xử lý
        assertThat(monitor.isRunning()).isFalse();
        assertThat(cache.localTrusted()).isFalse();
        assertThat(accepts(port)).isFalse();
    }

    @Test
    void anIdleInstanceStopsAtOnce() {
        ConfigurableApplicationContext app = instance();
        long started = System.nanoTime();

        app.close();

        assertThat(Duration.ofNanos(System.nanoTime() - started)).isLessThan(Duration.ofSeconds(10));
        assertThatThrownBy(() -> app.getBean(DiagramCache.class)).isInstanceOf(IllegalStateException.class);
    }

    // ------------------------------------------------------------------ helpers

    private static ConfigurableApplicationContext instance(String... extra) {
        var postgres = E2eTestBase.postgres;   // container dùng chung của bộ E2E
        List<String> args = new ArrayList<>(List.of(
                "--server.port=0",
                "--management.server.port=0",
                "--spring.datasource.url=" + postgres.getJdbcUrl(),
                "--spring.datasource.username=" + postgres.getUsername(),
                "--spring.datasource.password=" + postgres.getPassword(),
                "--spring.data.redis.host=" + redis.getHost(),
                "--spring.data.redis.port=" + redis.getMappedPort(6379),
                "--diagramio.cache.mode=two-level",
                "--diagramio.cache.monitor-interval=100ms"));
        args.addAll(List.of(extra));
        return new SpringApplicationBuilder(DiagramioApplication.class).run(args.toArray(String[]::new));
    }

    /**
     * Chờ tới khi câu UPDATE của PUT đang đứng chờ khóa dòng mà test giữ. Hỏi bằng kết nối riêng ở chế độ autocommit:
     * trong một transaction, PostgreSQL giữ nguyên ảnh chụp của {@code pg_stat_activity} tới hết transaction.
     */
    private static void awaitUpdateWaitingForTheLock() throws SQLException {
        var postgres = E2eTestBase.postgres;
        try (Connection observer = DriverManager.getConnection(postgres.getJdbcUrl(), postgres.getUsername(),
                postgres.getPassword())) {
            await(() -> {
                try (PreparedStatement waiting = observer.prepareStatement("""
                        SELECT count(*) FROM pg_stat_activity
                        WHERE wait_event_type = 'Lock' AND query ILIKE 'UPDATE diagrams%'""");
                     ResultSet rs = waiting.executeQuery()) {
                    rs.next();
                    return rs.getInt(1) > 0;
                } catch (SQLException e) {
                    throw new IllegalStateException(e);
                }
            }, "the PUT waits for the row lock");
        }
    }

    /** Không có response 2xx: kết nối bị đóng (IOException) hoặc 503. */
    private static boolean notServed(HttpClient client, HttpRequest request) throws InterruptedException {
        try {
            int status = client.send(request, HttpResponse.BodyHandlers.ofString()).statusCode();
            return status == 503;
        } catch (IOException e) {
            return true;
        }
    }

    private static int versionInDatabase(String id) throws SQLException {
        var postgres = E2eTestBase.postgres;
        try (Connection connection = DriverManager.getConnection(postgres.getJdbcUrl(), postgres.getUsername(),
                postgres.getPassword());
             PreparedStatement select = connection.prepareStatement("SELECT version FROM diagrams WHERE id = ?::uuid")) {
            select.setString(1, id);
            try (ResultSet rs = select.executeQuery()) {
                rs.next();
                return rs.getInt(1);
            }
        }
    }

    private static boolean accepts(int port) {
        try (Socket socket = new Socket()) {
            socket.connect(new InetSocketAddress("127.0.0.1", port), 200);
            return true;
        } catch (ConnectException e) {
            return false;
        } catch (IOException e) {
            return false;
        }
    }

    private static String redisField(String id, String field) throws Exception {
        var result = redis.execInContainer("redis-cli", "HGET", "diagramio:diagram:" + id, field);
        return result.getStdout().trim();
    }

    private static String login(int port) throws Exception {
        String user = "g" + UUID.randomUUID().toString().substring(0, 12).replace("-", "");
        String email = user + "@graceful.test";
        HttpResponse<String> registered = send(port, "POST", "/auth/register", null,
                obj("username", user, "email", email, "password", "Passw0rd!123"));
        assertThat(registered.statusCode()).as(registered.body()).isEqualTo(201);
        HttpResponse<String> login = send(port, "POST", "/auth/login", null,
                obj("email", email, "password", "Passw0rd!123"));
        assertThat(login.statusCode()).as(login.body()).isEqualTo(200);
        return JSON.readTree(login.body()).path("accessToken").asText();
    }

    private static String createDiagram(int port, String token) throws Exception {
        HttpResponse<String> created = send(port, "POST", "/diagrams", token,
                obj("title", "Graceful", "diagramType", "UML"));
        assertThat(created.statusCode()).as(created.body()).isEqualTo(201);
        return JSON.readTree(created.body()).path("id").asText();
    }

    private static ObjectNode updateBody(int version, String title) {
        ObjectNode body = obj("title", title, "diagramType", "UML", "version", version, "isPublic", false);
        body.set("data", JSON.createObjectNode().<ObjectNode>set("nodes", JSON.createArrayNode())
                .set("edges", JSON.createArrayNode()));
        return body;
    }

    private static HttpRequest request(int port, String method, String path, String token, JsonNode body) {
        HttpRequest.Builder builder = HttpRequest.newBuilder(URI.create("http://127.0.0.1:" + port + "/api/v1" + path))
                .timeout(Duration.ofSeconds(30))
                .method(method, body == null ? HttpRequest.BodyPublishers.noBody()
                        : HttpRequest.BodyPublishers.ofString(body.toString()));
        if (body != null) {
            builder.header("Content-Type", "application/json");
        }
        if (token != null) {
            builder.header("Authorization", "Bearer " + token);
        }
        return builder.build();
    }

    private static HttpResponse<String> send(int port, String method, String path, String token, JsonNode body)
            throws Exception {
        return HTTP.send(request(port, method, path, token, body), HttpResponse.BodyHandlers.ofString());
    }

    private static CompletableFuture<HttpResponse<String>> sendAsync(int port, String method, String path,
                                                                    String token, JsonNode body) {
        return HTTP.sendAsync(request(port, method, path, token, body), HttpResponse.BodyHandlers.ofString());
    }

    private static ObjectNode obj(Object... keyValues) {
        ObjectNode node = JSON.createObjectNode();
        for (int i = 0; i < keyValues.length; i += 2) {
            node.putPOJO((String) keyValues[i], keyValues[i + 1]);
        }
        return node;
    }

    private static void await(BooleanSupplier condition, String what) {
        long deadline = System.nanoTime() + Duration.ofSeconds(15).toNanos();
        while (System.nanoTime() < deadline) {
            if (condition.getAsBoolean()) {
                return;
            }
            try {
                Thread.sleep(20);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                throw new AssertionError(e);
            }
        }
        throw new AssertionError("timed out waiting until " + what);
    }
}
