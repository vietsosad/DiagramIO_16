package com.diagramio.e2e;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import java.io.IOException;
import java.net.Inet4Address;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.NetworkInterface;
import java.net.Socket;
import java.net.SocketException;
import java.util.Collections;
import java.util.Optional;
import org.junit.jupiter.api.Assumptions;
import org.junit.jupiter.api.Test;
import org.springframework.boot.test.web.server.LocalManagementPort;

/**
 * Cổng quản trị (Actuator) phục vụ phép đo pha 2: chỉ health và prometheus được mở, chỉ trên cổng
 * quản trị, cổng này chỉ nghe localhost; cổng API không lộ thêm endpoint nào.
 */
class ManagementEndpointsE2eTest extends E2eTestBase {

    @LocalManagementPort
    private int managementPort;

    private String management(String path) {
        return "http://127.0.0.1:" + managementPort + path;
    }

    @Test
    void managementServerRunsOnItsOwnPort() {
        assertThat(managementPort).isPositive().isNotEqualTo(port);
    }

    @Test
    void prometheusExposesServerSideMetricsUsedByTheBenchmark() {
        assertStatus(get(API + "/health").send(), 200);

        Res res = get(management("/actuator/prometheus")).header("Accept", "text/plain").send();

        assertStatus(res, 200);
        assertThat(res.header("Content-Type")).startsWith("text/plain");
        assertThat(res.body())
                // thời gian xử lý từng endpoint, kèm bucket để tính p95 phía server
                .contains("http_server_requests_seconds_count{")
                .contains("http_server_requests_seconds_bucket{")
                .contains("uri=\"/api/v1/health\"")
                // pool kết nối CSDL và luồng Tomcat
                .contains("hikaricp_connections_active{")
                .contains("hikaricp_connections_pending{")
                .contains("tomcat_threads_busy_threads{")
                // JVM
                .contains("jvm_memory_used_bytes{")
                .contains("process_cpu_usage");
    }

    @Test
    void managementHealthIsUp() {
        Res res = get(management("/actuator/health")).send();

        assertStatus(res, 200);
        assertThat(res.json().path("status").asText()).isEqualTo("UP");
    }

    @Test
    void livenessAndReadinessProbesAreOnTheManagementPortOnly() {
        for (String probe : new String[]{"/actuator/health/liveness", "/actuator/health/readiness"}) {
            Res res = get(management(probe)).send();
            assertStatus(res, 200);
            assertThat(res.json().path("status").asText()).as(probe).isEqualTo("UP");
            assertStatus(get(probe).send(), 401);   // cổng API: như mọi endpoint Actuator khác
        }
    }

    @Test
    void otherActuatorEndpointsAreNotExposed() {
        for (String path : new String[]{"/actuator", "/actuator/env", "/actuator/beans", "/actuator/heapdump",
                "/actuator/metrics", "/actuator/loggers"}) {
            assertThat(get(management(path)).send().status()).as(path).isIn(401, 404);
        }
    }

    @Test
    void actuatorIsNotReachableThroughTheApiPort() {
        assertStatus(get("/actuator/prometheus").send(), 401);
        assertStatus(get("/actuator/health").send(), 401);

        Actor user = registerAndLogin();
        assertStatus(get("/actuator/prometheus").as(user).send(), 404);
        assertStatus(get("/actuator/health").as(user).send(), 404);
    }

    @Test
    void managementPortListensOnLoopbackOnly() throws IOException {
        Optional<InetAddress> external = firstNonLoopbackIpv4();
        Assumptions.assumeTrue(external.isPresent(), "máy không có địa chỉ IPv4 nào ngoài loopback");

        try (Socket loopback = new Socket()) {
            loopback.connect(new InetSocketAddress("127.0.0.1", managementPort), 2000);
        }
        assertThatThrownBy(() -> {
            try (Socket outside = new Socket()) {
                outside.connect(new InetSocketAddress(external.get(), managementPort), 2000);
            }
        }).isInstanceOf(IOException.class);
    }

    private static Optional<InetAddress> firstNonLoopbackIpv4() throws SocketException {
        for (NetworkInterface nic : Collections.list(NetworkInterface.getNetworkInterfaces())) {
            if (!nic.isUp() || nic.isLoopback()) {
                continue;
            }
            for (InetAddress address : Collections.list(nic.getInetAddresses())) {
                if (address instanceof Inet4Address && !address.isLoopbackAddress() && !address.isLinkLocalAddress()) {
                    return Optional.of(address);
                }
            }
        }
        return Optional.empty();
    }
}
