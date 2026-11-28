#!/usr/bin/env python3
"""Lấy mẫu CPU và bộ nhớ theo nhóm tiến trình, mỗi --interval giây, đọc thẳng /proc (Linux).

Nhóm: app (java chạy app.jar, mọi instance), db (postgres, gồm mọi backend), cache (redis-server), lb (nginx, B4),
loadgen (k6), collector (script đo này), other. Chạy nhiều instance (B4) thì mỗi instance còn có nhóm riêng
app_<cổng>, cộng lại bằng app. Ghi thêm CPU toàn máy (user, system, iowait, steal...) và độ bận từng CPU, để kiểm tra
bố trí CPU và phát hiện máy ảo bị chiếm CPU (steal). Chạy tới khi nhận SIGTERM/SIGINT.

CSV dạng dài: ts,metric,key,value
  cpu_cores,<nhóm>     số CPU nhóm đó dùng trung bình trong khoảng (1.0 = một CPU bận 100%)
  rss_mb,<nhóm>        bộ nhớ thường trú
  system_pct,<loại>    % thời gian của toàn bộ CPU theo loại (user, system, iowait, steal, idle...)
  cpu_busy_pct,cpuN    % bận của từng CPU
"""

from __future__ import annotations

import argparse
import os
import re
import signal
import sys
import time

CLK_TCK = os.sysconf("SC_CLK_TCK")
SERVER_PORT = re.compile(rb"--server\.port=(\d+)")
PAGE_MB = os.sysconf("SC_PAGE_SIZE") / 2**20
STAT_FIELDS = ["user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal"]

running = True


def stop(*_):
    global running
    running = False


def classify(pid: str, comm: str) -> str:
    """Nhóm của tiến trình; instance của app có dạng "app_<cổng>" (main() cộng chúng vào nhóm app)."""
    if comm == "postgres":
        return "db"
    if comm == "k6":
        return "loadgen"
    if comm.startswith("redis-server"):
        return "cache"
    if comm == "nginx":
        return "lb"
    if comm.startswith("java"):
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmdline = f.read()
        except OSError:
            return "other"
        if b"app.jar" not in cmdline:
            return "java_other"
        port = SERVER_PORT.search(cmdline)
        return f"app_{port.group(1).decode()}" if port else "app"
    if comm.startswith("python"):
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmdline = f.read()
        except OSError:
            return "other"
        return "collector" if b"bench/collect" in cmdline else "other"
    return "other"


def read_processes(cache: dict[str, str]) -> dict[str, tuple[str, int, int]]:
    """pid -> (nhóm, tổng tick CPU, rss pages). Nhóm được nhớ theo pid để không đọc lại cmdline."""
    result = {}
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/stat") as f:
                raw = f.read()
        except OSError:
            continue
        # comm nằm trong ngoặc và có thể chứa dấu cách: tách theo dấu ")" cuối cùng
        close = raw.rfind(")")
        comm = raw[raw.find("(") + 1:close]
        fields = raw[close + 2:].split()
        # sau comm: state(0) ... utime(11) stime(12) ... rss(21)
        ticks = int(fields[11]) + int(fields[12])
        rss = int(fields[21])
        group = cache.get(pid)
        if group is None:
            group = cache[pid] = classify(pid, comm)
        result[pid] = (group, ticks, rss)
    return result


def read_cpu_times() -> dict[str, list[int]]:
    times = {}
    with open("/proc/stat") as f:
        for line in f:
            if not line.startswith("cpu"):
                break
            parts = line.split()
            times[parts[0]] = [int(x) for x in parts[1:9]]
    return times


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", required=True)
    parser.add_argument("--interval", type=float, default=5.0)
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    cache: dict[str, str] = {}
    prev_proc = read_processes(cache)
    prev_cpu = read_cpu_times()
    prev_t = time.time()
    with open(args.out, "w", buffering=1) as out:
        out.write("ts,metric,key,value\n")
        while running:
            deadline = prev_t + args.interval
            while running and time.time() < deadline:
                time.sleep(min(0.2, max(0.0, deadline - time.time())))
            now = time.time()
            procs = read_processes(cache)
            cpu = read_cpu_times()
            elapsed = now - prev_t
            groups: dict[str, list[float]] = {}
            for pid, (group, ticks, rss) in procs.items():
                before = prev_proc.get(pid)
                delta = ticks - before[1] if before else ticks
                cores = max(0, delta) / CLK_TCK / elapsed
                # Một instance của app tính cả vào nhóm riêng của nó lẫn nhóm app chung
                for name in ((group, "app") if group.startswith("app_") else (group,)):
                    g = groups.setdefault(name, [0.0, 0.0])
                    g[0] += cores
                    g[1] += rss * PAGE_MB
            ts = f"{now:.3f}"
            for group in sorted(groups):
                out.write(f"{ts},cpu_cores,{group},{groups[group][0]:.3f}\n")
                out.write(f"{ts},rss_mb,{group},{groups[group][1]:.1f}\n")
            for name, values in cpu.items():
                old = prev_cpu.get(name)
                if not old:
                    continue
                deltas = [b - a for a, b in zip(old, values)]
                total = sum(deltas) or 1
                if name == "cpu":
                    for field, d in zip(STAT_FIELDS, deltas):
                        out.write(f"{ts},system_pct,{field},{100.0 * d / total:.2f}\n")
                else:
                    idle = deltas[3] + deltas[4]
                    out.write(f"{ts},cpu_busy_pct,{name},{100.0 * (total - idle) / total:.2f}\n")
            prev_proc, prev_cpu, prev_t = procs, cpu, now
            # pid đã kết thúc thì bỏ khỏi bộ nhớ đệm nhóm
            for pid in list(cache):
                if pid not in procs:
                    del cache[pid]
    return 0


if __name__ == "__main__":
    sys.exit(main())
