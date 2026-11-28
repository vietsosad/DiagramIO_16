// Cấu hình một lần chạy, đọc từ biến môi trường (k6 run -e TÊN=giá_trị hoặc biến môi trường thường).
// Giá trị mặc định là của phép đo trên Kaggle (docs/adr/0001-phuong-phap-do.md).

function int(name, fallback) {
  const raw = __ENV[name];
  if (raw === undefined || raw === '') {
    return fallback;
  }
  const value = parseInt(raw, 10);
  if (isNaN(value)) {
    throw new Error(`${name} phải là số nguyên, nhận được "${raw}"`);
  }
  return value;
}

function float(name, fallback) {
  const raw = __ENV[name];
  if (raw === undefined || raw === '') {
    return fallback;
  }
  const value = parseFloat(raw);
  if (isNaN(value)) {
    throw new Error(`${name} phải là số, nhận được "${raw}"`);
  }
  return value;
}

export const BASE_URL = (__ENV.BASE_URL || 'http://127.0.0.1:8080').replace(/\/$/, '');
export const API = `${BASE_URL}/api/v1`;
export const FIXTURES = __ENV.FIXTURES || '/kaggle/tmp/dataset/fixtures';
export const OUT = __ENV.OUT || 'k6-summary.json';

// closed: số VU cố định, không nghỉ (như pha 1); fixed: tốc độ đến cố định; capacity: tăng tốc độ đến theo bậc
export const MODE = __ENV.MODE || 'fixed';
export const WARMUP = int('WARMUP', 60);           // giây, không tính vào kết quả
// fixed: số giây đầu warm-up tăng dần tốc độ từ RAMP_FROM lên RATE (0 = đủ RATE ngay từ đầu, như m0, b12, b3).
// JVM nguội nhận ngay 700 req/s thì quá tải suốt 3-4 phút đầu (phiên b3, ADR-0001), tăng dần thì không.
export const WARMUP_RAMP = int('WARMUP_RAMP', 0);
export const MEASURE = int('MEASURE', 180);        // giây đo (closed, fixed)
export const VUS = int('VUS', 10);                 // closed
export const RATE = int('RATE', 200);              // request/giây (fixed)
export const RATE_START = int('RATE_START', 100);  // capacity: bậc đầu, cũng là tốc độ lúc warm-up
export const RATE_STEP = int('RATE_STEP', 100);
export const RATE_MAX = int('RATE_MAX', 3000);
export const STEP = int('STEP', 30);               // giây mỗi bậc
export const SETTLE = int('SETTLE', 5);            // giây đầu mỗi bậc bị loại (đang chuyển bậc)
export const MAX_VUS = int('MAX_VUS', 1000);
export const PRE_VUS = int('PRE_VUS', 0);          // 0 = tự tính

// SLO dùng để báo cáo; ngưỡng dừng sớm chỉ để không chạy tiếp khi hệ thống đã quá tải rõ ràng
export const SLO_P95_MS = float('SLO_P95_MS', 100);
export const ABORT_P95_MS = float('ABORT_P95_MS', 1000);
export const ABORT_ERROR_RATE = float('ABORT_ERROR_RATE', 0.05);

export const ZIPF_S = float('ZIPF_S', 1.0);        // độ lệch của phân bố "sơ đồ nóng"
export const SIZE_CLASS = __ENV.SIZE_CLASS || '';  // 'L' cho W3: chỉ thao tác trên sơ đồ lớn
export const PAGE_SIZE = int('PAGE_SIZE', 20);
export const W1_PAYLOAD = __ENV.W1_PAYLOAD || 'tiny';

export const RAMP_FROM = int('RAMP_FROM', 50);    // fixed với WARMUP_RAMP > 0: tốc độ lúc bắt đầu tăng

if (['closed', 'fixed', 'capacity'].indexOf(MODE) < 0) {
  throw new Error(`MODE phải là closed, fixed hoặc capacity, nhận được "${MODE}"`);
}
if (WARMUP_RAMP < 0 || WARMUP_RAMP > WARMUP) {
  throw new Error(`WARMUP_RAMP phải trong khoảng 0..WARMUP (${WARMUP}), nhận được ${WARMUP_RAMP}`);
}

/** Cấu hình đã dùng, ghi kèm kết quả để mỗi lần chạy tự mô tả được. */
export const RUN_CONFIG = {
  baseUrl: BASE_URL, mode: MODE, warmup: WARMUP, warmupRamp: WARMUP_RAMP, rampFrom: RAMP_FROM,
  measure: MEASURE, vus: VUS, rate: RATE,
  rateStart: RATE_START, rateStep: RATE_STEP, rateMax: RATE_MAX, step: STEP, settle: SETTLE,
  maxVus: MAX_VUS, preVus: PRE_VUS, sloP95Ms: SLO_P95_MS, abortP95Ms: ABORT_P95_MS,
  abortErrorRate: ABORT_ERROR_RATE, zipfS: ZIPF_S, sizeClass: SIZE_CLASS, pageSize: PAGE_SIZE,
  w1Payload: W1_PAYLOAD,
};
