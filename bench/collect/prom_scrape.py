#!/usr/bin/env python3
"""Cào /actuator/prometheus của app mỗi --interval giây và giữ các số liệu dùng để giải thích kết quả.

Không cần dựng Prometheus: ghi thẳng CSV dạng dài ts,metric,labels,value. Chạy tới khi nhận SIGTERM/SIGINT.
Nhiều instance (B4): lặp lại --url; mỗi vòng cào hết các URL, mọi dòng của vòng đó cùng một ts, nhãn có thêm
inst=<cổng> để report.py cộng theo vòng (gauge) và theo từng chuỗi (counter, kể cả khi instance khởi động lại). Một
instance không trả lời (đã tắt, đang khởi động lại) thì vòng đó thiếu nó.
Giữ lại:
  http_server_requests_seconds_{count,sum,bucket}  gộp theo method, uri (bỏ status, outcome, exception)
  hikaricp_connections_*                           pool kết nối CSDL (active, idle, pending, max...)
  tomcat_threads_*                                 luồng xử lý request
  jvm_gc_pause_seconds_{count,sum}, jvm_memory_used_bytes (heap), jvm_threads_live_threads,
  process_cpu_usage, system_cpu_usage
  cache_*, diagramio_*                             cache sơ đồ (B2): trúng/trượt L1 (Caffeine) và L2 (Redis)
"""

from __future__ import annotations

import argparse
import re
import signal
import sys
import time
import urllib.parse
import urllib.request

KEEP_PREFIXES = ("hikaricp_connections", "tomcat_threads", "jvm_threads_live_threads", "process_cpu_usage",
                 "system_cpu_usage", "cache_gets", "cache_puts", "cache_evictions", "cache_size", "diagramio_")
KEEP_EXACT = ("jvm_gc_pause_seconds_count", "jvm_gc_pause_seconds_sum")
HTTP = ("http_server_requests_seconds_count", "http_server_requests_seconds_sum",
        "http_server_requests_seconds_bucket")
HTTP_LABELS = ("method", "uri", "le")
LINE = re.compile(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(.*)\})?\s+(\S+)')
LABEL = re.compile(r'(\w+)="((?:[^"\\]|\\.)*)"')

running = True


def stop(*_):
    global running
    running = False


def parse(text: str):
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        m = LINE.match(line)
        if not m:
            continue
        name, labels, value = m.group(1), m.group(2) or "", m.group(3)
        try:
            number = float(value)
        except ValueError:
            continue
        yield name, dict(LABEL.findall(labels)), number


def select(samples, instance: str = "") -> dict[tuple[str, str], float]:
    """Giữ số liệu cần thiết; số liệu HTTP được cộng dồn qua các nhãn bị bỏ. instance: thêm nhãn inst=<instance>."""
    rows: dict[tuple[str, str], float] = {}
    for name, labels, value in samples:
        if name in HTTP:
            key = ";".join(f"{k}={labels[k]}" for k in HTTP_LABELS if k in labels)
        elif name in KEEP_EXACT:
            key = ""
        elif name == "jvm_memory_used_bytes":
            if labels.get("area") != "heap":
                continue
            name, key = "jvm_heap_used_bytes", ""
        elif name.startswith(KEEP_PREFIXES):
            key = ";".join(f"{k}={v}" for k, v in sorted(labels.items()) if k not in ("application",))
        else:
            continue
        if instance:
            key = f"{key};inst={instance}" if key else f"inst={instance}"
        rows[(name, key)] = rows.get((name, key), 0.0) + value
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", required=True, action="append", help="lặp lại cho từng instance")
    parser.add_argument("--out", required=True)
    parser.add_argument("--interval", type=float, default=5.0)
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    # Một instance: giữ đúng dạng cũ (không nhãn inst); nhiều instance: nhãn inst là cổng quản trị
    targets = [(url, "" if len(args.url) == 1 else str(urllib.parse.urlparse(url).port))
               for url in args.url]
    failures = 0
    with open(args.out, "w", buffering=1) as out:
        out.write("ts,metric,labels,value\n")
        next_at = time.time()
        while running:
            now = time.time()
            ts = f"{now:.3f}"
            for url, instance in targets:
                request = urllib.request.Request(url, headers={"Accept": "text/plain"})
                try:
                    with urllib.request.urlopen(request, timeout=3) as response:
                        text = response.read().decode("utf-8")
                except OSError as error:
                    failures += 1
                    print(f"prom_scrape: lỗi lần {failures} ({url}): {error}", file=sys.stderr)
                    continue
                for (name, key), value in sorted(select(parse(text), instance).items()):
                    out.write(f'{ts},{name},"{key}",{value:g}\n')
            next_at += args.interval
            while running and time.time() < next_at:
                time.sleep(min(0.2, max(0.0, next_at - time.time())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
