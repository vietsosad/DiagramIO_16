package com.diagramio.e2e;

import static org.assertj.core.api.Assertions.assertThat;

import com.diagramio.DiagramioApplication;
import com.diagramio.TestDatabase;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.file.Path;
import java.time.Duration;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.stream.Collectors;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.MethodOrderer;
import org.junit.jupiter.api.Order;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestMethodOrder;
import org.springframework.boot.builder.SpringApplicationBuilder;
import org.springframework.boot.web.servlet.context.ServletWebServerApplicationContext;
import org.springframework.context.ConfigurableApplicationContext;
import org.testcontainers.containers.GenericContainer;
import org.testcontainers.containers.wait.strategy.Wait;
import org.testcontainers.images.builder.Transferable;
import org.testcontainers.utility.MountableFile;

/**
 * Nginx thật (cùng {@code deploy/nginx/http.conf}, {@code server.conf} mà docker-compose.scale.yml và bộ đo dùng)
 * đứng trước hai instance thật của app, cache {@code two-level} (B4, ADR-0006). Kiểm tra những gì chỉ đúng khi có
 * proxy: địa chỉ bên ngoài trong response, lỗi của Nginx theo khung JSON của API, và tắt êm một instance giữa lúc có
 * tải mà không mất request nào: GET, PUT đi kết nối giữ sẵn và được gửi lại nếu instance đóng kết nối; POST đi kết nối
 * mới và bị instance đang tắt từ chối ngay ở bước kết nối.
 *
 * <p>Nginx gọi app qua cổng của máy chạy test, kết nối TCP thẳng (instance đã tắt thì kết nối bị từ chối như trong
 * triển khai thật), bằng một tên riêng trỏ tới gateway IPv4 của Docker: {@code host.docker.internal} của Docker Desktop
 * có thêm một địa chỉ IPv6 không tới được, và Nginx coi mỗi địa chỉ là một instance.
 */
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class NginxFailoverE2eTest {

    private static final ObjectMapper JSON = new ObjectMapper();
    private static final String APP_HOST = "diagramio-host";
    private static final HttpClient HTTP = HttpClient.newBuilder().version(HttpClient.Version.HTTP_1_1)
            .connectTimeout(Duration.ofSeconds(5)).build();

    static final GenericContainer<?> redis = new GenericContainer<>(TestDatabase.REDIS_IMAGE).withExposedPorts(6379);
    private static GenericContainer<?> nginx;
    private static ConfigurableApplicationContext a;
    private static ConfigurableApplicationContext b;
    private static String base;
    /** Địa chỉ Nginx dùng để gọi app: tên riêng lúc đầu, rồi địa chỉ IPv4 của nó. */
    private static String appAddress = APP_HOST;

    @BeforeAll
    static void startNginxInFrontOfTwoInstances() throws Exception {
        redis.start();
        a = instance();
        b = instance();
        nginx = new GenericContainer<>("nginx:1.28-alpine")
                .withExtraHost(APP_HOST, "host-gateway")
                .withCopyFileToContainer(MountableFile.forHostPath(Path.of("deploy/nginx/http.conf")),
                        "/etc/nginx/diagramio/http.conf")
                .withCopyFileToContainer(MountableFile.forHostPath(Path.of("deploy/nginx/server.conf")),
                        "/etc/nginx/diagramio/server.conf")
                .withCopyToContainer(Transferable.of(nginxConf(port(a), port(b))), "/etc/nginx/nginx.conf")
                .withExposedPorts(8080)
                .waitingFor(Wait.forListeningPort());
        nginx.start();
        base = "http://" + nginx.getHost() + ":" + nginx.getMappedPort(8080);
        // host-gateway có thể ghi cả IPv4 lẫn IPv6 vào /etc/hosts (Docker Desktop), và Nginx coi mỗi địa chỉ là một
        // instance: ghi thẳng địa chỉ IPv4 vào cấu hình
        appAddress = nginx.execInContainer("cat", "/etc/hosts").getStdout().lines()
                .filter(line -> line.endsWith(APP_HOST) && line.split("\\s+")[0].contains("."))
                .map(line -> line.split("\\s+")[0]).findFirst()
                .orElseThrow(() -> new IllegalStateException("no IPv4 address for " + APP_HOST));
        nginx.copyFileToContainer(Transferable.of(nginxConf(port(a), port(b))), "/etc/nginx/nginx.conf");
        reloadAndAwaitOldWorkersGone();
    }

    @AfterAll
    static void stopAll() {
        for (ConfigurableApplicationContext context : new ConfigurableApplicationContext[]{a, b}) {
            if (context != null) {
                context.close();
            }
        }
        if (nginx != null) {
            nginx.stop();
        }
        redis.stop();
    }

    /** Như nginx.conf của docker-compose.scale.yml, với hai instance chạy trên máy chạy test. */
    private static String nginxConf(int portA, int portB) {
        return nginxConf(portA, portB, false);
    }

    /** {@code drainA}: A được đánh dấu {@code down}, Nginx không gửi request mới nào tới nó. */
    private static String nginxConf(int portA, int portB, boolean drainA) {
        String servers = "server %s:%d max_fails=1 fail_timeout=5s%s;\n"
                .formatted(appAddress, portA, drainA ? " down" : "")
                + "server %s:%d max_fails=1 fail_timeout=5s;\n".formatted(appAddress, portB);
        return """
                worker_processes 1;
                events { worker_connections 1024; }
                http {
                    include /etc/nginx/diagramio/http.conf;
                    access_log /dev/stdout diagramio;
                    upstream diagramio {
                        least_conn;
                        %s
                        keepalive 16;
                        keepalive_timeout 30s;
                    }
                    upstream diagramio_fresh {
                        least_conn;
                        %s
                    }
                    server {
                        listen 8080;
                        include /etc/nginx/diagramio/server.conf;
                    }
                }
                """.formatted(servers, servers);
    }

    // ------------------------------------------------------------------ tests

    @Test
    @Order(1)
    void responsesCarryTheAddressClientsUsedNotTheInstancePort() throws Exception {
        String token = login();
        HttpResponse<String> created = send("POST", "/diagrams", token, obj("title", "Behind Nginx", "diagramType", "ERD"));
        assertThat(created.statusCode()).as(created.body()).isEqualTo(201);
        assertThat(created.headers().firstValue("Location")).hasValueSatisfying(
                location -> assertThat(location).startsWith(base + "/api/v1/diagrams/"));
        String id = JSON.readTree(created.body()).path("id").asText();

        HttpResponse<String> share = send("POST", "/diagrams/" + id + "/shares", token, obj("role", "view"));

        assertThat(share.statusCode()).as(share.body()).isEqualTo(201);
        assertThat(JSON.readTree(share.body()).path("shareUrl").asText()).startsWith(base + "/");
    }

    @Test
    @Order(2)
    void oversizedBodiesGetJsonErrorsFromTheAppOrFromNginx() throws Exception {
        String token = login();
        String id = createDiagram(token);

        HttpResponse<String> overApp = send("PUT", "/diagrams/" + id, token, bigUpdate(5 * 1024 * 1024));
        HttpResponse<String> overNginx = send("PUT", "/diagrams/" + id, token, bigUpdate(9 * 1024 * 1024));

        for (HttpResponse<String> res : List.of(overApp, overNginx)) {
            assertThat(res.statusCode()).as(res.body()).isEqualTo(413);
            JsonNode error = JSON.readTree(res.body());
            assertThat(error.path("status").asInt()).isEqualTo(413);
            assertThat(error.path("message").asText()).isEqualTo("Request body must not exceed 4 MB");
            assertThat(error.path("path").asText()).isEqualTo("/api/v1/diagrams/" + id);
            assertThat(error.has("timestamp")).isTrue();
        }
    }

    /**
     * Một bước của lượt khởi động lại cuốn chiếu: đánh dấu A {@code down} và nạp lại Nginx (worker cũ làm xong request
     * dở rồi đóng các kết nối giữ sẵn tới A), chờ một chút, rồi mới tắt êm A. Không request nào được mất.
     *
     * <p>Không đánh dấu trước mà tắt ngay thì GET, PUT vẫn không bao giờ lỗi (Nginx gửi lại), nhưng một POST tới đúng
     * lúc A đóng cổng nghe có thể nhận 502: kết nối đã bắt tay xong còn nằm trong hàng đợi của nhân hệ điều hành thì bị
     * reset khi cổng đóng, sau khi Nginx đã gửi request (đo được khoảng một lần trong vài lần tắt, ADR-0006).
     */
    @Test
    @Order(3)
    void drainingAnInstanceInNginxThenStoppingItUnderLoadLosesNoRequest() throws Exception {
        AtomicBoolean running = new AtomicBoolean(true);
        Map<String, AtomicInteger> outcomes = new ConcurrentHashMap<>();
        List<String> failures = java.util.Collections.synchronizedList(new ArrayList<>());
        ExecutorService pool = Executors.newFixedThreadPool(4);
        List<Future<?>> clients = new ArrayList<>();
        for (int i = 0; i < 4; i++) {
            String token = login();
            String id = createDiagram(token);
            clients.add(pool.submit(() -> mixedTraffic(token, id, running, outcomes, failures)));
        }
        Thread.sleep(1_000);

        nginx.copyFileToContainer(Transferable.of(nginxConf(port(a), port(b), true)), "/etc/nginx/nginx.conf");
        reloadAndAwaitOldWorkersGone();   // worker mang cấu hình cũ còn có thể gửi tới A tới khi nó thoát hẳn
        a.close();   // tắt êm: SIGTERM của bước khởi động lại
        Thread.sleep(1_500);
        running.set(false);
        for (Future<?> client : clients) {
            client.get(30, TimeUnit.SECONDS);
        }
        pool.shutdown();

        if (!failures.isEmpty()) {
            nginx.getLogs().lines().filter(line -> line.contains("[error]") || line.contains("[warn]"))
                    .forEach(line -> System.out.println("NGINX " + line));
            System.out.println("ports: A=" + port(a) + " B=" + port(b));
        }
        assertThat(failures).isEmpty();
        assertThat(outcomes.keySet()).containsExactlyInAnyOrder("GET 200", "PUT 200", "POST 201");
        assertThat(outcomes.get("POST 201").get()).isPositive();
        // Log truy cập của Nginx (format "diagramio"): sau khi A tắt, lần thử cuối của mỗi request là B; request nào
        // Nginx đã gửi lại sang instance khác thì có nhiều địa chỉ upstream
        List<String> access = nginx.getLogs().lines().filter(line -> line.matches("^\\d+\\.\\d{3} \\d{3} .*")).toList();
        long retried = access.stream().filter(line -> line.contains(", ")).count();
        System.out.println("Nginx: " + access.size() + " requests, " + retried + " sent again to the other instance");
        assertThat(access.get(access.size() - 1)).contains(":" + port(b) + "\"");
    }

    @Test
    @Order(4)
    void withNoInstanceLeftNginxAnswersWithTheJsonErrorOfTheApi() throws Exception {
        String token = login();
        b.close();

        HttpResponse<String> res = send("GET", "/diagrams?page=0&size=5", token, null);

        assertThat(res.statusCode()).isEqualTo(502);
        assertThat(res.headers().firstValue("Content-Type")).hasValueSatisfying(
                type -> assertThat(type).startsWith("application/json"));
        JsonNode error = JSON.readTree(res.body());
        assertThat(error.path("status").asInt()).isEqualTo(502);
        assertThat(error.path("error").asText()).isEqualTo("Bad Gateway");
        assertThat(error.path("path").asText()).isEqualTo("/api/v1/diagrams");
    }

    // ------------------------------------------------------------------ helpers

    /**
     * Nạp lại cấu hình và chờ tới khi không còn worker nào của cấu hình cũ: worker cũ phục vụ nốt các kết nối của client
     * (tới {@code keepalive_min_timeout}) theo cấu hình cũ, nên vẫn có thể gửi tới instance vừa được đánh dấu
     * {@code down}. {@code nginx -s reload} chỉ gửi tín hiệu cho master rồi trả về ngay.
     */
    private static void reloadAndAwaitOldWorkersGone() throws Exception {
        Set<String> before = nginxWorkers();
        assertThat(nginx.execInContainer("nginx", "-s", "reload").getExitCode()).isZero();
        long deadline = System.nanoTime() + Duration.ofSeconds(15).toNanos();
        while (System.nanoTime() < deadline) {
            Set<String> now = nginxWorkers();
            if (!now.isEmpty() && Collections.disjoint(before, now)) {
                return;
            }
            Thread.sleep(100);
        }
        throw new AssertionError("an old nginx worker is still running");
    }

    /** PID của các worker Nginx, kể cả worker cũ đang tắt. */
    private static Set<String> nginxWorkers() throws Exception {
        return nginx.execInContainer("ps", "-o", "pid,args").getStdout().lines()
                .filter(line -> line.contains("worker process"))
                .map(line -> line.trim().split("\\s+")[0])
                .collect(Collectors.toSet());
    }

    /** Một client: mở sơ đồ, sửa (tự đếm version), lưu phiên bản, lặp tới khi dừng. Ghi lại mọi kết quả. */
    private static void mixedTraffic(String token, String id, AtomicBoolean running,
                                     Map<String, AtomicInteger> outcomes, List<String> failures) {
        int version = 1;
        int round = 0;
        while (running.get()) {
            try {
                record(outcomes, failures, "GET", send("GET", "/diagrams/" + id, token, null), 200);
                HttpResponse<String> put = send("PUT", "/diagrams/" + id, token, update(version, "round " + round));
                if (record(outcomes, failures, "PUT", put, 200)) {
                    version = JSON.readTree(put.body()).path("version").asInt();
                }
                record(outcomes, failures, "POST", send("POST", "/diagrams/" + id + "/versions", token,
                        obj("note", "round " + round)), 201);
                round++;
            } catch (Exception e) {
                failures.add(e.getClass().getSimpleName() + ": " + e.getMessage());
            }
        }
    }

    private static boolean record(Map<String, AtomicInteger> outcomes, List<String> failures, String method,
                                  HttpResponse<String> res, int expected) {
        outcomes.computeIfAbsent(method + " " + res.statusCode(), k -> new AtomicInteger()).incrementAndGet();
        if (res.statusCode() != expected) {
            failures.add(method + " " + res.statusCode() + " " + res.body());
            return false;
        }
        return true;
    }

    private static ConfigurableApplicationContext instance() {
        var postgres = E2eTestBase.postgres;   // container dùng chung của bộ E2E
        return new SpringApplicationBuilder(DiagramioApplication.class).run(
                "--server.port=0",
                "--management.server.port=0",
                "--spring.datasource.url=" + postgres.getJdbcUrl(),
                "--spring.datasource.username=" + postgres.getUsername(),
                "--spring.datasource.password=" + postgres.getPassword(),
                "--spring.data.redis.host=" + redis.getHost(),
                "--spring.data.redis.port=" + redis.getMappedPort(6379),
                "--diagramio.cache.mode=two-level",
                "--diagramio.cache.monitor-interval=100ms");
    }

    private static int port(ConfigurableApplicationContext context) {
        return ((ServletWebServerApplicationContext) context).getWebServer().getPort();
    }

    private static String login() throws Exception {
        String user = "n" + UUID.randomUUID().toString().substring(0, 12).replace("-", "");
        String email = user + "@nginx.test";
        HttpResponse<String> registered = send("POST", "/auth/register", null,
                obj("username", user, "email", email, "password", "Passw0rd!123"));
        assertThat(registered.statusCode()).as(registered.body()).isEqualTo(201);
        HttpResponse<String> login = send("POST", "/auth/login", null, obj("email", email, "password", "Passw0rd!123"));
        assertThat(login.statusCode()).as(login.body()).isEqualTo(200);
        return JSON.readTree(login.body()).path("accessToken").asText();
    }

    private static String createDiagram(String token) throws Exception {
        HttpResponse<String> created = send("POST", "/diagrams", token, obj("title", "Failover", "diagramType", "UML"));
        assertThat(created.statusCode()).as(created.body()).isEqualTo(201);
        return JSON.readTree(created.body()).path("id").asText();
    }

    private static ObjectNode update(int version, String title) {
        ObjectNode body = obj("title", title, "diagramType", "UML", "version", version, "isPublic", false);
        body.set("data", JSON.createObjectNode().<ObjectNode>set("nodes", JSON.createArrayNode())
                .set("edges", JSON.createArrayNode()));
        return body;
    }

    /** Body PUT hợp lệ về cấu trúc, có data cỡ {@code bytes}. */
    private static String bigUpdate(int bytes) {
        return "{\"title\":\"Big\",\"diagramType\":\"UML\",\"version\":1,\"isPublic\":false,"
                + "\"data\":{\"nodes\":[{\"id\":\"n1\",\"label\":\"" + "x".repeat(bytes) + "\"}],\"edges\":[]}}";
    }

    private static HttpResponse<String> send(String method, String path, String token, Object body) throws Exception {
        String json = body == null ? null : body instanceof String s ? s : body.toString();
        HttpRequest.Builder builder = HttpRequest.newBuilder(URI.create(base + "/api/v1" + path))
                .timeout(Duration.ofSeconds(20))
                .method(method, json == null ? HttpRequest.BodyPublishers.noBody()
                        : HttpRequest.BodyPublishers.ofString(json));
        if (json != null) {
            builder.header("Content-Type", "application/json");
        }
        if (token != null) {
            builder.header("Authorization", "Bearer " + token);
        }
        return HTTP.send(builder.build(), HttpResponse.BodyHandlers.ofString());
    }

    private static ObjectNode obj(Object... keyValues) {
        ObjectNode node = JSON.createObjectNode();
        for (int i = 0; i < keyValues.length; i += 2) {
            node.putPOJO((String) keyValues[i], keyValues[i + 1]);
        }
        return node;
    }
}
