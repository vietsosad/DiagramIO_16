#!/usr/bin/env bash
# In cấu hình máy và phần mềm của phiên đo (bench/kaggle/run_matrix.sh ghi vào env.txt).
# Máy Kaggle có thể khác nhau giữa các phiên, nên mọi kết quả phải đi kèm file này.
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"

section() { printf '\n== %s\n' "$1"; }

section "Thời điểm (UTC)"
date -u +%Y-%m-%dT%H:%M:%SZ
section "Hệ điều hành"
uname -a
grep PRETTY_NAME /etc/os-release
section "CPU (lscpu)"
lscpu
section "CPU từng nhân (lscpu -e: CORE trùng nhau = hai luồng hyper-thread của cùng một nhân)"
lscpu -e
section "CPU được phép dùng, hạn mức cgroup"
echo "nproc: $(nproc)"
grep -E '^Cpus_allowed_list' /proc/self/status
cat /sys/fs/cgroup/cpu.max 2>/dev/null | sed 's/^/cgroup v2 cpu.max: /'
cat /sys/fs/cgroup/cpuset.cpus.effective 2>/dev/null | sed 's/^/cgroup v2 cpuset.cpus.effective: /'
cat /sys/fs/cgroup/cpu/cpu.cfs_quota_us 2>/dev/null | sed 's/^/cgroup v1 cfs_quota_us: /'
for cpu in $(allowed_cpus); do
  echo "cpu$cpu thread_siblings: $(cat "/sys/devices/system/cpu/cpu$cpu/topology/thread_siblings_list" 2>/dev/null)"
done
section "Bố trí CPU của phép đo"
echo "hệ thống được đo (app, PostgreSQL, Redis, Nginx): $SUT_CPUS"
echo "tạo tải và thu số liệu (k6, bench/collect): $LOADGEN_CPUS"
section "Bộ nhớ, đĩa"
free -h
df -h "$WORK_DIR" 2>/dev/null || df -h /tmp
section "Phần mềm"
"$JAVA" -version 2>&1
"$K6" version 2>&1 | awk 'NR == 1'
"$ASPROF" --version 2>&1 | awk 'NR == 1'
"$PYTHON" --version
pg_admin -d postgres -At -c "SELECT version()"
redis-server --version 2>/dev/null || echo "redis-server: chưa cài"
"$NGINX" -v 2>&1 || echo "nginx: chưa cài"
section "Cấu hình PostgreSQL"
pg_admin -d postgres -At -F ' = ' -c "SELECT name, setting || COALESCE(unit, '') FROM pg_settings WHERE name IN
  ('shared_buffers', 'effective_cache_size', 'work_mem', 'max_connections', 'max_wal_size', 'checkpoint_timeout',
   'jit', 'synchronous_commit', 'shared_preload_libraries', 'track_io_timing', 'max_parallel_workers_per_gather',
   'random_page_cost') ORDER BY name"
section "Cấu hình Redis"
redis_cli CONFIG GET maxmemory 2>/dev/null | paste - - | sed 's/	/ = /'
redis_cli CONFIG GET maxmemory-policy 2>/dev/null | paste - - | sed 's/	/ = /'
redis_cli CONFIG GET save 2>/dev/null | paste - - | sed 's/	/ = /'
section "Ứng dụng"
echo "JVM_OPTS: $JVM_OPTS -XX:ActiveProcessorCount=$SUT_NCPU"
echo "cổng API $APP_PORT, cổng quản trị $MGMT_PORT"
section "Mã nguồn"
git -C "$REPO_DIR" rev-parse HEAD 2>/dev/null
git -C "$REPO_DIR" status --short 2>/dev/null | awk 'NR <= 20'
