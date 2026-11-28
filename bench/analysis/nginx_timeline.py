#!/usr/bin/env python3
"""Dòng thời gian từng giây từ log truy cập của Nginx (format "diagramio" của deploy/nginx/http.conf), cho B4.

  nginx_timeline.py <nginx_access.log> <timeline.csv>

Mỗi dòng log: $msec $status $request_time $request_method "$upstream_addr" "$upstream_status" "$upstream_response_time".
Mỗi giây một dòng CSV:
  ts        giây (Unix) của các request kết thúc trong giây đó
  requests  số request; ok: 2xx và 304; r409; r4xx (4xx khác 409); r5xx: Nginx trả cho client (gồm 502 khi không
            instance nào trả lời được); post_5xx: phần POST, PATCH của r5xx
  retried   số request Nginx đã gửi tới hơn một instance (chuyển sang instance khác khi một instance lỗi)
  p50_ms, p99_ms, max_ms: thời gian phía Nginx
  served_<cổng>: số request do instance đó trả lời (lần thử cuối)
Log thô lớn (không commit); timeline.csv nhỏ, report.py vẽ dòng thời gian của W7 từ nó.
"""

from __future__ import annotations

import csv
import math
import re
import sys
from collections import defaultdict

LINE = re.compile(r'^(\d+)\.(\d+) (\d{3}) ([\d.]+) (\S+) "([^"]*)" "([^"]*)" "([^"]*)"')


def quantile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, math.ceil(q * len(ordered)) - 1))]


def main() -> int:
    log_path, out_path = sys.argv[1], sys.argv[2]
    seconds: dict[int, list] = defaultdict(list)
    ports: set[str] = set()
    with open(log_path, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = LINE.match(line)
            if not m:
                continue
            sec, _, status, rt, method, addrs, _statuses, _times = m.groups()
            # Nhiều lần thử: "127.0.0.1:8081, 127.0.0.1:8082"; "-" khi request không tới app; tên nhóm upstream (không
            # có cổng) khi không còn instance nào để thử
            attempts = [a for a in re.split(r", | : ", addrs) if a and a != "-"]
            last = attempts[-1].rsplit(":", 1)[-1] if attempts else ""
            port = last if last.isdigit() else ""
            if port:
                ports.add(port)
            seconds[int(sec)].append((int(status), float(rt) * 1000.0, method, port, len(attempts) > 1))
    columns = ["ts", "requests", "ok", "r409", "r4xx", "r5xx", "post_5xx", "retried", "p50_ms", "p99_ms", "max_ms"]
    columns += [f"served_{p}" for p in sorted(ports)]
    with open(out_path, "w", newline="", encoding="utf-8") as out:
        writer = csv.writer(out)
        writer.writerow(columns)
        for sec in sorted(seconds):
            rows = seconds[sec]
            times = [r[1] for r in rows]
            served = defaultdict(int)
            for r in rows:
                if r[3]:
                    served[r[3]] += 1
            writer.writerow([
                sec, len(rows),
                sum(1 for r in rows if 200 <= r[0] < 300 or r[0] == 304),
                sum(1 for r in rows if r[0] == 409),
                sum(1 for r in rows if 400 <= r[0] < 500 and r[0] != 409),
                sum(1 for r in rows if r[0] >= 500),
                sum(1 for r in rows if r[0] >= 500 and r[2] in ("POST", "PATCH")),
                sum(1 for r in rows if r[4]),
                f"{quantile(times, 0.50):.1f}", f"{quantile(times, 0.99):.1f}", f"{max(times):.1f}",
            ] + [served.get(p, 0) for p in sorted(ports)])
    return 0


if __name__ == "__main__":
    sys.exit(main())
