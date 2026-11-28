#!/usr/bin/env bash
# Cài mọi thứ cho phép đo trên một máy Linux trắng: Kaggle Notebook CPU (chạy bằng root, không có systemd,
# phải bật Internet) hoặc container Ubuntu/Debian để thử. Chạy lại nhiều lần được.
#
#   bash bench/kaggle/setup.sh
#
# Cài: PostgreSQL $PG_MAJOR (kho PGDG, có pg_stat_statements và pg_prewarm), Redis, Nginx, JDK Temurin, k6,
# async-profiler.
# Cấu hình PostgreSQL một lần ở đây và dùng chung cho mọi cấu hình được đo.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"

[ "$(id -u)" = 0 ] || die "cần chạy bằng root (Kaggle mặc định là root)"
export DEBIAN_FRONTEND=noninteractive

case "$(uname -m)" in
  x86_64) JDK_ARCH=x64; K6_ARCH=amd64; AP_ARCH=x64 ;;
  aarch64) JDK_ARCH=aarch64; K6_ARCH=arm64; AP_ARCH=arm64 ;;
  *) die "kiến trúc $(uname -m) chưa được hỗ trợ" ;;
esac
# shellcheck disable=SC1091
. /etc/os-release
CODENAME="${VERSION_CODENAME:-}"
[ -n "$CODENAME" ] || die "không đọc được VERSION_CODENAME trong /etc/os-release"
log "Hệ điều hành: $PRETTY_NAME ($CODENAME), $(uname -m), CPU được phép: $(allowed_cpus)"

# ---- Gói hệ thống
# Image Kaggle có sẵn nhiều nguồn apt của bên thứ ba; một nguồn hỏng không được làm dừng cả bước cài đặt
# (nếu gói cần thiết thật sự không cài được thì apt-get install bên dưới vẫn báo lỗi)
apt_update() {
  apt-get update -qq --allow-releaseinfo-change || log "CẢNH BÁO: apt-get update có nguồn lỗi, vẫn tiếp tục"
}

log "Cài gói hệ thống"
apt_update
apt-get install -y -qq --no-install-recommends ca-certificates curl gnupg git procps util-linux tar gzip > /dev/null
# Kaggle đã có python3 kèm matplotlib; chỉ cài từ apt khi thiếu (ví dụ container Ubuntu trắng)
if ! command -v "$PYTHON" > /dev/null || ! "$PYTHON" -c 'import matplotlib' 2> /dev/null; then
  apt-get install -y -qq --no-install-recommends python3 python3-matplotlib > /dev/null
fi

# ---- PostgreSQL từ kho PGDG (cùng phiên bản chính trên mọi bản Ubuntu/Debian)
if ! command -v "/usr/lib/postgresql/$PG_MAJOR/bin/postgres" > /dev/null; then
  log "Cài PostgreSQL $PG_MAJOR (PGDG, $CODENAME-pgdg)"
  install -d /usr/share/postgresql-common/pgdg
  curl -fsSL -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
    https://www.postgresql.org/media/keys/ACCC4CF8.asc
  echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt $CODENAME-pgdg main" \
    > /etc/apt/sources.list.d/pgdg.list
  apt_update
  # Container không có systemd: postinst có thể báo không khởi động được dịch vụ, cụm sẽ được bật bên dưới
  apt-get install -y -qq "postgresql-$PG_MAJOR" > /dev/null || true
fi
[ -x "/usr/lib/postgresql/$PG_MAJOR/bin/postgres" ] || die "cài PostgreSQL $PG_MAJOR thất bại"
if ! pg_lsclusters -h | awk '{print $1" "$2}' | grep -qx "$PG_MAJOR main"; then
  pg_createcluster "$PG_MAJOR" main > /dev/null
fi

# Cấu hình cho phép đo: đủ bộ đệm để dữ liệu nằm trong RAM, không checkpoint giữa lần đo (checkpoint tay trước
# mỗi lần chạy), tắt JIT (truy vấn OLTP ngắn; JIT chỉ thêm vài ms biên dịch cho các lần quét lớn), bật thống kê.
cat > "/etc/postgresql/$PG_MAJOR/main/conf.d/90-diagramio-bench.conf" << EOF
# Sinh bởi bench/kaggle/setup.sh
listen_addresses = '127.0.0.1'
port = $PG_PORT
max_connections = 200
shared_buffers = $PG_SHARED_BUFFERS
effective_cache_size = $PG_EFFECTIVE_CACHE
work_mem = 16MB
maintenance_work_mem = 512MB
max_wal_size = 8GB
checkpoint_timeout = 30min
jit = off
shared_preload_libraries = 'pg_stat_statements'
pg_stat_statements.max = 10000
pg_stat_statements.track = top
track_io_timing = on
EOF

log "Khởi động PostgreSQL trên CPU $SUT_CPUS"
if pg_lsclusters -h | awk -v v="$PG_MAJOR" '$1 == v && $2 == "main" {print $4}' | grep -q online; then
  pg_ctlcluster "$PG_MAJOR" main stop
fi
# Mọi tiến trình con của postmaster thừa hưởng tập CPU này
taskset -c "$SUT_CPUS" pg_ctlcluster "$PG_MAJOR" main start
for _ in $(seq 1 30); do
  pg_isready -q -h 127.0.0.1 -p "$PG_PORT" && break
  sleep 1
done
pg_isready -q -h 127.0.0.1 -p "$PG_PORT" || die "PostgreSQL không lên"

pg_admin -d postgres << EOF
SET client_min_messages = warning;
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '$DB_USER') THEN
    CREATE ROLE $DB_USER LOGIN CREATEDB PASSWORD '$DB_PASSWORD';
  END IF;
END
\$\$;
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
-- Đọc được thống kê của mọi phiên (bench/collect/pg_stats.sh xuất số liệu bằng user này)
GRANT pg_read_all_stats TO $DB_USER;
EOF

# ---- Redis $REDIS_VERSION từ packages.redis.io (cùng bản chính với docker-compose và Testcontainers); kho đó
# không có bản này thì dùng bản của hệ điều hành (Redis 6 trở lên đủ cho mọi lệnh app dùng). Phiên bản thật được
# ghi vào env.txt.
if ! command -v redis-server > /dev/null; then
  log "Cài Redis $REDIS_VERSION"
  if curl -fsSL https://packages.redis.io/gpg | gpg --dearmor --yes -o /usr/share/keyrings/redis-archive-keyring.gpg; then
    echo "deb [signed-by=/usr/share/keyrings/redis-archive-keyring.gpg] https://packages.redis.io/deb $CODENAME main" \
      > /etc/apt/sources.list.d/redis.list
    apt_update
  else
    log "CẢNH BÁO: không thêm được kho packages.redis.io"
  fi
  # Bản mới nhất của dòng $REDIS_VERSION trong kho (ví dụ 6:7.4.2-1rl1~jammy1). awk đọc hết đầu vào thay vì
  # dừng ở dòng đầu khớp: dừng sớm làm apt-cache nhận SIGPIPE, và với pipefail thì cả script dừng theo.
  redis_pkg=$(apt-cache madison redis-server 2>/dev/null \
    | awk -v v="$REDIS_VERSION" '{gsub(/ /, "", $3)} !found && index($3, ":" v ".") > 0 {print $3; found = 1}')
  if [ -n "$redis_pkg" ]; then
    apt-get install -y -qq --no-install-recommends "redis-server=$redis_pkg" "redis-tools=$redis_pkg" > /dev/null || true
  fi
  if ! command -v redis-server > /dev/null; then
    log "CẢNH BÁO: không có Redis $REDIS_VERSION, dùng bản của hệ điều hành"
    apt-get install -y -qq --no-install-recommends redis-server > /dev/null || true
  fi
fi
command -v redis-server > /dev/null || die "cài Redis thất bại"
# Gói có thể đã tự bật một redis-server cấu hình mặc định (có ghi đĩa, không ghim CPU): tắt đi, bật bản của bộ đo
redis_cli shutdown nosave > /dev/null 2>&1 || true
sleep 1
ensure_redis

# ---- Nginx (B4): bản stable của nginx.org, cùng dòng với image nginx:1.28-alpine của docker-compose.scale.yml
# (cần từ 1.27.4 cho keepalive_min_timeout). Kho đó không dùng được thì dùng bản của hệ điều hành; lb_config trong
# env.sh bỏ chỉ thị mà bản cũ không có, và env.txt ghi phiên bản thật.
if ! command -v "$NGINX" > /dev/null; then
  log "Cài Nginx (nginx.org, stable)"
  if curl -fsSL https://nginx.org/keys/nginx_signing.key | gpg --dearmor --yes -o /usr/share/keyrings/nginx-archive-keyring.gpg; then
    echo "deb [signed-by=/usr/share/keyrings/nginx-archive-keyring.gpg] https://nginx.org/packages/$ID $CODENAME nginx" \
      > /etc/apt/sources.list.d/nginx.list
    # Ưu tiên kho nginx.org hơn bản của hệ điều hành (cùng tên gói nginx)
    printf 'Package: *\nPin: origin nginx.org\nPin: release o=nginx\nPin-Priority: 900\n' > /etc/apt/preferences.d/99nginx
    apt_update
  else
    log "CẢNH BÁO: không thêm được kho nginx.org"
  fi
  apt-get install -y -qq --no-install-recommends nginx > /dev/null || true
fi
command -v "$NGINX" > /dev/null || die "cài Nginx thất bại"
# Gói có thể đã tự bật Nginx với cấu hình mặc định (cổng 80): mỗi lần chạy bộ đo tự bật Nginx của nó (env.sh, start_lb)
if pgrep -x nginx > /dev/null; then
  "$NGINX" -s quit 2> /dev/null || pkill -x nginx || true
fi

# ---- Công cụ tải về, có kiểm tra SHA-256 khi bản phát hành có công bố
download() {  # download <url> <file>; thử lại cả khi đường truyền đứt giữa chừng
  curl -fsSL --retry 5 --retry-all-errors --retry-delay 3 -o "$2" "$1"
}

install -d "$TOOLS_DIR"
TMP_DL=$(mktemp -d)
trap 'rm -rf "$TMP_DL"' EXIT

if [ ! -x "$JAVA" ] || ! "$JAVA" -version 2>&1 | grep -q "${JDK_VERSION%%+*}"; then
  log "Cài JDK Temurin $JDK_VERSION"
  jdk_file="OpenJDK21U-jdk_${JDK_ARCH}_linux_hotspot_${JDK_VERSION/+/_}.tar.gz"
  jdk_url="https://github.com/adoptium/temurin21-binaries/releases/download/jdk-${JDK_VERSION/+/%2B}/$jdk_file"
  download "$jdk_url" "$TMP_DL/$jdk_file"
  download "$jdk_url.sha256.txt" "$TMP_DL/jdk.sha256"
  (cd "$TMP_DL" && sha256sum -c --status <(awk -v f="$jdk_file" '{print $1"  "f}' jdk.sha256)) \
    || die "SHA-256 của JDK không khớp"
  rm -rf "$JAVA_HOME" && mkdir -p "$JAVA_HOME"
  tar -xzf "$TMP_DL/$jdk_file" -C "$JAVA_HOME" --strip-components=1
fi

if [ ! -x "$K6" ] || ! "$K6" version 2>/dev/null | grep -q "${K6_VERSION#v}"; then
  log "Cài k6 $K6_VERSION"
  k6_file="k6-$K6_VERSION-linux-$K6_ARCH.tar.gz"
  download "https://github.com/grafana/k6/releases/download/$K6_VERSION/$k6_file" "$TMP_DL/$k6_file"
  download "https://github.com/grafana/k6/releases/download/$K6_VERSION/k6-$K6_VERSION-checksums.txt" "$TMP_DL/k6.sha256"
  (cd "$TMP_DL" && grep " $k6_file\$" k6.sha256 | sha256sum -c --status) || die "SHA-256 của k6 không khớp"
  rm -rf "$TOOLS_DIR/k6" && mkdir -p "$TOOLS_DIR/k6"
  tar -xzf "$TMP_DL/$k6_file" -C "$TOOLS_DIR/k6" --strip-components=1
fi

if [ ! -x "$ASPROF" ]; then
  log "Cài async-profiler $ASYNC_PROFILER_VERSION"
  ap_file="async-profiler-$ASYNC_PROFILER_VERSION-linux-$AP_ARCH.tar.gz"
  download "https://github.com/async-profiler/async-profiler/releases/download/v$ASYNC_PROFILER_VERSION/$ap_file" \
    "$TMP_DL/$ap_file"
  rm -rf "$TOOLS_DIR/async-profiler" && mkdir -p "$TOOLS_DIR/async-profiler"
  tar -xzf "$TMP_DL/$ap_file" -C "$TOOLS_DIR/async-profiler" --strip-components=1
fi

log "Sẵn sàng:"
"$JAVA" -version 2>&1 | head -1
"$K6" version | head -1
"$ASPROF" --version 2>&1 | head -1
pg_admin -d postgres -At -c "SELECT version()"
redis-server --version
"$NGINX" -v 2>&1
log "CPU hệ thống được đo: $SUT_CPUS; CPU tạo tải: $LOADGEN_CPUS"
