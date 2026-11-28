# shellcheck shell=bash
# shellcheck disable=SC2034 # các biến ở đây được dùng trong những script source file này
# Biến và hàm dùng chung cho các script trong bench/kaggle. Không chạy trực tiếp: "source env.sh".
# Mọi giá trị đều đổi được bằng biến môi trường trước khi chạy script.

BENCH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_DIR="$(cd "$BENCH_DIR/.." && pwd)"

# ---- Phiên bản phần mềm cố định (ghi lại trong env.txt của mỗi phiên đo)
JDK_VERSION="${JDK_VERSION:-21.0.13.1+2}"            # Eclipse Temurin
K6_VERSION="${K6_VERSION:-v2.3.0}"
ASYNC_PROFILER_VERSION="${ASYNC_PROFILER_VERSION:-4.6}"
PG_MAJOR="${PG_MAJOR:-16}"

# ---- Thư mục
TOOLS_DIR="${TOOLS_DIR:-/opt/diagramio-bench}"       # JDK, k6, async-profiler
WORK_DIR="${WORK_DIR:-/tmp/diagramio-bench}"         # dữ liệu sinh ra, bản build, file tạm
RESULTS_DIR="${RESULTS_DIR:-$REPO_DIR/bench/results}"
DATASET_DIR="${DATASET_DIR:-$WORK_DIR/dataset}"

JAVA_HOME="${JAVA_HOME_BENCH:-$TOOLS_DIR/jdk}"
JAVA="$JAVA_HOME/bin/java"
K6="$TOOLS_DIR/k6/k6"
ASPROF="$TOOLS_DIR/async-profiler/bin/asprof"
PYTHON="${PYTHON:-python3}"

# ---- PostgreSQL (cụm mặc định của gói Debian/Ubuntu: /etc/postgresql/$PG_MAJOR/main)
PG_PORT="${PG_PORT:-5432}"
PG_SHARED_BUFFERS="${PG_SHARED_BUFFERS:-4GB}"
PG_EFFECTIVE_CACHE="${PG_EFFECTIVE_CACHE:-16GB}"
DB_USER="${DB_USER:-bench}"
DB_PASSWORD="${DB_PASSWORD:-bench}"
DB_TEMPLATE="${DB_TEMPLATE:-bench_template}"
DB_RUN="${DB_RUN:-bench_run}"

# ---- Redis (cache L2 của B2, chỉ cấu hình two-level dùng; các cấu hình khác để nó chạy không)
REDIS_PORT="${REDIS_PORT:-6379}"
REDIS_MAXMEMORY="${REDIS_MAXMEMORY:-2gb}"
REDIS_VERSION="${REDIS_VERSION:-7.4}"                 # bản chính lấy từ packages.redis.io (khớp Testcontainers)

# ---- Ứng dụng
APP_PORT="${APP_PORT:-8080}"
MGMT_PORT="${MGMT_PORT:-9090}"
JVM_HEAP="${JVM_HEAP:-2g}"
JVM_OPTS="${JVM_OPTS:--Xms$JVM_HEAP -Xmx$JVM_HEAP -XX:+UseG1GC -XX:+AlwaysPreTouch}"
JWT_SECRET="${JWT_SECRET:-diagramio-bench-only-secret-not-for-production-0123456789}"
# Access token sống đủ lâu cho một lần chạy dài (mặc định 15 phút); không ảnh hưởng chi phí kiểm tra token
JWT_MINUTES="${JWT_MINUTES:-480}"
# Tham số chung cho mọi cấu hình, kể cả bản dựng từ tag cũ, để phép so sánh luôn công bằng. Flame graph của M0
# cho thấy Spring Security quan sát (observation) từng filter và mỗi lần cập nhật một LongTaskTimer O(n) theo số
# request đang chạy: gần 8% CPU của app ở W2 là chi phí của chính phần đo. Hai cờ dưới đây tắt phần đó mà vẫn
# giữ số liệu http.server.requests.
BENCH_APP_ARGS="${BENCH_APP_ARGS---management.observations.enable.spring.security=false --management.observations.long-task-timer.enabled=false}"

log() { echo "[$(date +%H:%M:%S)] $*"; }
die() { echo "[$(date +%H:%M:%S)] LỖI: $*" >&2; exit 1; }

# ---- Bố trí CPU: hệ thống được đo (app, PostgreSQL) và máy tạo tải (k6, bộ thu số liệu) không dùng chung CPU.
# Mặc định lấy 4 CPU đầu tiên mà tiến trình được phép dùng: 3 cho hệ thống được đo, 1 cho tạo tải (giống Kaggle
# 4 vCPU). Máy có nhiều CPU hơn vẫn chỉ dùng 4 CPU này để kết quả so được với Kaggle.
allowed_cpus() {
  # Cpus_allowed_list có dạng "0-3,8-9"; đổi thành "0 1 2 3 8 9" (không cần python, chạy được trước khi cài gì)
  local list part i out=()
  list=$(awk '/^Cpus_allowed_list:/ {print $2}' /proc/self/status)
  IFS=, read -r -a parts <<< "$list"
  for part in "${parts[@]}"; do
    if [[ $part == *-* ]]; then
      for ((i = ${part%-*}; i <= ${part#*-}; i++)); do out+=("$i"); done
    else
      out+=("$part")
    fi
  done
  echo "${out[*]}"
}

cpu_layout() {
  if [ -n "${SUT_CPUS:-}" ] && [ -n "${LOADGEN_CPUS:-}" ]; then
    return
  fi
  local cpus
  read -r -a cpus <<< "$(allowed_cpus)"
  if [ "${#cpus[@]}" -lt 4 ]; then
    die "cần ít nhất 4 CPU để tách hệ thống được đo và máy tạo tải, máy này có ${#cpus[@]}"
  fi
  SUT_CPUS="${cpus[0]},${cpus[1]},${cpus[2]}"
  LOADGEN_CPUS="${cpus[3]}"
}
cpu_layout
SUT_NCPU=$(awk -F, '{print NF}' <<< "$SUT_CPUS")

# psql không in NOTICE (ví dụ "database ... does not exist, skipping") để log gọn
pg_admin() { PGOPTIONS='-c client_min_messages=warning' runuser -u postgres -- psql -X -q -v ON_ERROR_STOP=1 -p "$PG_PORT" "$@"; }
pg_bench() {
  PGOPTIONS='-c client_min_messages=warning' PGPASSWORD="$DB_PASSWORD" \
    psql -X -q -v ON_ERROR_STOP=1 -h 127.0.0.1 -p "$PG_PORT" -U "$DB_USER" "$@"
}

# Chờ một URL trả 200, tối đa $2 giây
wait_http() {
  local url=$1 timeout=${2:-120} waited=0
  until curl -fsS -o /dev/null "$url" 2>/dev/null; do
    sleep 1
    waited=$((waited + 1))
    if [ "$waited" -ge "$timeout" ]; then
      return 1
    fi
  done
}

# Khởi động app trên CPU của hệ thống được đo. start_app <jar> <database> <thư mục log> [tham số Spring thêm...]
start_app() {
  local jar=$1 database=$2 logdir=$3
  shift 3
  # shellcheck disable=SC2086 # JVM_OPTS cố ý được tách thành nhiều tham số
  taskset -c "$SUT_CPUS" "$JAVA" $JVM_OPTS -XX:ActiveProcessorCount="$SUT_NCPU" \
    -Xlog:gc:file="$logdir/gc.log":uptime,level,tags \
    -jar "$jar" \
    --server.port="$APP_PORT" --management.server.port="$MGMT_PORT" \
    --spring.datasource.url="jdbc:postgresql://127.0.0.1:$PG_PORT/$database" \
    --spring.datasource.username="$DB_USER" --spring.datasource.password="$DB_PASSWORD" \
    --jwt.secret="$JWT_SECRET" --jwt.access-token-minutes="$JWT_MINUTES" \
    $BENCH_APP_ARGS "$@" > "$logdir/app.log" 2>&1 &
  APP_PID=$!
  if ! wait_http "http://127.0.0.1:$APP_PORT/api/v1/health" 180; then
    tail -n 60 "$logdir/app.log" >&2
    kill -KILL "$APP_PID" 2>/dev/null || true
    die "app không khởi động được (log: $logdir/app.log)"
  fi
}

# Dừng app nhẹ nhàng (SIGTERM), quá 30 giây thì SIGKILL
stop_app() {
  [ -n "${APP_PID:-}" ] || return 0
  kill -TERM "$APP_PID" 2>/dev/null || true
  local waited=0
  while kill -0 "$APP_PID" 2>/dev/null; do
    sleep 1
    waited=$((waited + 1))
    if [ "$waited" -ge 30 ]; then
      kill -KILL "$APP_PID" 2>/dev/null || true
      break
    fi
  done
  wait "$APP_PID" 2>/dev/null || true
  APP_PID=
}

# ---- Nhiều instance sau Nginx (B4, docs/adr/0006-mo-rong-ngang-san-sang.md). Nginx nhận ở APP_PORT như app khi
# chạy một mình, nên k6 không đổi gì; instance i nghe APP_PORT+i, cổng quản trị MGMT_PORT+i. Heap (JVM_HEAP) và tổng
# số kết nối CSDL (POOL_TOTAL) chia đều cho các instance, để so 1, 2, 3 instance trên cùng tài nguyên.
POOL_TOTAL="${POOL_TOTAL:-12}"
NGINX="${NGINX:-nginx}"
declare -A INSTANCE_PID=()

instance_port() { echo $((APP_PORT + $1)); }
instance_mgmt() { echo $((MGMT_PORT + $1)); }

# Số MB của một kích thước kiểu JVM (2g, 1536m)
to_mb() {
  case "$1" in
    *[gG]) echo $(( ${1%[gG]} * 1024 )) ;;
    *[mM]) echo "${1%[mM]}" ;;
    *) die "kích thước không hiểu được: $1" ;;
  esac
}

# start_instances <n> <jar> <database> <thư mục log> [tham số Spring thêm...]: khởi động n instance rồi chờ cả n sẵn sàng
start_instances() {
  INSTANCES=$1 INSTANCE_JAR=$2 INSTANCE_DB=$3 INSTANCE_LOGDIR=$4
  shift 4
  INSTANCE_ARGS=("$@")
  INSTANCE_HEAP_MB=$(( $(to_mb "$JVM_HEAP") / INSTANCES ))
  INSTANCE_POOL=$(( POOL_TOTAL / INSTANCES ))
  local i
  for i in $(seq 1 "$INSTANCES"); do
    launch_instance "$i"
  done
  for i in $(seq 1 "$INSTANCES"); do
    if ! wait_ready "$i" 180; then
      tail -n 60 "$INSTANCE_LOGDIR/app-$i.log" >&2
      die "instance $i không khởi động được (log: $INSTANCE_LOGDIR/app-$i.log)"
    fi
  done
  # Bộ lấy flame graph gắn vào instance đầu tiên
  APP_PID=${INSTANCE_PID[1]}
}

# Khởi động (lại) instance i, không chờ. -Xms/-Xmx đặt sau JVM_OPTS nên thắng giá trị trong đó
launch_instance() {
  local i=$1
  # shellcheck disable=SC2086 # JVM_OPTS, BENCH_APP_ARGS cố ý được tách thành nhiều tham số
  taskset -c "$SUT_CPUS" "$JAVA" $JVM_OPTS -Xms"${INSTANCE_HEAP_MB}"m -Xmx"${INSTANCE_HEAP_MB}"m \
    -XX:ActiveProcessorCount="$SUT_NCPU" -Xlog:gc:file="$INSTANCE_LOGDIR/gc-$i-%p.log":uptime,level,tags \
    -jar "$INSTANCE_JAR" \
    --server.port="$(instance_port "$i")" --management.server.port="$(instance_mgmt "$i")" \
    --spring.datasource.url="jdbc:postgresql://127.0.0.1:$PG_PORT/$INSTANCE_DB" \
    --spring.datasource.username="$DB_USER" --spring.datasource.password="$DB_PASSWORD" \
    --spring.datasource.hikari.maximum-pool-size="$INSTANCE_POOL" \
    --jwt.secret="$JWT_SECRET" --jwt.access-token-minutes="$JWT_MINUTES" \
    $BENCH_APP_ARGS "${INSTANCE_ARGS[@]}" >> "$INSTANCE_LOGDIR/app-$i.log" 2>&1 &
  INSTANCE_PID[$i]=$!
}

# Readiness của instance i trên cổng quản trị (Actuator, B4)
wait_ready() {
  wait_http "http://127.0.0.1:$(instance_mgmt "$1")/actuator/health/readiness" "${2:-180}"
}

# PID của instance i, tìm theo cổng của nó: instance có thể đã được bật lại trong một tiến trình khác (sự cố của W7
# chạy trong subshell nền), nên không dựa vào INSTANCE_PID của shell này
instance_pid() {
  pgrep -f -- "--server\.port=$(instance_port "$1")( |$)" | head -n 1 || true
}

# Chờ tiến trình kết thúc tối đa $2 giây (không dùng wait: tiến trình có thể không phải con của shell này)
await_exit() {
  local pid=$1 limit=$(( ${2:-30} * 5 )) n=0
  while kill -0 "$pid" 2>/dev/null && [ "$n" -lt "$limit" ]; do
    sleep 0.2
    n=$((n + 1))
  done
  ! kill -0 "$pid" 2>/dev/null
}

# Dừng instance i: kill_instance <i> (SIGKILL, như máy chết) hoặc term_instance <i> (SIGTERM, tắt êm; quá 30 giây SIGKILL)
kill_instance() {
  local pid
  pid=$(instance_pid "$1")
  [ -n "$pid" ] || return 0
  kill -KILL "$pid" 2>/dev/null || true
  await_exit "$pid" 10 || true
  INSTANCE_PID[$1]=''
}

term_instance() {
  local pid
  pid=$(instance_pid "$1")
  [ -n "$pid" ] || return 0
  kill -TERM "$pid" 2>/dev/null || true
  if ! await_exit "$pid" 30; then
    kill -KILL "$pid" 2>/dev/null || true
    await_exit "$pid" 10 || true
  fi
  INSTANCE_PID[$1]=''
}

# Dừng mọi instance của lần chạy (cùng lúc, mỗi instance tắt êm)
stop_instances() {
  [ -n "${INSTANCES:-}" ] || return 0
  local i pid pids=()
  for i in $(seq 1 "$INSTANCES"); do
    pid=$(instance_pid "$i")
    if [ -n "$pid" ]; then
      pids+=("$pid")
      kill -TERM "$pid" 2>/dev/null || true
    fi
  done
  for pid in "${pids[@]}"; do
    await_exit "$pid" 30 || { kill -KILL "$pid" 2>/dev/null || true; await_exit "$pid" 10 || true; }
  done
  INSTANCE_PID=()
  INSTANCES=
  APP_PID=
}

# Cấu hình Nginx của một lần chạy, dùng đúng deploy/nginx/http.conf và server.conf như docker-compose.scale.yml.
# lb_config <n> <thư mục> [instance đang được đánh dấu down]. Nginx cũ hơn 1.27.4 không có keepalive_min_timeout: dùng
# bản http.conf bỏ dòng đó (env.txt ghi phiên bản Nginx).
lb_config() {
  local n=$1 dir=$2 drained=${3:-0} servers='' i flag http_conf="$REPO_DIR/deploy/nginx/http.conf"
  for i in $(seq 1 "$n"); do
    flag=''
    [ "$i" = "$drained" ] && flag=' down'
    servers+="        server 127.0.0.1:$(instance_port "$i") max_fails=1 fail_timeout=5s$flag;"$'\n'
  done
  if ! "$NGINX" -V 2>&1 | grep -qE 'nginx/(1\.(2[89]|[3-9][0-9])|1\.27\.([4-9]|[1-9][0-9]))'; then
    grep -v 'keepalive_min_timeout' "$http_conf" > "$dir/nginx-http.conf"
    http_conf="$dir/nginx-http.conf"
  fi
  mkdir -p "$dir/nginx-tmp"
  cat << EOF
# Sinh bởi bench/kaggle/env.sh (lb_config) cho lần chạy này
user root;
worker_processes 2;
worker_rlimit_nofile 65536;
pid $dir/nginx.pid;
error_log $dir/nginx_error.log warn;
events {
    worker_connections 16384;
}
http {
    include $http_conf;
    access_log $dir/nginx_access.log diagramio buffer=256k flush=1s;
    client_body_temp_path $dir/nginx-tmp/body;
    proxy_temp_path $dir/nginx-tmp/proxy;
    # Như deploy/nginx/nginx.conf: vùng nhớ chung để các worker cùng thấy số request dở (least_conn) và instance lỗi
    upstream diagramio {
        zone diagramio 256k;
        least_conn;
$servers        keepalive 32;
        keepalive_timeout 30s;
    }
    upstream diagramio_fresh {
        zone diagramio_fresh 256k;
        least_conn;
$servers    }
    server {
        listen 127.0.0.1:$APP_PORT;
        include $REPO_DIR/deploy/nginx/server.conf;
    }
}
EOF
}

# start_lb <n> <thư mục>: Nginx trên CPU của hệ thống được đo, trước n instance
start_lb() {
  LB_DIR=$2 LB_INSTANCES=$1
  lb_config "$1" "$2" > "$2/nginx.conf"
  taskset -c "$SUT_CPUS" "$NGINX" -c "$2/nginx.conf" -p "$2/nginx-tmp" \
    || die "Nginx không khởi động được (xem $2/nginx_error.log)"
  wait_http "http://127.0.0.1:$APP_PORT/api/v1/health" 30 || die "Nginx không trả lời trên cổng $APP_PORT"
}

lb_workers() { pgrep -P "$(cat "$LB_DIR/nginx.pid")" | sort; }

# reload_lb [instance đánh dấu down]: nạp lại cấu hình rồi chờ tới khi không còn worker nào của cấu hình cũ (worker cũ
# phục vụ nốt kết nối của client theo cấu hình cũ, vẫn có thể gửi tới instance vừa bị đánh dấu down)
reload_lb() {
  local before after
  lb_config "$LB_INSTANCES" "$LB_DIR" "${1:-0}" > "$LB_DIR/nginx.conf"
  before=$(lb_workers)
  kill -HUP "$(cat "$LB_DIR/nginx.pid")"
  for _ in $(seq 1 150); do
    after=$(lb_workers)
    if [ -n "$after" ] && [ -z "$(comm -12 <(echo "$before") <(echo "$after"))" ]; then
      return 0
    fi
    sleep 0.1
  done
  log "CẢNH BÁO: worker cũ của Nginx chưa thoát sau 15 giây"
}

stop_lb() {
  [ -n "${LB_DIR:-}" ] && [ -f "$LB_DIR/nginx.pid" ] || return 0
  local pid
  pid=$(cat "$LB_DIR/nginx.pid")
  kill -QUIT "$pid" 2>/dev/null || true
  for _ in $(seq 1 100); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.1
  done
  kill -KILL "$pid" 2>/dev/null || true
  LB_DIR=
}

redis_cli() { redis-cli -h 127.0.0.1 -p "$REDIS_PORT" "$@"; }

# Bật Redis nếu chưa chạy: trên CPU của hệ thống được đo (như PostgreSQL), chỉ nghe localhost, không ghi xuống
# đĩa (chỉ là cache). Máy chưa cài Redis (setup cũ) thì bỏ qua: các cấu hình không dùng Redis vẫn chạy được.
ensure_redis() {
  command -v redis-server > /dev/null || return 0
  redis_cli ping > /dev/null 2>&1 && return 0
  mkdir -p "$WORK_DIR"
  taskset -c "$SUT_CPUS" redis-server --port "$REDIS_PORT" --bind 127.0.0.1 --save '' --appendonly no \
    --maxmemory "$REDIS_MAXMEMORY" --maxmemory-policy volatile-lru --daemonize yes --logfile "$WORK_DIR/redis.log"
  for _ in $(seq 1 30); do
    redis_cli ping > /dev/null 2>&1 && return 0
    sleep 0.5
  done
  die "Redis không lên (log: $WORK_DIR/redis.log)"
}

# CSDL của một lần chạy: chép nguyên từ bench_template rồi nạp vào bộ đệm, để lần chạy nào cũng bắt đầu
# từ cùng một trạng thái (dữ liệu ghi của lần trước không ảnh hưởng lần sau). Cache Redis cũng được xóa sạch.
reset_run_db() {
  if command -v redis-server > /dev/null; then
    ensure_redis
    redis_cli FLUSHALL > /dev/null
  fi
  pg_admin -d postgres -c "DROP DATABASE IF EXISTS $DB_RUN WITH (FORCE)"
  pg_admin -d postgres -c "CREATE DATABASE $DB_RUN TEMPLATE $DB_TEMPLATE OWNER $DB_USER STRATEGY FILE_COPY"
  pg_admin -d "$DB_RUN" -c "SELECT count(pg_prewarm(c.oid)) FROM pg_class c
      JOIN pg_namespace n ON n.oid = c.relnamespace
      WHERE n.nspname IN ('public', 'pg_toast') AND c.relkind IN ('r', 'i', 't')" > /dev/null
  pg_admin -d postgres -c "CHECKPOINT"
}
