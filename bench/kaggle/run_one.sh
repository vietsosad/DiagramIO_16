#!/usr/bin/env bash
# Một lần đo: chép CSDL mẫu, khởi động app, thu số liệu phía server, chạy k6, xuất thống kê PostgreSQL.
#
#   bash bench/kaggle/run_one.sh --config C0 --jar <app.jar> --workload w2 --mode fixed --level 500 \
#        --rep 1 --session <thư mục phiên> [--profile]
#
#   --workload  w1 | w2 | w3 (W3 = kịch bản W2 chỉ trên sơ đồ lớn) | w7 | w7r (B4: W2 qua nhiều instance; w7 giết
#               instance 1 bằng kill -9 ở 1/3 pha đo và bật lại ở 2/3; w7r khởi động lại cuốn chiếu từng instance,
#               Nginx thôi gửi tới instance trước khi nó tắt êm)
#   --mode      closed (level = số VU) | fixed (level = request/giây) | capacity (level bỏ qua; bậc tải lấy từ
#               RATE_START, RATE_STEP, RATE_MAX, STEP)
#   --instances số instance sau Nginx (B4); 0 (mặc định) là một app nghe thẳng APP_PORT như trước
#   --profile   lấy flame graph CPU (async-profiler) PROFILE_SECONDS giây ở giữa pha đo; lần chạy này
#               bị nhiễu bởi profiler nên báo cáo không gộp nó vào số liệu độ trễ
#
# APP_ARGS (biến môi trường): tham số thêm cho app của cấu hình này, ví dụ --diagramio.cache.mode=two-level
#
# Kết quả ở <phiên>/<config>_<workload>_<mode><level>_r<rep>/: meta.json, k6-summary.json, procstat.csv,
# prom.csv, pg_*.csv; có Nginx thì thêm timeline.csv (từng giây) và incidents.csv (w7, w7r); log thô (app*.log,
# gc*.log, k6.log, nginx_*.log) không commit.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"

config='' jar='' workload='' mode='' level='' rep=1 session='' profile=0 instances=0
while [ $# -gt 0 ]; do
  case "$1" in
    --config) config=$2; shift 2 ;;
    --jar) jar=$2; shift 2 ;;
    --workload) workload=$2; shift 2 ;;
    --mode) mode=$2; shift 2 ;;
    --level) level=$2; shift 2 ;;
    --rep) rep=$2; shift 2 ;;
    --session) session=$2; shift 2 ;;
    --instances) instances=$2; shift 2 ;;
    --profile) profile=1; shift ;;
    *) die "tham số lạ: $1" ;;
  esac
done
[ -n "$config" ] && [ -f "$jar" ] && [ -n "$workload" ] && [ -n "$mode" ] && [ -n "$session" ] \
  || die "thiếu tham số, xem đầu file run_one.sh"

WARMUP=${WARMUP:-60}
MEASURE=${MEASURE:-180}
PROFILE_SECONDS=${PROFILE_SECONDS:-60}

k6_env=(-e "MODE=$mode" -e "WARMUP=$WARMUP" -e "MEASURE=$MEASURE" -e "WORKLOAD=$workload"
        -e "FIXTURES=$DATASET_DIR/fixtures" -e "BASE_URL=http://127.0.0.1:$APP_PORT")
incident=''
case "$workload" in
  w1) script=w1.js ;;
  w2) script=w2.js ;;
  w3) script=w2.js; k6_env+=(-e SIZE_CLASS=L) ;;
  w7) script=w2.js; incident='kill' ;;
  w7r) script=w2.js; incident='rolling' ;;
  *) die "workload chưa hỗ trợ: $workload" ;;
esac
if [ -n "$incident" ] && [ "$instances" -lt 2 ]; then
  die "$workload cần ít nhất 2 instance (--instances)"
fi
case "$mode" in
  closed) [ -n "$level" ] || die "closed cần --level (số VU)"; k6_env+=(-e "VUS=$level") ;;
  fixed) [ -n "$level" ] || die "fixed cần --level (request/giây)"; k6_env+=(-e "RATE=$level") ;;
  capacity) level=auto ;;
  *) die "mode không hợp lệ: $mode" ;;
esac
for name in WARMUP_RAMP RAMP_FROM RATE_START RATE_STEP RATE_MAX STEP SETTLE MAX_VUS PRE_VUS ABORT_P95_MS \
            ABORT_ERROR_RATE ZIPF_S MIX PAGE_SIZE W1_PAYLOAD PASSWORD; do
  if [ -n "${!name:-}" ]; then
    k6_env+=(-e "$name=${!name}")
  fi
done

run="${config}_${workload}_${mode}${level}_r${rep}"
run_dir="$session/$run"
rm -rf "$run_dir" && mkdir -p "$run_dir"
log "=== $run"

cleanup() {
  for pid in ${PROC_PID:-} ${PROM_PID:-} ${PROFILER_PID:-} ${INCIDENT_PID:-}; do
    kill -TERM "$pid" 2>/dev/null || true
  done
  stop_lb
  stop_instances
  stop_app
  # Lần chạy hỏng giữa chừng vẫn phải hiện trong báo cáo (report.py chỉ đọc thư mục có meta.json)
  if [ ! -f "$run_dir/meta.json" ]; then
    printf '{"run": "%s", "config": "%s", "workload": "%s", "mode": "%s", "level": "%s", "rep": "%s", "profiled": %s, "status": "failed", "k6_exit": -1, "warmup_s": %s, "measure_s": %s, "sut_cpus": "%s"}\n' \
      "$run" "$config" "$workload" "$mode" "$level" "$rep" "$([ "$profile" = 1 ] && echo true || echo false)" \
      "$WARMUP" "$MEASURE" "$SUT_CPUS" > "$run_dir/meta.json"
  fi
}
trap cleanup EXIT

reset_run_db
if [ "$profile" = 1 ]; then
  # Thông tin gỡ lỗi giữa các safepoint, để flame graph không bị lệch về điểm safepoint
  JVM_OPTS="$JVM_OPTS -XX:+UnlockDiagnosticVMOptions -XX:+DebugNonSafepoints"
fi
prom_urls=()
if [ "$instances" -gt 0 ]; then
  # shellcheck disable=SC2086 # APP_ARGS cố ý được tách thành nhiều tham số
  start_instances "$instances" "$jar" "$DB_RUN" "$run_dir" ${APP_ARGS:-}
  start_lb "$instances" "$run_dir"
  for i in $(seq 1 "$instances"); do
    prom_urls+=(--url "http://127.0.0.1:$(instance_mgmt "$i")/actuator/prometheus")
  done
else
  # shellcheck disable=SC2086 # APP_ARGS cố ý được tách thành nhiều tham số
  start_app "$jar" "$DB_RUN" "$run_dir" ${APP_ARGS:-}
  prom_urls+=(--url "http://127.0.0.1:$MGMT_PORT/actuator/prometheus")
fi

taskset -c "$LOADGEN_CPUS" "$PYTHON" "$BENCH_DIR/collect/procstat.py" --out "$run_dir/procstat.csv" &
PROC_PID=$!
taskset -c "$LOADGEN_CPUS" "$PYTHON" "$BENCH_DIR/collect/prom_scrape.py" \
  "${prom_urls[@]}" --out "$run_dir/prom.csv" 2> "$run_dir/prom_scrape.log" &
PROM_PID=$!
bash "$BENCH_DIR/collect/pg_stats.sh" reset

profile_mid_measure() {
  local start_ms='' begin wait_s event
  for _ in $(seq 1 1200); do
    start_ms=$(grep -oE 'SCENARIO_START [0-9]+' "$run_dir/k6.log" 2>/dev/null | head -n 1 | cut -d' ' -f2 || true)
    [ -n "$start_ms" ] && break
    sleep 0.5
  done
  if [ -z "$start_ms" ]; then
    echo "không thấy SCENARIO_START trong k6.log" > "$run_dir/asprof.log"
    return
  fi
  begin=$(( start_ms / 1000 + WARMUP + MEASURE / 2 - PROFILE_SECONDS / 2 ))
  wait_s=$(( begin - $(date +%s) ))
  [ "$wait_s" -gt 0 ] && sleep "$wait_s"
  # perf_events thường bị chặn trong container; khi đó dùng bộ đếm thời gian CPU (ctimer, itimer)
  for event in cpu ctimer itimer; do
    if "$ASPROF" -d "$PROFILE_SECONDS" -e "$event" -f "$run_dir/flame.html" "$APP_PID" >> "$run_dir/asprof.log" 2>&1 \
        && [ -s "$run_dir/flame.html" ]; then
      echo "$event" > "$run_dir/flame-event.txt"
      return
    fi
  done
}
if [ "$profile" = 1 ]; then
  profile_mid_measure &
  PROFILER_PID=$!
fi

# Sự cố của W7 (B4), tính giờ từ lúc k6 bắt đầu kịch bản; mỗi bước ghi một dòng vào incidents.csv
incident_event() { echo "$(date +%s.%3N),$1,$2" >> "$run_dir/incidents.csv"; }
sleep_until() { local wait_s; wait_s=$(awk -v t="$1" -v now="$(date +%s.%N)" 'BEGIN { printf "%.3f", t - now }');
  awk -v w="$wait_s" 'BEGIN { exit !(w > 0) }' && sleep "$wait_s"; return 0; }
run_incident() {
  local start_ms='' t0
  for _ in $(seq 1 1200); do
    start_ms=$(grep -oE 'SCENARIO_START [0-9]+' "$run_dir/k6.log" 2>/dev/null | head -n 1 | cut -d' ' -f2 || true)
    [ -n "$start_ms" ] && break
    sleep 0.5
  done
  [ -n "$start_ms" ] || { echo "không thấy SCENARIO_START trong k6.log" >> "$run_dir/incidents.log"; return; }
  t0=$(( start_ms / 1000 + WARMUP ))
  echo "ts,event,instance" > "$run_dir/incidents.csv"
  if [ "$incident" = kill ]; then
    # Máy chết đột ngột: không tắt êm, không báo Nginx; bật lại sau 1/3 pha đo
    sleep_until $(( t0 + MEASURE / 3 ))
    incident_event kill 1
    kill_instance 1
    sleep_until $(( t0 + 2 * MEASURE / 3 ))
    incident_event start 1
    launch_instance 1
    wait_ready 1 180 && incident_event ready 1
  else
    # Triển khai cuốn chiếu: với từng instance, Nginx thôi gửi tới nó, nó tắt êm, khởi động lại, rồi nhận lại traffic
    sleep_until $(( t0 + MEASURE / 6 ))
    for i in $(seq 1 "$instances"); do
      incident_event drain "$i"
      reload_lb "$i"
      incident_event stop "$i"
      term_instance "$i"
      incident_event start "$i"
      launch_instance "$i"
      wait_ready "$i" 180 && incident_event ready "$i"
      # Ghi trước khi nạp lại: worker mới của Nginx gửi tới instance ngay khi nhận cấu hình, trước khi worker cũ thoát
      incident_event undrain "$i"
      reload_lb 0
      sleep 5
    done
  fi
}
if [ -n "$incident" ]; then
  run_incident > "$run_dir/incidents.log" 2>&1 &
  INCIDENT_PID=$!
fi

started_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
set +e
taskset -c "$LOADGEN_CPUS" "$K6" run --quiet --no-color "${k6_env[@]}" -e "OUT=$run_dir/k6-summary.json" \
  "$BENCH_DIR/k6/$script" > "$run_dir/k6.log" 2>&1
k6_exit=$?
set -e
ended_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
if [ -n "${PROFILER_PID:-}" ]; then
  wait "$PROFILER_PID" 2>/dev/null || true
  PROFILER_PID=
fi
if [ -n "${INCIDENT_PID:-}" ]; then
  # Instance bật lại trong sự cố phải xong trước khi dừng mọi thứ (tối đa thời gian chờ sẵn sàng của nó)
  wait "$INCIDENT_PID" 2>/dev/null || true
  INCIDENT_PID=
fi

kill -TERM "$PROC_PID" "$PROM_PID" 2>/dev/null || true
wait "$PROC_PID" "$PROM_PID" 2>/dev/null || true
PROC_PID='' PROM_PID=''
# Dừng app trước: backend PostgreSQL đẩy hết thống kê khi kết nối đóng. Nginx dừng trước để ghi hết log truy cập
stop_lb
stop_instances
stop_app
sleep 1
bash "$BENCH_DIR/collect/pg_stats.sh" dump "$run_dir"
if [ -f "$run_dir/nginx_access.log" ]; then
  "$PYTHON" "$BENCH_DIR/analysis/nginx_timeline.py" "$run_dir/nginx_access.log" "$run_dir/timeline.csv" \
    || log "CẢNH BÁO: không dựng được timeline.csv từ log của Nginx"
fi

# 0: đạt; 99: có ngưỡng bị vượt (với capacity là đã dừng sớm khi quá tải, bình thường)
status=ok
if [ "$k6_exit" != 0 ] && { [ "$k6_exit" != 99 ] || [ "$mode" != capacity ]; }; then
  status=failed
fi
META_RUN="$run" META_CONFIG="$config" META_WORKLOAD="$workload" META_MODE="$mode" META_LEVEL="$level" \
META_REP="$rep" META_PROFILE="$profile" META_STARTED="$started_at" META_ENDED="$ended_at" META_K6_EXIT="$k6_exit" \
META_STATUS="$status" META_SHA="$(cat "$(dirname "$jar")/git-sha" 2>/dev/null || echo unknown)" \
META_JVM="$JVM_OPTS" META_SUT="$SUT_CPUS" META_LOADGEN="$LOADGEN_CPUS" META_WARMUP="$WARMUP" META_MEASURE="$MEASURE" \
META_K6_ENV="${k6_env[*]}" META_APP_ARGS="${APP_ARGS:-}" META_MANIFEST="$DATASET_DIR/manifest.json" \
META_INSTANCES="$instances" META_INCIDENT="$incident" META_HEAP_MB="${INSTANCE_HEAP_MB:-}" \
META_POOL="${INSTANCE_POOL:-}" \
"$PYTHON" - "$run_dir/meta.json" << 'EOF'
import hashlib, json, os, sys
e = os.environ
manifest = e["META_MANIFEST"]
meta = {
    "run": e["META_RUN"], "config": e["META_CONFIG"], "git_sha": e["META_SHA"], "workload": e["META_WORKLOAD"],
    "mode": e["META_MODE"], "level": e["META_LEVEL"], "rep": e["META_REP"], "profiled": e["META_PROFILE"] == "1",
    "status": e["META_STATUS"], "k6_exit": int(e["META_K6_EXIT"]), "started_at": e["META_STARTED"],
    "ended_at": e["META_ENDED"], "warmup_s": int(e["META_WARMUP"]), "measure_s": int(e["META_MEASURE"]),
    "jvm_opts": e["META_JVM"], "sut_cpus": e["META_SUT"], "loadgen_cpus": e["META_LOADGEN"],
    "k6_args": e["META_K6_ENV"], "app_args": e["META_APP_ARGS"],
    "instances": int(e["META_INSTANCES"]), "incident": e["META_INCIDENT"] or None,
    "heap_mb_per_instance": int(e["META_HEAP_MB"]) if e["META_HEAP_MB"] else None,
    "pool_per_instance": int(e["META_POOL"]) if e["META_POOL"] else None,
    "dataset_manifest_sha256": hashlib.sha256(open(manifest, "rb").read()).hexdigest() if os.path.exists(manifest) else None,
}
with open(sys.argv[1], "w") as f:
    json.dump(meta, f, indent=1)
EOF
tail -n 25 "$run_dir/k6.log"
if [ "$status" != ok ]; then
  log "CẢNH BÁO: $run kết thúc với mã k6 $k6_exit (xem $run_dir/k6.log)"
fi
