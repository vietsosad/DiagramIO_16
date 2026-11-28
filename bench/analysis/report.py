#!/usr/bin/env python3
"""Tổng hợp một phiên đo thành report.md, summary.csv và biểu đồ trong charts/.

    python3 bench/analysis/report.py bench/results/<phiên>

Với mỗi lần chạy (thư mục có meta.json): lấy số liệu k6 của pha đo, cắt số liệu phía server (procstat.csv,
prom.csv) đúng cửa sổ đo nhờ thời điểm bắt đầu kịch bản mà k6 ghi lại, quy thống kê PostgreSQL ra trên mỗi
request. Các lần lặp của cùng một phép đo được gộp bằng trung vị kèm min-max; lần chạy có profiler hoặc bị lỗi
không được gộp. Báo cáo kèm phần kiểm tra tính hợp lệ của phép đo (máy tạo tải không bão hòa, máy ảo không bị
chiếm CPU, đạt tốc độ đến yêu cầu, không có 409 giả tạo...).
Chỉ dùng thư viện chuẩn; biểu đồ cần matplotlib (thiếu thì bỏ qua biểu đồ).
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
import sys
from collections import defaultdict
from pathlib import Path

SLO_ERROR_RATE = 0.01         # lỗi dưới 1%
SLO_ACHIEVED = 0.95           # đạt ít nhất 95% tốc độ đến yêu cầu
LOADGEN_CPU_LIMIT = 0.80      # k6 dùng dưới 80% CPU được cấp
STEAL_LIMIT_PCT = 5.0
DROPPED_LIMIT = 0.005         # bỏ dưới 0,5% iteration (thường là lúc k6 đang khởi tạo thêm VU) thì không đáng kể
WARM_DRIFT_LIMIT = 0.25       # CPU app đầu pha đo cao hơn cuối pha đo quá 25%: JVM chưa ấm

# Bảng màu phân loại theo thứ tự cố định (đã kiểm tra mù màu cho 3 màu đầu), mực chữ và nền biểu đồ
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
OTHER = "#898781"
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"


# ----------------------------------------------------------------------------- đọc dữ liệu

def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def num(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class Run:
    def __init__(self, directory: Path):
        self.dir = directory
        self.meta = json.loads((directory / "meta.json").read_text(encoding="utf-8"))
        summary_path = directory / "k6-summary.json"
        self.summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else {}
        self.metrics = self.summary.get("metrics", {})
        self.config = self.summary.get("config", {})
        self.mode = self.meta["mode"]
        self.workload = self.meta["workload"]
        self.level = self.meta["level"]
        self.cfg = self.meta["config"]
        self.rep = str(self.meta["rep"])
        start_ms = self.value("scenario_start_ms", "value")
        self.start = start_ms / 1000.0 if start_ms else None
        self.warmup = self.meta.get("warmup_s", 0)
        self.measure = self.meta.get("measure_s", 0)
        self.proc = self._read_proc()
        self.prom = self._read_prom()

    # -- k6
    def value(self, metric: str, key: str, default=None):
        return self.metrics.get(metric, {}).get("values", {}).get(key, default)

    def ops(self) -> list[str]:
        found = []
        for name in self.metrics:
            m = re.match(r"http_req_duration\{phase:measure,op:([^}]+)\}", name)
            if m:
                found.append(m.group(1))
        return found

    # -- số liệu phía server
    def _read_proc(self):
        rows = []
        for r in read_csv(self.dir / "procstat.csv"):
            rows.append((num(r["ts"]), r["metric"], r["key"], num(r["value"])))
        return rows

    def _read_prom(self):
        series = defaultdict(list)
        for r in read_csv(self.dir / "prom.csv"):
            series[(r["metric"], r["labels"])].append((num(r["ts"]), num(r["value"])))
        return series

    @property
    def window(self):
        if self.start is None:
            return None
        return self.start + self.warmup, self.start + self.warmup + self.measure

    def proc_mean(self, metric: str, key: str, window) -> float | None:
        values = [v for ts, m, k, v in self.proc if m == metric and k == key and window[0] < ts <= window[1] + 0.5]
        return statistics.fmean(values) if values else None

    def proc_max(self, metric: str, key: str, window) -> float | None:
        values = [v for ts, m, k, v in self.proc if m == metric and k == key and window[0] < ts <= window[1] + 0.5]
        return max(values) if values else None

    def gauge(self, metric: str, window, agg=statistics.fmean) -> float | None:
        """Gộp mọi mẫu của mọi chuỗi (với nhiều instance: giá trị của từng instance, ví dụ heap lớn nhất)."""
        values = [v for (m, _), points in self.prom.items() if m == metric
                  for ts, v in points if window[0] <= ts <= window[1]]
        return agg(values) if values else None

    def gauge_total(self, metric: str, window, agg=statistics.fmean) -> float | None:
        """Cộng các chuỗi của cùng một vòng cào (mọi instance, cùng ts) rồi gộp theo thời gian: tổng số kết nối
        CSDL, luồng Tomcat của cả hệ thống. Một instance thì như gauge()."""
        per_round: dict[float, float] = defaultdict(float)
        for (m, _), points in self.prom.items():
            if m == metric:
                for ts, v in points:
                    if window[0] <= ts <= window[1]:
                        per_round[ts] += v
        return agg(per_round.values()) if per_round else None

    def counter_delta(self, metric: str, window, label_filter=lambda labels: True) -> float | None:
        """Phần tăng của bộ đếm cộng dồn trong cửa sổ, cộng qua các chuỗi nhãn (và các instance). Giá trị giảm
        nghĩa là instance đã khởi động lại và đếm lại từ 0 (W7): phần tăng từ đó là chính giá trị mới."""
        total, found = 0.0, False
        for (m, labels), points in self.prom.items():
            if m != metric or not label_filter(labels):
                continue
            inside = [v for ts, v in points if window[0] <= ts <= window[1]]
            if len(inside) >= 2:
                total += sum(b - a if b >= a else b for a, b in zip(inside, inside[1:]))
                found = True
        return total if found else None

    def prom_span(self, window) -> float | None:
        stamps = sorted({ts for points in self.prom.values() for ts, _ in points if window[0] <= ts <= window[1]})
        return stamps[-1] - stamps[0] if len(stamps) >= 2 else None

    def server_latency(self, window):
        """Thời gian xử lý trung bình và p95 phía server (ms) từ histogram http.server.requests."""
        not_actuator = lambda labels: "uri=/actuator" not in labels   # noqa: E731
        count = self.counter_delta("http_server_requests_seconds_count", window, not_actuator)
        total = self.counter_delta("http_server_requests_seconds_sum", window, not_actuator)
        mean = 1000.0 * total / count if count else None
        buckets = defaultdict(float)
        for (m, labels), points in self.prom.items():
            if m != "http_server_requests_seconds_bucket" or not not_actuator(labels):
                continue
            le = re.search(r"le=([^;]+)", labels)
            inside = [v for ts, v in points if window[0] <= ts <= window[1]]
            if le and len(inside) >= 2:
                buckets[le.group(1)] += sum(b - a if b >= a else b for a, b in zip(inside, inside[1:]))
        return mean, histogram_quantile(buckets, 0.95)


def histogram_quantile(buckets: dict[str, float], q: float) -> float | None:
    """Phân vị từ bucket cộng dồn kiểu Prometheus (nội suy tuyến tính trong bucket), đơn vị ms."""
    if not buckets:
        return None
    bounds = sorted(((float("inf") if le == "+Inf" else float(le)), c) for le, c in buckets.items())
    total = bounds[-1][1]
    if total <= 0:
        return None
    rank = q * total
    prev_le, prev_c = 0.0, 0.0
    for le, c in bounds:
        if c >= rank:
            if le == float("inf"):
                return 1000.0 * prev_le
            if c == prev_c:
                return 1000.0 * le
            return 1000.0 * (prev_le + (le - prev_le) * (rank - prev_c) / (c - prev_c))
        prev_le, prev_c = le, c
    return None


# ----------------------------------------------------------------------------- tính số liệu một lần chạy

def pg_totals(run: Run) -> dict:
    db = (read_csv(run.dir / "pg_stat_database.csv") or [{}])[0]
    statements = read_csv(run.dir / "pg_stat_statements.csv")
    statio = read_csv(run.dir / "pg_statio_user_tables.csv")
    bgwriter = (read_csv(run.dir / "pg_stat_bgwriter.csv") or [{}])[0]
    return {
        "calls": sum(num(s["calls"]) for s in statements),
        "exec_ms": sum(num(s["total_exec_ms"]) for s in statements),
        "commits": num(db.get("xact_commit")),
        "blocks": num(db.get("blks_hit")) + num(db.get("blks_read")),
        "blocks_read": num(db.get("blks_read")),
        "toast": sum(num(r.get("toast_blks_hit")) + num(r.get("toast_blks_read")) for r in statio),
        "checkpoints": num(bgwriter.get("checkpoints_timed")) + num(bgwriter.get("checkpoints_req")),
        "statements": statements,
    }


def run_metrics(run: Run) -> dict:
    m: dict = {"run": run.meta["run"], "config": run.cfg, "workload": run.workload, "mode": run.mode,
               "level": run.level, "rep": run.rep, "status": run.meta["status"], "profiled": run.meta["profiled"],
               "k6_exit": run.meta["k6_exit"], "git_sha": run.meta.get("git_sha"),
               # B4: số instance sau Nginx (0: một app nghe thẳng), sự cố của W7 (kill, rolling)
               "instances": run.meta.get("instances") or 0, "incident": run.meta.get("incident"),
               "heap_mb_per_instance": run.meta.get("heap_mb_per_instance"),
               "pool_per_instance": run.meta.get("pool_per_instance")}
    d = "http_req_duration{phase:measure}"
    count = run.value("http_reqs{phase:measure}", "count", 0) or 0
    m.update(count=count, p50=run.value(d, "med"), p95=run.value(d, "p(95)"), p99=run.value(d, "p(99)"),
             avg=run.value(d, "avg"), err=run.value("http_req_failed{phase:measure}", "rate", 0.0))
    m["rps"] = count / run.measure if run.mode != "capacity" and run.measure else None
    m["measure_s"] = run.warmup + run.measure   # iteration bị bỏ được đếm trên cả lần chạy
    m["target"] = num(run.level) if run.mode == "fixed" else None
    for key, metric in (("r304", "resp_304"), ("r409", "resp_409"), ("r4xx", "resp_4xx_other"),
                        ("r5xx", "resp_5xx"), ("rnone", "resp_no_response"), ("conflicts", "write_conflicts"),
                        ("fallbacks", "op_fallbacks"), ("dropped", "dropped_iterations")):
        m[key] = run.value(metric, "count", 0) or 0
    m["vus_max"] = run.value("vus_max", "value")
    # Thao tác không có request nào trong pha đo (ví dụ đăng nhập của W1, chỉ xảy ra lúc warm-up) bị bỏ qua
    m["ops"] = {op: {"count": run.value(f"http_reqs{{phase:measure,op:{op}}}", "count", 0),
                     "p95": run.value(f"http_req_duration{{phase:measure,op:{op}}}", "p(95)"),
                     "p99": run.value(f"http_req_duration{{phase:measure,op:{op}}}", "p(99)"),
                     "err": run.value(f"http_req_failed{{phase:measure,op:{op}}}", "rate", 0.0)}
                for op in run.ops() if run.value(f"http_reqs{{phase:measure,op:{op}}}", "count", 0)}

    window = run.window
    if window and run.mode != "capacity":
        sut = [f"cpu{c}" for c in str(run.meta.get("sut_cpus", "")).split(",") if c != ""]
        # app: tổng các instance; lb: Nginx (B4)
        for group in ("app", "db", "cache", "loadgen", "lb"):
            m[f"cpu_{group}"] = run.proc_mean("cpu_cores", group, window)
        m["loadgen_max"] = run.proc_max("cpu_cores", "loadgen", window)
        m["steal"] = run.proc_mean("system_pct", "steal", window)
        busy = [run.proc_mean("cpu_busy_pct", c, window) for c in sut]
        busy = [b for b in busy if b is not None]
        m["sut_busy"] = statistics.fmean(busy) if busy else None
        m["srv_mean"], m["srv_p95"] = run.server_latency(window)
        # Tổng của mọi instance ở cùng một thời điểm
        m["hikari_active"] = run.gauge_total("hikaricp_connections_active", window)
        m["hikari_pending_max"] = run.gauge_total("hikaricp_connections_pending", window, max)
        m["tomcat_busy"] = run.gauge_total("tomcat_threads_busy_threads", window)
        m["tomcat_busy_max"] = run.gauge_total("tomcat_threads_busy_threads", window, max)
        gc = run.counter_delta("jvm_gc_pause_seconds_sum", window)
        span = run.prom_span(window)
        m["gc_ms_per_s"] = 1000.0 * gc / span if gc is not None and span else None
        heap = run.gauge("jvm_heap_used_bytes", window, max)
        m["heap_max_mb"] = heap / 2**20 if heap else None
        # Tỉ lệ trúng cache sơ đồ (B2): Caffeine (L1) và Redis (L2), None nếu cấu hình không có cache
        for level, metric in (("l1", "cache_gets_total"), ("l2", "diagramio_cache_l2_total")):
            hits = run.counter_delta(metric, window, lambda labels: "cache=diagrams" in labels and "result=hit" in labels)
            misses = run.counter_delta(metric, window, lambda labels: "cache=diagrams" in labels and "result=miss" in labels)
            m[f"hit_{level}"] = hits / (hits + misses) if hits is not None and misses is not None and hits + misses else None
        # Cache token chia sẻ (B3): trúng gồm cả cache âm; "bypass" (link đang được đổi) tính là trượt
        token = {r: run.counter_delta("diagramio_cache_share_tokens_total", window,
                                      lambda labels, r=r: f"result={r}" in labels)
                 for r in ("hit", "absent", "miss", "bypass")}
        if all(v is not None for v in token.values()) and sum(token.values()):
            m["hit_token"] = (token["hit"] + token["absent"]) / sum(token.values())
        # JVM chưa ấm hết sau warm-up (JIT còn biên dịch): ở cùng tốc độ đến, CPU app của phần ba đầu pha đo cao hơn
        # hẳn phần ba cuối (phiên b12: C1 ở 400 req/s dùng 0,93 rồi 0,56 nhân)
        third = (window[1] - window[0]) / 3
        first = run.proc_mean("cpu_cores", "app", (window[0], window[0] + third))
        last = run.proc_mean("cpu_cores", "app", (window[1] - third, window[1]))
        m["cpu_app_drift"] = first / last - 1 if first and last else None
    if run.start is not None:
        # Lệnh Redis (L2) lỗi trong cả lần chạy, kể cả warm-up (lỗi lúc warm-up vẫn có thể để lại hậu quả)
        whole = (run.start - 30, run.start + run.warmup + run.measure + 30)
        is_error = lambda labels: "cache=diagrams" in labels and "result=error" in labels   # noqa: E731
        m["l2_errors"] = run.counter_delta("diagramio_cache_l2_total", whole, is_error)
        m["l2_errors_measure"] = run.counter_delta("diagramio_cache_l2_total", window, is_error) if window else None

    pg = pg_totals(run)
    total_requests = run.value("http_reqs", "count", 0) or 0
    if total_requests:
        m["sql_per_req"] = pg["calls"] / total_requests
        m["commits_per_req"] = pg["commits"] / total_requests
        m["blocks_per_req"] = pg["blocks"] / total_requests
        m["toast_per_req"] = pg["toast"] / total_requests
        m["db_ms_per_req"] = pg["exec_ms"] / total_requests
    m["blocks_read"] = pg["blocks_read"]
    m["checkpoints"] = pg["checkpoints"]
    m["top_sql"] = pg["statements"][:8]
    m["total_requests"] = total_requests
    # resp_304 đếm cả warm-up, nên chia cho tổng request của cả lần chạy (cùng phạm vi)
    m["share_304"] = 100.0 * m["r304"] / total_requests if total_requests else None

    if run.mode == "capacity":
        m["steps"] = capacity_steps(run)
        slo = num(run.config.get("sloP95Ms"), 100.0)
        m["slo_p95"] = slo
        m["capacity"] = capacity_of(m["steps"], slo)
    return m


def capacity_steps(run: Run) -> list[dict]:
    steps = []
    step = run.config.get("step", 30)
    settle = run.config.get("settle", 5)
    seconds = step - 1 - settle
    for s in run.summary.get("steps", []):
        tags = f"stage:{s['stage']},phase:measure"
        count = run.value(f"http_reqs{{{tags}}}", "count", 0) or 0
        if count == 0:
            continue
        row = {"stage": s["stage"], "target": s["rate"], "count": count, "achieved": count / seconds,
               "p95": run.value(f"http_req_duration{{{tags}}}", "p(95)"),
               "p99": run.value(f"http_req_duration{{{tags}}}", "p(99)"),
               "err": run.value(f"http_req_failed{{{tags}}}", "rate", 0.0)}
        if run.start is not None:
            window = (run.start + s["start"] + 1 + settle, run.start + s["end"])
            row["cpu_app"] = run.proc_mean("cpu_cores", "app", window)
            row["cpu_db"] = run.proc_mean("cpu_cores", "db", window)
            row["loadgen_max"] = run.proc_max("cpu_cores", "loadgen", window)
        steps.append(row)
    return steps


def step_passes(step: dict, slo: float) -> bool:
    return (step["p95"] is not None and step["p95"] <= slo and step["err"] < SLO_ERROR_RATE
            and step["achieved"] >= SLO_ACHIEVED * step["target"])


def level_passes(ms: list[dict], slo: float = 100.0) -> bool:
    """Các lần lặp của một mức tải cố định: trung vị p95 giữ SLO và đạt tốc độ đến yêu cầu."""
    p95 = spread([m.get("p95") for m in ms])
    rps = spread([m.get("rps") for m in ms])
    target = ms[0].get("target") if ms else None
    return (p95 is not None and p95[0] <= slo
            and (target is None or (rps is not None and rps[0] >= SLO_ACHIEVED * target)))


def capacity_of(steps: list[dict], slo: float) -> float | None:
    """Tốc độ của bậc cuối cùng còn đạt SLO, tính liên tục từ bậc đầu (bậc lỗi đầu tiên chặn các bậc sau)."""
    best = None
    for step in steps:
        if not step_passes(step, slo):
            break
        best = step["target"]
    return best


# ----------------------------------------------------------------------------- kiểm tra hợp lệ

def validity(m: dict) -> list[str]:
    issues = []
    if m["status"] != "ok":
        issues.append("không chạy xong (app hoặc bộ đo lỗi, xem app.log và k6.log)" if m["k6_exit"] == -1
                      else f"k6 thoát với mã {m['k6_exit']}")
    loadgen = m.get("loadgen_max")
    if loadgen is None and m.get("steps"):
        maxima = [s.get("loadgen_max") for s in m["steps"] if s.get("loadgen_max") is not None]
        loadgen = max(maxima) if maxima else None
    if loadgen is not None and loadgen > LOADGEN_CPU_LIMIT:
        issues.append(f"k6 dùng tới {loadgen:.2f} CPU (> {LOADGEN_CPU_LIMIT}): máy tạo tải có thể là nút thắt")
    if m.get("steal") is not None and m["steal"] > STEAL_LIMIT_PCT:
        issues.append(f"steal {m['steal']:.1f}% (máy ảo bị chiếm CPU)")
    overloaded = (m["mode"] == "fixed" and m.get("rps") and m["target"]
                  and m["rps"] < SLO_ACHIEVED * m["target"])
    if overloaded:
        # Mức tải cao hơn sức chứa: hệ thống không phục vụ kịp nên k6 phải bỏ iteration. Đây là kết quả
        # (điểm bão hòa) chứ không phải lỗi bộ đo, nhưng độ trễ của lần chạy này không so được với lần khác.
        issues.append(f"vượt sức chứa: chỉ đạt {m['rps']:.0f}/{m['target']:.0f} req/s")
    elif m["mode"] == "fixed" and m["target"] and m["dropped"] > DROPPED_LIMIT * m["target"] * (m["measure_s"] or 1):
        issues.append(f"{m['dropped']:.0f} iteration bị bỏ (thiếu VU sẵn sàng)")
    # W7: instance bật lại giữa pha đo ấm lại từ đầu, CPU đầu và cuối pha đo không so được; lỗi và 409 trong lúc có sự
    # cố là kết quả của phép đo (mục sự cố của báo cáo), không phải lỗi bộ đo
    incident = bool(m.get("incident"))
    if (m["mode"] == "fixed" and not incident and m.get("cpu_app_drift") is not None
            and m["cpu_app_drift"] > WARM_DRIFT_LIMIT):
        issues.append(f"CPU app ở phần ba đầu pha đo cao hơn phần ba cuối {100 * m['cpu_app_drift']:.0f}%: "
                      "JVM chưa ấm hết sau warm-up (tăng WARMUP)")
    if m.get("l2_errors"):
        issues.append(f"{m['l2_errors']:.0f} lệnh Redis (L2) lỗi trong lần chạy, "
                      f"{m.get('l2_errors_measure') or 0:.0f} trong pha đo")
    if m["workload"] in ("w2", "w3") and m["conflicts"]:
        # Mỗi VU chỉ sửa sơ đồ của mình và tự đếm version, nên 409 nghĩa là server coi version đúng là cũ: VU nhớ
        # sai (lỗi bộ đo) hoặc server so với một bản cũ trong cache (lỗi của hệ thống, như phiên b12 ở C3)
        issues.append(f"{m['conflicts']:.0f} lần 409 ngoài dự kiến (VU nhớ sai version, hoặc server so với bản cũ "
                      "trong cache)")
    if m["fallbacks"]:
        issues.append(f"{m['fallbacks']:.0f} thao tác phải đổi sang đọc vì phân vùng của VU không có dữ liệu phù hợp (giảm MAX_VUS hoặc tăng SCALE)")
    if m.get("checkpoints"):
        issues.append(f"{m['checkpoints']:.0f} checkpoint giữa lần đo")
    if m["mode"] != "capacity" and not incident and m["err"] and m["err"] >= SLO_ERROR_RATE:
        issues.append(f"lỗi {100 * m['err']:.2f}%")
    return issues


# ----------------------------------------------------------------------------- gộp các lần lặp

def spread(values) -> tuple[float, float, float] | None:
    values = [v for v in values if v is not None]
    if not values:
        return None
    return statistics.median(values), min(values), max(values)


def fmt(stat, digits=1, unit="") -> str:
    if stat is None:
        return "–"
    if isinstance(stat, tuple):
        med, lo, hi = stat
        if lo == hi:
            return f"{med:.{digits}f}{unit}"
        return f"{med:.{digits}f}{unit} ({lo:.{digits}f}–{hi:.{digits}f})"
    return f"{stat:.{digits}f}{unit}"


def hit_ratio(ms: list[dict]) -> str:
    """Tỉ lệ trúng cache L1, L2 và cache token (nếu có), trung vị qua các lần lặp; "–" khi không có cache."""
    parts = []
    for level, name in (("l1", "L1"), ("l2", "L2"), ("token", "token")):
        s = spread([m.get(f"hit_{level}") for m in ms])
        if s:
            parts.append(f"{name} {100 * s[0]:.0f}%")
    return ", ".join(parts) or "–"


def level_key(level: str):
    try:
        return (0, float(level))
    except ValueError:
        return (1, level)


def workload_name(workload: str) -> str:
    """Tên kịch bản như trong tài liệu: W2, W7, W7r."""
    return workload[:2].upper() + workload[2:]


# ----------------------------------------------------------------------------- biểu đồ

def charts_available():
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        plt.rcParams.update({
            "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
            "axes.edgecolor": AXIS, "axes.labelcolor": INK_2, "axes.titlecolor": INK, "text.color": INK,
            "xtick.color": MUTED, "ytick.color": MUTED, "axes.grid": True, "grid.color": GRID,
            "grid.linewidth": 0.8, "grid.linestyle": "-", "axes.spines.top": False, "axes.spines.right": False,
            "axes.titlesize": 12, "axes.titleweight": "bold", "font.size": 10, "legend.frameon": False,
            "axes.axisbelow": True,
            "lines.linewidth": 2, "lines.solid_capstyle": "round", "lines.solid_joinstyle": "round",
        })
        return plt
    except Exception:   # noqa: BLE001 - thiếu matplotlib thì chỉ bỏ biểu đồ
        return None


def color_of(configs: list[str], cfg: str) -> str:
    return SERIES[configs.index(cfg) % len(SERIES)]


def chart_capacity(plt, out: Path, workload: str, runs: list[dict], configs: list[str], slo: float) -> str:
    fig, ax = plt.subplots(figsize=(7.5, 4.2))
    top = slo * 2
    for cfg in configs:
        stages = defaultdict(lambda: {"achieved": [], "p95": []})
        for m in runs:
            if m["config"] == cfg:
                for s in m["steps"]:
                    stages[s["target"]]["achieved"].append(s["achieved"])
                    stages[s["target"]]["p95"].append(s["p95"])
        if not stages:
            continue
        xs = [statistics.median(stages[t]["achieved"]) for t in sorted(stages)]
        ys = [statistics.median([p for p in stages[t]["p95"] if p is not None] or [0]) for t in sorted(stages)]
        top = max(top, min(max(ys), slo * 10))
        ax.plot(xs, ys, color=color_of(configs, cfg), marker="o", markersize=6, markeredgecolor=SURFACE,
                markeredgewidth=2, label=cfg)
    ax.axhline(slo, color=INK_2, linewidth=1)
    ax.annotate(f"SLO p95 {slo:.0f} ms", xy=(0.01, slo), xycoords=("axes fraction", "data"),
                xytext=(0, 4), textcoords="offset points", color=INK_2, fontsize=9)
    ax.set_ylim(0, top * 1.05)
    ax.set_xlim(left=0)
    ax.set_xlabel("Throughput đạt được (request/giây)")
    ax.set_ylabel("p95 (ms)")
    ax.set_title(f"{workload_name(workload)}: độ trễ p95 theo tải (trung vị các lần lặp)", loc="left")
    if len(configs) > 1:
        ax.legend(loc="upper left")
    name = f"capacity_{workload}.png"
    fig.tight_layout()
    fig.savefig(out / name, dpi=130)
    plt.close(fig)
    return name


def chart_fixed(plt, out: Path, workload: str, groups: dict, configs: list[str], slo: float) -> str:
    fig, ax = plt.subplots(figsize=(7.5, 4.2))
    for cfg in configs:
        points = sorted(((num(level), spread([m["p95"] for m in ms])) for (c, level), ms in groups.items()
                         if c == cfg), key=lambda p: p[0])
        points = [(x, s) for x, s in points if s]
        if not points:
            continue
        xs = [x for x, _ in points]
        med = [s[0] for _, s in points]
        err = [[s[0] - s[1] for _, s in points], [s[2] - s[0] for _, s in points]]
        ax.errorbar(xs, med, yerr=err, color=color_of(configs, cfg), marker="o", markersize=6,
                    markeredgecolor=SURFACE, markeredgewidth=2, capsize=3, elinewidth=1, label=cfg)
    ax.axhline(slo, color=INK_2, linewidth=1)
    ax.annotate(f"SLO p95 {slo:.0f} ms", xy=(0.01, slo), xycoords=("axes fraction", "data"),
                xytext=(0, 4), textcoords="offset points", color=INK_2, fontsize=9)
    ax.set_ylim(bottom=0)
    ax.set_xlabel("Tốc độ đến (request/giây)")
    ax.set_ylabel("p95 (ms), vạch: min–max các lần lặp")
    ax.set_title(f"{workload_name(workload)}: p95 ở tải cố định", loc="left")
    if len(configs) > 1:
        ax.legend(loc="upper left")
    name = f"fixed_{workload}.png"
    fig.tight_layout()
    fig.savefig(out / name, dpi=130)
    plt.close(fig)
    return name


def chart_ops(plt, out: Path, workload: str, level: str, per_config: dict, configs: list[str]) -> str:
    # Màu theo vị trí trong mọi cấu hình của phiên (cùng màu ở mọi biểu đồ), chỉ vẽ cấu hình có số liệu
    shown = [c for c in configs if per_config.get(c)]
    ops = sorted({op for d in per_config.values() for op in d},
                 key=lambda op: -(per_config.get(shown[0], {}).get(op) or 0))
    fig, ax = plt.subplots(figsize=(7.5, 0.42 * len(ops) * max(1, len(shown)) + 1.2))
    height = 0.8 / max(1, len(shown))
    for k, cfg in enumerate(shown):
        values = [per_config.get(cfg, {}).get(op) or 0 for op in ops]
        ys = [i + k * height for i in range(len(ops))]
        ax.barh(ys, values, height=height, color=color_of(configs, cfg), edgecolor=SURFACE, linewidth=2,
                label=cfg)
    ax.set_yticks([i + height * (len(shown) - 1) / 2 for i in range(len(ops))])
    ax.set_yticklabels(ops)
    ax.invert_yaxis()
    ax.grid(axis="y", visible=False)
    ax.set_xlabel("p95 (ms)")
    ax.set_title(f"{workload_name(workload)} ở {level} req/s: p95 theo thao tác", loc="left")
    if len(shown) > 1:
        ax.legend(loc="lower right")
    name = f"ops_{workload}_{level}.png"
    fig.tight_layout()
    fig.savefig(out / name, dpi=130)
    plt.close(fig)
    return name


def chart_cpu(plt, out: Path, workload: str, groups: dict, configs: list[str]) -> str:
    labels, parts = [], {"app": [], "db": [], "cache": [], "lb": [], "loadgen": []}
    for (cfg, level) in sorted(groups, key=lambda k: (configs.index(k[0]), level_key(k[1]))):
        ms = groups[(cfg, level)]
        labels.append(f"{cfg}\n{level}" if len(configs) > 1 else str(level))
        for g in parts:
            s = spread([m.get(f"cpu_{g}") for m in ms])
            parts[g].append(s[0] if s else 0)
    if not any(parts["lb"]):
        del parts["lb"]   # phiên không có Nginx: giữ màu và chú thích như các phiên trước
    fig, ax = plt.subplots(figsize=(max(7.5, 0.9 * len(labels) + 1.5), 4.2))
    bottom = [0.0] * len(labels)
    names = {"app": "app (JVM, mọi instance)", "db": "PostgreSQL", "cache": "Redis", "lb": "Nginx", "loadgen": "k6"}
    for k, g in enumerate(parts):
        ax.bar(range(len(labels)), parts[g], bottom=bottom, color=SERIES[k], edgecolor=SURFACE, linewidth=2,
               label=names[g], width=0.6)
        bottom = [b + v for b, v in zip(bottom, parts[g])]
    ax.set_xticks(range(len(labels)))
    ax.set_xticklabels(labels)
    ax.grid(axis="x", visible=False)
    ax.set_xlabel("Mức tải" if workload != "w1" else "Số VU")
    ax.set_ylabel("CPU (số nhân, trung bình pha đo)")
    ax.set_title(f"{workload_name(workload)}: CPU theo tiến trình", loc="left")
    ax.legend(loc="upper left")
    name = f"cpu_{workload}.png"
    fig.tight_layout()
    fig.savefig(out / name, dpi=130)
    plt.close(fig)
    return name


# ----------------------------------------------------------------------------- sự cố (W7, B4)

INCIDENT_TAIL_S = 10   # cửa sổ sự cố kéo dài thêm 10 giây sau sự kiện cuối (instance vừa nhận lại traffic)
EVENT_NAMES = {"kill": "kill -9", "start": "bật lại", "ready": "sẵn sàng", "drain": "Nginx thôi gửi",
               "stop": "tắt êm", "undrain": "nhận lại traffic"}


def incident_metrics(run_dir: Path, window) -> dict | None:
    """Số liệu từng giây phía Nginx (timeline.csv) quanh các sự kiện của incidents.csv, chỉ trong pha đo."""
    timeline = [{k: num(v) for k, v in r.items()} for r in read_csv(run_dir / "timeline.csv")]
    events = [(num(r["ts"]), r["event"], r["instance"]) for r in read_csv(run_dir / "incidents.csv")]
    if not timeline or not events or window is None:
        return None
    w0, w1 = window
    seconds = [r for r in timeline if w0 <= r["ts"] < w1]
    first, last = events[0][0], events[-1][0]
    before = [r for r in seconds if r["ts"] + 1 <= first]
    during = [r for r in seconds if first - 1 < r["ts"] <= last + INCIDENT_TAIL_S]
    after = [r for r in seconds if r["ts"] > last + INCIDENT_TAIL_S]
    baseline = statistics.median([r["p99_ms"] for r in before]) if before else None
    # Một giây "xấu": Nginx trả lỗi 5xx, hoặc p99 của giây đó vượt cả SLO lẫn 2 lần p99 bình thường
    threshold = max(100.0, 2 * baseline) if baseline else 100.0
    bad = [r for r in during if r["r5xx"] > 0 or r["p99_ms"] > threshold]
    kill = next((ts for ts, event, _ in events if event == "kill"), None)
    restart = next((ts for ts, event, _ in events if event == "start"), None)
    recovery = None
    if kill is not None:
        # Từ lúc kill tới hết giây xấu cuối cùng trước khi instance được bật lại
        down = [r for r in bad if kill - 1 < r["ts"] < (restart or w1)]
        recovery = max(0.0, down[-1]["ts"] + 1 - kill) if down else 0.0
    total = lambda rows, key: sum(r.get(key, 0) for r in rows)   # noqa: E731
    return {
        "events": events, "seconds": seconds, "w0": w0, "baseline_p99": baseline, "threshold": threshold,
        "requests": total(seconds, "requests"), "r5xx": total(seconds, "r5xx"),
        "post_5xx": total(seconds, "post_5xx"), "r409": total(seconds, "r409"), "retried": total(seconds, "retried"),
        "window_requests": total(during, "requests"), "window_5xx": total(during, "r5xx"),
        "window_s": (last + INCIDENT_TAIL_S) - first, "bad_seconds": len(bad), "recovery_s": recovery,
        "p99_before": baseline,
        "p99_during_max": max((r["p99_ms"] for r in during), default=None),
        "p99_after": statistics.median([r["p99_ms"] for r in after]) if after else None,
    }


def chart_incident(plt, out: Path, name: str, inc: dict, slo: float) -> str:
    seconds, w0 = inc["seconds"], inc["w0"]
    xs = [r["ts"] + 0.5 - w0 for r in seconds]   # giữa giây của mỗi dòng (request kết thúc trong giây đó)
    ports = sorted({k for r in seconds for k in r if k.startswith("served_")})
    fig, (top, bottom) = plt.subplots(2, 1, figsize=(8.5, 5.6), sharex=True, gridspec_kw={"height_ratios": [1, 1.2]})
    top.plot(xs, [r["p99_ms"] for r in seconds], color=SERIES[0], linewidth=1.5, label="p99 từng giây")
    top.axhline(slo, color=INK_2, linewidth=1)
    top.annotate(f"SLO {slo:.0f} ms", xy=(0.01, slo), xycoords=("axes fraction", "data"), xytext=(0, 4),
                 textcoords="offset points", color=INK_2, fontsize=9)
    top.set_ylabel("p99 (ms, phía Nginx)")
    top.set_ylim(bottom=0)
    for k, port in enumerate(ports):
        bottom.plot(xs, [r.get(port, 0) for r in seconds], color=SERIES[(k + 1) % len(SERIES)], linewidth=1.5,
                    label=f"instance cổng {port.removeprefix('served_')}")
    # Request Nginx phải gửi sang instance khác (instance đầu lỗi kết nối) và lỗi 5xx client nhận, dạng cột
    retried = [(x, r["retried"]) for x, r in zip(xs, seconds) if r.get("retried")]
    if retried:
        bottom.bar([x for x, _ in retried], [v for _, v in retried], width=1.0, color=SERIES[3], label="Nginx gửi lại")
    errors = [(x, r["r5xx"]) for x, r in zip(xs, seconds) if r["r5xx"]]
    if errors:
        bottom.bar([x for x, _ in errors], [e for _, e in errors], width=1.0, color=SERIES[7], label="lỗi 5xx")
    bottom.set_ylabel("request/giây")
    bottom.set_xlabel("giây kể từ đầu pha đo")
    bottom.set_ylim(bottom=0)
    drains = {i: ts for ts, event, i in inc["events"] if event == "drain"}
    if drains:
        # Khởi động lại cuốn chiếu: các sự kiện của một instance cách nhau vài giây, nên tô cả khoảng instance đó nằm
        # ngoài Nginx (từ lúc thôi gửi tới lúc nhận lại traffic) thay vì vẽ từng vạch
        undrains = {i: ts for ts, event, i in inc["events"] if event == "undrain"}
        for i, start in drains.items():
            end = undrains.get(i, start)
            for ax in (top, bottom):
                ax.axvspan(start - w0, end - w0, color=GRID, alpha=0.6, linewidth=0)
            top.annotate(f"instance {i} ngoài Nginx", xy=(start - w0, 1), xycoords=("data", "axes fraction"),
                         xytext=(3, -3), textcoords="offset points", va="top", fontsize=8, color=INK_2)
    else:
        for ts, event, instance in inc["events"]:
            for ax in (top, bottom):
                ax.axvline(ts - w0, color=MUTED, linewidth=1, linestyle="--")
            top.annotate(f"{EVENT_NAMES.get(event, event)} {instance}", xy=(ts - w0, 1),
                         xycoords=("data", "axes fraction"), xytext=(2, -2), textcoords="offset points", rotation=90,
                         va="top", fontsize=8, color=INK_2)
    top.set_title(f"{name}: độ trễ và phân phối request quanh sự cố", loc="left")
    bottom.legend(loc="lower left", fontsize=8, ncol=4)
    file = f"incident_{name}.png"
    fig.tight_layout()
    fig.savefig(out / file, dpi=130)
    plt.close(fig)
    return file


def incident_section(session: Path, runs: list[dict], plt, charts_dir: Path) -> list[str]:
    lines = ["## Sự cố: một instance chết, khởi động lại cuốn chiếu (W7, B4)", "",
             "Số liệu phía Nginx từng giây (`timeline.csv`, từ log truy cập), chỉ trong pha đo. Cửa sổ sự cố: từ sự "
             f"kiện đầu tới {INCIDENT_TAIL_S} giây sau sự kiện cuối. Giây xấu: Nginx trả 5xx, hoặc p99 của giây đó "
             "vượt cả SLO 100 ms lẫn 2 lần p99 trước sự cố. Hồi phục: từ lúc `kill -9` tới hết giây xấu cuối cùng "
             "trước khi instance được bật lại. \"Gửi lại\": request Nginx chuyển sang instance khác khi instance đầu "
             "lỗi; POST và PATCH chỉ được chuyển khi chưa gửi được (lỗi lúc kết nối), đã gửi thì không bao giờ gửi "
             "lại. PUT đã ghi xong ở instance chết rồi được gửi lại thì nhận 409 (VU đọc lại version).", "",
             "| Lần chạy | Sự cố | Request | Lỗi 5xx (POST, PATCH) | 409 | Gửi lại | Lỗi trong cửa sổ sự cố | "
             "Giây xấu | Hồi phục (s) | p99 trước / lớn nhất trong / sau (ms) | Lỗi phía k6 |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    charts = []
    for m in sorted(runs, key=lambda m: (m["config"], m["workload"], m["rep"])):
        run = Run(session / m["run"])
        inc = incident_metrics(run.dir, run.window)
        if inc is None:
            lines.append(f"| `{m['run']}` | {m['incident']} | thiếu timeline.csv hoặc incidents.csv | | | | | | | | |")
            continue
        kind = "kill -9 rồi bật lại" if m["incident"] == "kill" else "khởi động lại cuốn chiếu"
        rate = 100.0 * inc["window_5xx"] / inc["window_requests"] if inc["window_requests"] else 0.0
        recovery = fmt(inc["recovery_s"], 0) if inc["recovery_s"] is not None else "–"
        lines.append(
            f"| `{m['run']}` | {kind} | {inc['requests']:.0f} | {inc['r5xx']:.0f} ({inc['post_5xx']:.0f}) | "
            f"{inc['r409']:.0f} | {inc['retried']:.0f} | {rate:.2f}% ({inc['window_5xx']:.0f}/{inc['window_requests']:.0f} "
            f"trong {inc['window_s']:.0f} s) | {inc['bad_seconds']} | {recovery} | "
            f"{fmt(inc['p99_before'])} / {fmt(inc['p99_during_max'])} / {fmt(inc['p99_after'])} | "
            f"{100 * m['err']:.2f}% |")
        if plt and m["rep"] == min(r["rep"] for r in runs if r["config"] == m["config"] and r["workload"] == m["workload"]):
            charts.append(chart_incident(plt, charts_dir, m["run"], inc, 100.0))
    lines.append("")
    lines += ["Sự kiện (giây kể từ đầu pha đo):", ""]
    for m in sorted(runs, key=lambda m: (m["config"], m["workload"], m["rep"])):
        run = Run(session / m["run"])
        events = [(num(r["ts"]), r["event"], r["instance"]) for r in read_csv(run.dir / "incidents.csv")]
        if events and run.window:
            text = ", ".join(f"{ts - run.window[0]:.1f} {EVENT_NAMES.get(e, e)} {i}" for ts, e, i in events)
            lines.append(f"- `{m['run']}`: {text}")
    lines.append("")
    lines += [f"![{c}](charts/{c})" for c in charts]
    if charts:
        lines.append("")
    return lines


# ----------------------------------------------------------------------------- báo cáo

def env_summary(session: Path) -> list[str]:
    text = (session / "env.txt").read_text(encoding="utf-8", errors="replace") if (session / "env.txt").exists() else ""

    def grab(pattern):
        m = re.search(pattern, text, re.M)
        return m.group(1).strip() if m else "?"

    model = grab(r"^Model name:\s*(.+)$")
    cpus = grab(r"^CPU\(s\):\s*(\d+)$")
    threads = grab(r"^Thread\(s\) per core:\s*(\d+)$")
    memory = grab(r"^Mem:\s+(\S+)")
    os_name = grab(r'PRETTY_NAME="?([^"]+)')
    sut = grab(r"hệ thống được đo \([^)]*\): (.+)$")
    loadgen = grab(r"tạo tải và thu số liệu \(k6, bench/collect\): (.+)$")
    java = grab(r"^(openjdk version .+)$")
    k6 = grab(r"^(k6 v\S+)")
    postgres = grab(r"^(PostgreSQL \d+\.\d+)")
    return [
        f"- Máy: {model}, {cpus} vCPU ({threads} luồng/nhân), RAM {memory}",
        f"- Hệ điều hành: {os_name}",
        f"- Bố trí CPU: hệ thống được đo {sut}, tạo tải {loadgen}",
        f"- Phần mềm: {java}; {k6}; {postgres}",
    ]


def write_report(session: Path, metrics: list[dict]) -> Path:
    plt = charts_available()
    charts_dir = session / "charts"
    if plt:
        charts_dir.mkdir(exist_ok=True)
    configs = sorted({m["config"] for m in metrics})
    usable = [m for m in metrics if not m["profiled"] and m["status"] == "ok"]
    lines = [f"# Kết quả đo: {session.name}", ""]
    lines += ["Sinh tự động bởi `bench/analysis/report.py`. Phương pháp: "
              "[ADR-0001](../../../docs/adr/0001-phuong-phap-do.md), cách chạy lại: [bench/README.md](../../README.md).", ""]
    lines += ["## Môi trường", ""] + env_summary(session)
    configs_txt = session / "configs.txt"
    if configs_txt.exists():
        for line in configs_txt.read_text().split("\n"):
            if line.strip():
                parts = line.split(maxsplit=3)   # cấu hình, ref, commit, [tham số app]
                cfg, ref, sha = parts[:3]
                args = f", tham số `{parts[3]}`" if len(parts) > 3 else ""
                scaled = next((m for m in metrics if m["config"] == cfg and m.get("instances")), None)
                if scaled:
                    args += f"; {scaled['instances']} instance sau Nginx"
                    if scaled.get("heap_mb_per_instance"):
                        args += (f", mỗi instance heap {scaled['heap_mb_per_instance']} MB, pool "
                                 f"{scaled['pool_per_instance']} kết nối")
                lines.append(f"- Cấu hình {cfg}: `{ref}` (commit `{sha[:10]}`){args}")
    explain_lines = explain_tables(session)
    manifest_path = session / "manifest.json"
    if manifest_path.exists():
        man = json.loads(manifest_path.read_text(encoding="utf-8"))
        c = man["counts"]
        lines.append(f"- Dữ liệu: scale {man['scale']}, seed {man['seed']}: {c['users']} user, {c['diagrams']} sơ đồ "
                     f"({man['data_bytes']['diagrams'] / 2**20:.0f} MB data), {c['diagram_versions']} phiên bản, "
                     f"{c['diagram_shares']} link")
    lines.append("")
    lines += explain_lines

    # ---- Tính hợp lệ
    lines += ["## Kiểm tra tính hợp lệ của phép đo", ""]
    problems = [(m, validity(m)) for m in metrics]
    bad = [(m, p) for m, p in problems if p]
    if not bad:
        lines.append(f"Cả {len(metrics)} lần chạy đều hợp lệ: k6 không bão hòa CPU, không có steal đáng kể, đạt "
                     "tốc độ đến yêu cầu, không có 409 giả tạo, không checkpoint giữa lần đo.")
    else:
        lines += ["| Lần chạy | Vấn đề |", "|---|---|"]
        lines += [f"| `{m['run']}` | {'; '.join(p)} |" for m, p in bad]
    lines.append("")

    by_workload = defaultdict(list)
    for m in usable:
        by_workload[m["workload"]].append(m)

    for workload in sorted(by_workload):
        runs = by_workload[workload]
        lines += [f"## {workload_name(workload)}", ""]

        capacity = [m for m in runs if m["mode"] == "capacity"]
        if capacity:
            slo = capacity[0]["slo_p95"]
            lines += [f"### Sức chứa: throughput tối đa còn giữ p95 ≤ {slo:.0f} ms, lỗi < 1%", ""]
            lines += ["| Cấu hình | Tốc độ tối đa đạt SLO (req/s) | Số lần lặp |", "|---|---|---|"]
            for cfg in configs:
                values = [m["capacity"] for m in capacity if m["config"] == cfg]
                if values:
                    lines.append(f"| {cfg} | {fmt(spread(values), 0)} | {len(values)} |")
            lines.append("")
            lines += ["Từng bậc tải (lần lặp đầu):", "",
                      "| Cấu hình | Mục tiêu | Đạt | p95 (ms) | p99 (ms) | Lỗi | CPU app | CPU PostgreSQL |",
                      "|---|---|---|---|---|---|---|---|"]
            for cfg in configs:
                first = next((m for m in sorted(capacity, key=lambda m: m["rep"]) if m["config"] == cfg), None)
                for s in (first or {}).get("steps", []):
                    mark = "" if step_passes(s, slo) else " ✗"
                    lines.append(f"| {cfg} | {s['target']}{mark} | {s['achieved']:.0f} | {fmt(s['p95'])} | "
                                 f"{fmt(s['p99'])} | {100 * s['err']:.2f}% | {fmt(s.get('cpu_app'), 2)} | "
                                 f"{fmt(s.get('cpu_db'), 2)} |")
            lines.append("")
            if plt:
                lines += [f"![Sức chứa {workload}](charts/{chart_capacity(plt, charts_dir, workload, capacity, configs, slo)})", ""]

        fixed = [m for m in runs if m["mode"] in ("fixed", "closed")]
        if fixed:
            groups = defaultdict(list)
            for m in fixed:
                groups[(m["config"], m["level"])].append(m)
            closed = fixed[0]["mode"] == "closed"
            title = "Mô hình tải đóng (số VU cố định, không nghỉ)" if closed else "Độ trễ ở tốc độ đến cố định"
            lines += [f"### {title}", "", "Trung vị (min–max) qua các lần lặp, chỉ tính pha đo.", ""]
            head = "VU" if closed else "Tốc độ đến"
            # Cột Nginx chỉ khi phiên có cấu hình chạy sau Nginx (B4); CPU app là tổng các instance
            with_lb = any(m.get("cpu_lb") for m in fixed)
            lb_head, lb_sep = (" CPU Nginx |", "---|") if with_lb else ("", "")
            lines += [f"| Cấu hình | {head} | Throughput (req/s) | p50 (ms) | p95 (ms) | p99 (ms) | Lỗi | "
                      f"CPU app | CPU PostgreSQL | CPU Redis |{lb_head} CPU k6 | Server trung bình (ms) | SQL/request | "
                      "Khối TOAST/request | Trúng cache | 304 |",
                      "|---|---|---|---|---|---|---|---|---|---|" + lb_sep + "---|---|---|---|---|---|"]
            for (cfg, level) in sorted(groups, key=lambda k: (configs.index(k[0]), level_key(k[1]))):
                ms = groups[(cfg, level)]
                g = lambda key: spread([m.get(key) for m in ms])   # noqa: E731
                lb_cell = f" {fmt(g('cpu_lb'), 2)} |" if with_lb else ""
                lines.append(
                    f"| {cfg} | {level} | {fmt(g('rps'), 0)} | {fmt(g('p50'))} | {fmt(g('p95'))} | {fmt(g('p99'))} | "
                    f"{fmt(spread([100 * m['err'] for m in ms]), 2, '%')} | {fmt(g('cpu_app'), 2)} | "
                    f"{fmt(g('cpu_db'), 2)} | {fmt(g('cpu_cache'), 2)} |{lb_cell} {fmt(g('cpu_loadgen'), 2)} | "
                    f"{fmt(g('srv_mean'), 2)} | {fmt(g('sql_per_req'), 2)} | {fmt(g('toast_per_req'), 1)} | "
                    f"{hit_ratio(ms)} | {fmt(g('share_304'), 1, '%')} |")
            lines.append("")
            lines += ["Phía server (trung vị qua các lần lặp). Transaction CSDL tính cả câu lệnh chạy autocommit, "
                      "mỗi câu như vậy là một transaction riêng ở PostgreSQL:", "",
                      f"| Cấu hình | {head} | p95 server (ms) | Kết nối CSDL đang dùng | Chờ kết nối (max) | "
                      "Luồng Tomcat bận (tb/max) | GC (ms/giây) | Transaction CSDL/request | Khối CSDL/request |",
                      "|---|---|---|---|---|---|---|---|---|"]
            for (cfg, level) in sorted(groups, key=lambda k: (configs.index(k[0]), level_key(k[1]))):
                ms = groups[(cfg, level)]
                med = lambda key: (spread([m.get(key) for m in ms]) or (None,))[0]   # noqa: E731
                busy = f"{fmt(med('tomcat_busy'))}/{fmt(med('tomcat_busy_max'), 0)}"
                lines.append(f"| {cfg} | {level} | {fmt(med('srv_p95'))} | {fmt(med('hikari_active'))} | "
                             f"{fmt(med('hikari_pending_max'), 0)} | {busy} | {fmt(med('gc_ms_per_s'), 2)} | "
                             f"{fmt(med('commits_per_req'), 2)} | {fmt(med('blocks_per_req'), 1)} |")
            lines.append("")

            # p95 theo thao tác ở mức cao nhất mà mọi cấu hình còn phục vụ kịp và giữ SLO (trung vị qua các lần lặp).
            # Ở mức quá tải, mọi thao tác chỉ còn thời gian xếp hàng như nhau (phiên b3, 700 req/s) nên không so được.
            # Mô hình đóng (W1) không có SLO theo tốc độ đến: giữ mức giữa như trước.
            levels = sorted({m["level"] for m in fixed}, key=level_key)
            if closed:
                ref_level, ref_note = levels[len(levels) // 2], ""
            else:
                ref_level, ref_note = levels[0], "mức thấp nhất, không mức nào mọi cấu hình đều giữ SLO"
                for level in reversed(levels):
                    if all(level_passes(ms) for (c, lv), ms in groups.items() if lv == level):
                        ref_level, ref_note = level, "mức cao nhất mà mọi cấu hình còn giữ SLO"
                        break
            per_config = {}
            for cfg in configs:
                ms = groups.get((cfg, ref_level), [])
                ops = sorted({op for m in ms for op in m["ops"]})
                per_config[cfg] = {op: (spread([m["ops"].get(op, {}).get("p95") for m in ms]) or (None,))[0]
                                   for op in ops}
            # Chỉ các cấu hình có chạy kịch bản này ở mức đó (W7 chỉ chạy ở một cấu hình)
            shown = [c for c in configs if per_config.get(c)]
            ops = sorted({op for d in per_config.values() for op in d},
                         key=lambda op: -(per_config.get(shown[0], {}).get(op) or 0))
            if ops:
                note = "" if closed or len(levels) == 1 else f", {ref_note}"
                lines += [f"p95 theo thao tác ở mức {ref_level} (ms, trung vị{note}):", "",
                          "| Thao tác | " + " | ".join(shown) + " |", "|---|" + "---|" * len(shown)]
                for op in ops:
                    lines.append(f"| {op} | " + " | ".join(fmt(per_config[c].get(op)) for c in shown) + " |")
                lines.append("")
            if plt:
                if not closed:
                    lines.append(f"![p95 {workload}](charts/{chart_fixed(plt, charts_dir, workload, groups, configs, 100.0)})")
                lines.append(f"![CPU {workload}](charts/{chart_cpu(plt, charts_dir, workload, groups, configs)})")
                if ops:
                    lines.append(f"![Thao tác {workload}](charts/{chart_ops(plt, charts_dir, workload, ref_level, per_config, configs)})")
                lines.append("")

            # Câu SQL tốn thời gian nhất của một lần chạy tiêu biểu
            sample_cfg = shown[0] if shown else configs[0]
            sample = next((m for m in sorted(groups.get((sample_cfg, ref_level), []), key=lambda m: m["rep"])), None)
            if sample and sample["top_sql"] and sample["total_requests"]:
                lines += [f"Câu SQL tốn thời gian nhất (`{sample['run']}`):", "",
                          "| Lần gọi/request | Trung bình (ms) | % thời gian CSDL | Câu lệnh |", "|---|---|---|---|"]
                total_ms = sum(num(s["total_exec_ms"]) for s in sample["top_sql"]) or 1
                for s in sample["top_sql"][:6]:
                    # Rút gọn danh sách cột của SELECT để thấy bảng và điều kiện WHERE
                    query = re.sub(r"^select .+? from ", "select … from ", s["query"], flags=re.I)
                    query = query if len(query) <= 140 else query[:137] + "..."
                    lines.append(f"| {num(s['calls']) / sample['total_requests']:.3f} | {num(s['mean_exec_ms']):.2f} | "
                                 f"{100 * num(s['total_exec_ms']) / total_ms:.0f}% | `{query}` |")
                lines.append("")

    incident_runs = [m for m in usable if m.get("incident")]
    if incident_runs:
        lines += incident_section(session, incident_runs, plt, charts_dir)

    profiled = [m for m in metrics if m["profiled"]]
    if profiled:
        lines += ["## Flame graph CPU", "", "Lấy ở giữa pha đo, lần chạy riêng không tính vào số liệu ở trên.", ""]
        for m in profiled:
            event_file = session / m["run"] / "flame-event.txt"
            event = event_file.read_text().strip() if event_file.exists() else "?"
            lines.append(f"- `{m['run']}`: [flame.html]({m['run']}/flame.html) (sự kiện `{event}`)")
        lines.append("")

    report = session / "report.md"
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


def explain_tables(session: Path) -> list[str]:
    """Tóm tắt EXPLAIN (ANALYZE, BUFFERS) của từng cấu hình (bench/kaggle/explain.sh), nếu phiên có chạy."""
    files = sorted(session.glob("explain-*.tsv"))
    if not files:
        return []
    lines = ["## Kế hoạch thực thi (EXPLAIN ANALYZE, BUFFERS)", "",
             "Mỗi câu chạy 3 lần, giữ lần cuối (cache đã ấm). Kế hoạch đầy đủ trong `explain-<cấu hình>.txt`.", "",
             "| Cấu hình | Câu truy vấn | Thời gian (ms) | Khối đọc từ bộ đệm | Khối đọc từ đĩa | Dòng trả về | Nút gốc / quét đầu tiên |",
             "|---|---|---|---|---|---|---|"]
    for f in files:
        cfg = f.stem.removeprefix("explain-")
        with f.open(encoding="utf-8") as fh:
            for row in csv.DictReader(fh, delimiter="\t"):
                lines.append(f"| {cfg} | {row['query']} | {row['execution_ms']} | {row['shared_hit']} | "
                             f"{row['shared_read']} | {row['rows']} | {row['top_node']} |")
    lines.append("")
    return lines


def write_summary_csv(session: Path, metrics: list[dict]) -> Path:
    fields = ["run", "config", "git_sha", "workload", "mode", "level", "rep", "status", "profiled", "count", "rps",
              "capacity", "p50", "p95", "p99", "avg", "err", "r304", "share_304", "r409", "r4xx", "r5xx", "rnone", "conflicts",
              "fallbacks", "dropped", "instances", "incident", "cpu_app", "cpu_db", "cpu_cache", "cpu_lb", "cpu_loadgen",
              "loadgen_max", "steal", "sut_busy",
              "srv_mean", "srv_p95", "hikari_active", "hikari_pending_max", "tomcat_busy", "tomcat_busy_max",
              "gc_ms_per_s", "heap_max_mb", "hit_l1", "hit_l2", "hit_token", "sql_per_req", "commits_per_req", "blocks_per_req", "toast_per_req",
              "db_ms_per_req", "checkpoints", "cpu_app_drift", "l2_errors"]
    path = session / "summary.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for m in sorted(metrics, key=lambda m: m["run"]):
            writer.writerow({k: (f"{v:.4g}" if isinstance(v, float) else v) for k, v in m.items() if k in fields})
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("session", type=Path, help="thư mục phiên đo (chứa các thư mục lần chạy)")
    args = parser.parse_args()
    session = args.session.resolve()
    runs = [Run(d) for d in sorted(session.iterdir()) if (d / "meta.json").exists()]
    if not runs:
        print(f"không có lần chạy nào trong {session}", file=sys.stderr)
        return 1
    metrics = [run_metrics(r) for r in runs]
    manifests = {r.meta.get("dataset_manifest_sha256") for r in runs} - {None}
    if len(manifests) > 1:
        print("CẢNH BÁO: các lần chạy dùng dữ liệu khác nhau", file=sys.stderr)
    report = write_report(session, metrics)
    summary = write_summary_csv(session, metrics)
    print(f"đã ghi {report} và {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
