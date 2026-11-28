#!/usr/bin/env bash
# Khởi động lại lần lượt từng bản sao của app trong docker-compose.scale.yml mà không làm rơi request nào (B4,
# docs/adr/0006-mo-rong-ngang-san-sang.md). Với mỗi bản sao: Nginx thôi gửi tới nó (đánh dấu "down", nạp lại cấu hình,
# chờ worker cũ thoát), Docker tắt êm rồi bật lại nó (SIGTERM, request đang chạy được làm xong), chờ readiness, rồi
# Nginx về lại danh sách mặc định.
#
#   deploy/rolling-restart.sh
#
# Chỉ khởi động lại các container đang chạy (cùng image, cùng cấu hình): Docker Compose không có cập nhật cuốn chiếu
# cho image mới (ADR-0006). COMPOSE_FILE, COMPOSE_PROJECT_NAME dùng như với lệnh docker compose.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

export COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.yml:docker-compose.scale.yml}"
export COMPOSE_PATH_SEPARATOR="${COMPOSE_PATH_SEPARATOR:-:}"
# Git Bash trên Windows: không đổi đường dẫn Linux truyền cho lệnh chạy trong container
export MSYS_NO_PATHCONV=1
RUNTIME_SERVERS=/etc/nginx/diagramio-runtime/servers.conf
# Lâu hơn SHUTDOWN_TIMEOUT của app (20 giây): Docker chờ tắt êm xong rồi mới SIGKILL
STOP_TIMEOUT="${STOP_TIMEOUT:-30}"

log() { echo "[$(date +%H:%M:%S)] $*"; }

nginx_workers() {
  docker compose exec -T nginx ps -o pid,args | awk '/worker process/ {print $1}' | sort
}

# Ghi danh sách instance cho Nginx rồi nạp lại. "nginx -s reload" trả về ngay: chờ tới khi không còn worker nào của
# cấu hình cũ, vì worker cũ phục vụ nốt kết nối của client theo cấu hình cũ, vẫn có thể gửi tới bản sao vừa bị đánh
# dấu down.
apply_servers() {
  local content=$1 before after
  before=$(nginx_workers)
  printf '%s\n' "$content" \
    | docker compose exec -T nginx sh -c "cat > $RUNTIME_SERVERS && nginx -t -q && nginx -s reload"
  for _ in $(seq 1 150); do
    after=$(nginx_workers)
    if [ -n "$after" ] && [ -z "$(comm -12 <(echo "$before") <(echo "$after"))" ]; then
      return 0
    fi
    sleep 0.1
  done
  echo "worker cũ của Nginx chưa thoát sau 15 giây" >&2
  return 1
}

wait_healthy() {
  local name=$1
  for _ in $(seq 1 180); do
    if [ "$(docker inspect -f '{{.State.Health.Status}}' "$name")" = healthy ]; then
      return 0
    fi
    sleep 1
  done
  echo "$name chưa sẵn sàng sau 180 giây" >&2
  return 1
}

mapfile -t replicas < <(docker compose ps --format '{{.Name}}' app | sort)
if [ "${#replicas[@]}" -lt 2 ]; then
  echo "cần ít nhất 2 bản sao đang chạy: docker compose -f docker-compose.yml -f docker-compose.scale.yml up -d --scale app=3" >&2
  exit 1
fi
default_servers=$(cat deploy/nginx/servers.conf)
# Dừng giữa chừng (lỗi, Ctrl-C) thì vẫn trả Nginx về danh sách mặc định
trap 'apply_servers "$default_servers" || true' EXIT

for replica in "${replicas[@]}"; do
  drained=''
  for other in "${replicas[@]}"; do
    flag=''
    [ "$other" = "$replica" ] && flag=' down'
    drained+="server $other:8080 resolve max_fails=1 fail_timeout=5s$flag;"$'\n'
  done
  log "$replica: Nginx thôi gửi request tới"
  apply_servers "$drained"
  log "$replica: tắt êm và khởi động lại"
  docker restart -t "$STOP_TIMEOUT" "$replica" > /dev/null
  wait_healthy "$replica"
  log "$replica: sẵn sàng, Nginx gửi request tới như cũ"
  apply_servers "$default_servers"
done
trap - EXIT
log "Xong: đã khởi động lại ${#replicas[@]} bản sao"
