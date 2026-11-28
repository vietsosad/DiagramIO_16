// Cách chạy một kịch bản: mô hình tải (closed, fixed, capacity), gắn pha warm-up/đo cho từng request,
// ngưỡng để k6 tính số liệu riêng cho pha đo, và file tóm tắt mà bench/analysis/report.py đọc.
import exec from 'k6/execution';
import { Gauge } from 'k6/metrics';
import {
  ABORT_ERROR_RATE, ABORT_P95_MS, MAX_VUS, MEASURE, MODE, OUT, PRE_VUS, RAMP_FROM, RATE, RATE_MAX, RATE_START,
  RATE_STEP, RUN_CONFIG, SETTLE, STEP, VUS, WARMUP, WARMUP_RAMP,
} from './config.js';

// Thời điểm kịch bản bắt đầu (ms từ epoch), để đối chiếu với số liệu phía server theo thời gian
const scenarioStart = new Gauge('scenario_start_ms');
let startRecorded = false;

function pad2(n) {
  return n < 10 ? `0${n}` : `${n}`;
}

/** Các bậc của chế độ capacity: tên bậc, tốc độ, thời điểm bắt đầu/kết thúc (giây kể từ đầu kịch bản). */
export const STEPS = (() => {
  const steps = [];
  if (MODE !== 'capacity') {
    return steps;
  }
  let start = WARMUP;
  for (let rate = RATE_START, k = 0; rate <= RATE_MAX; rate += RATE_STEP, k++) {
    steps.push({ stage: `s${pad2(k)}`, rate, start, end: start + STEP });
    start += STEP;
  }
  return steps;
})();

function preAllocated(rate) {
  return PRE_VUS > 0 ? PRE_VUS : Math.min(MAX_VUS, Math.max(50, Math.ceil(rate * 0.25)));
}

function scenario() {
  if (MODE === 'closed') {
    return { executor: 'constant-vus', vus: VUS, duration: `${WARMUP + MEASURE}s`, gracefulStop: '30s' };
  }
  if (MODE === 'fixed' && WARMUP_RAMP > 0) {
    // Tăng tuyến tính từ RAMP_FROM lên RATE trong WARMUP_RAMP giây đầu, rồi giữ RATE tới hết pha đo. Pha đo bắt đầu
    // sau WARMUP như mọi chế độ, nên đã chạy đủ RATE ít nhất WARMUP - WARMUP_RAMP giây.
    const from = Math.min(RATE, Math.max(1, RAMP_FROM));
    return {
      executor: 'ramping-arrival-rate', startRate: from, timeUnit: '1s',
      stages: [
        { target: RATE, duration: `${WARMUP_RAMP}s` },
        { target: RATE, duration: `${WARMUP - WARMUP_RAMP + MEASURE}s` },
      ],
      preAllocatedVUs: preAllocated(RATE), maxVUs: MAX_VUS, gracefulStop: '30s',
    };
  }
  if (MODE === 'fixed') {
    return {
      executor: 'constant-arrival-rate', rate: RATE, timeUnit: '1s', duration: `${WARMUP + MEASURE}s`,
      preAllocatedVUs: preAllocated(RATE), maxVUs: MAX_VUS, gracefulStop: '30s',
    };
  }
  // Mỗi bậc: tăng tốc độ trong 1 giây rồi giữ nguyên; giây đầu và SETTLE giây đầu bậc không được tính
  const stages = [{ target: RATE_START, duration: `${WARMUP}s` }];
  for (const s of STEPS) {
    stages.push({ target: s.rate, duration: '1s' }, { target: s.rate, duration: `${STEP - 1}s` });
  }
  return {
    executor: 'ramping-arrival-rate', startRate: RATE_START, timeUnit: '1s', stages,
    preAllocatedVUs: preAllocated(RATE_START * 2), maxVUs: MAX_VUS, gracefulStop: '30s',
  };
}

/**
 * Ngưỡng của k6. Phần lớn chỉ để k6 tính số liệu riêng (submetric) cho pha đo và từng thao tác, không bao giờ
 * trượt. Riêng chế độ capacity dừng sớm khi một bậc đã quá tải rõ ràng (p95 hoặc tỉ lệ lỗi vượt ngưỡng dừng).
 */
function thresholds(ops) {
  const t = {};
  const compute = ['p(95)>=0'];
  t['http_req_duration{phase:measure}'] = compute;
  t['http_req_failed{phase:measure}'] = ['rate>=0'];
  t['http_reqs{phase:measure}'] = ['count>=0'];
  if (MODE === 'capacity') {
    for (const s of STEPS) {
      const tags = `stage:${s.stage},phase:measure`;
      t[`http_req_duration{${tags}}`] = [{ threshold: `p(95)<${ABORT_P95_MS}`, abortOnFail: true, delayAbortEval: '10s' }];
      t[`http_req_failed{${tags}}`] = [{ threshold: `rate<${ABORT_ERROR_RATE}`, abortOnFail: true, delayAbortEval: '10s' }];
      t[`http_reqs{${tags}}`] = ['count>=0'];
    }
  } else {
    for (const op of ops) {
      t[`http_req_duration{phase:measure,op:${op}}`] = compute;
      t[`http_req_failed{phase:measure,op:${op}}`] = ['rate>=0'];
      t[`http_reqs{phase:measure,op:${op}}`] = ['count>=0'];
    }
  }
  return t;
}

export function buildOptions(ops) {
  return {
    scenarios: { main: scenario() },
    thresholds: thresholds(ops),
    summaryTrendStats: ['avg', 'min', 'med', 'max', 'p(90)', 'p(95)', 'p(99)', 'count'],
    discardResponseBodies: true,   // chỉ đọc body khi cần (responseType: 'text'), đỡ CPU cho máy tạo tải
    setupTimeout: '600s',
    userAgent: 'diagramio-bench/1',
  };
}

/** Pha của thời điểm hiện tại: warmup, measure, settle (đầu một bậc), tail (sau bậc cuối). */
export function phaseTags() {
  if (!startRecorded) {
    scenarioStart.add(exec.scenario.startTime);
    startRecorded = true;
    if (exec.vu.idInTest === 1) {
      // bench/kaggle/run_one.sh đọc dòng này để hẹn giờ lấy flame graph giữa pha đo
      console.log(`SCENARIO_START ${exec.scenario.startTime}`);
    }
  }
  const elapsed = (Date.now() - exec.scenario.startTime) / 1000;
  if (elapsed < WARMUP) {
    return { phase: 'warmup' };
  }
  if (MODE !== 'capacity') {
    return { phase: 'measure' };
  }
  const k = Math.floor((elapsed - WARMUP) / STEP);
  if (k >= STEPS.length) {
    return { phase: 'tail' };
  }
  const offset = elapsed - STEPS[k].start;
  return { phase: offset < 1 + SETTLE ? 'settle' : 'measure', stage: STEPS[k].stage };
}

function fmt(ms) {
  return ms === undefined ? '-' : `${ms.toFixed(1)} ms`;
}

function textSummary(data, ops) {
  const lines = [`\nKết quả (${MODE}, pha đo):`];
  const d = data.metrics['http_req_duration{phase:measure}'];
  const reqs = data.metrics['http_reqs{phase:measure}'];
  const failed = data.metrics['http_req_failed{phase:measure}'];
  if (d && reqs) {
    const throughput = MODE === 'capacity' ? '' : `, ${(reqs.values.count / MEASURE).toFixed(1)} req/s`;
    lines.push(`  ${reqs.values.count} request${throughput}, `
      + `lỗi ${(100 * (failed ? failed.values.rate : 0)).toFixed(2)}%`);
    lines.push(`  p50 ${fmt(d.values.med)}  p95 ${fmt(d.values['p(95)'])}  p99 ${fmt(d.values['p(99)'])}`);
  }
  if (MODE === 'capacity') {
    for (const s of STEPS) {
      const sd = data.metrics[`http_req_duration{stage:${s.stage},phase:measure}`];
      const sr = data.metrics[`http_reqs{stage:${s.stage},phase:measure}`];
      if (sd && sr && sr.values.count > 0) {
        const achieved = sr.values.count / (STEP - 1 - SETTLE);
        lines.push(`  ${s.stage} mục tiêu ${s.rate}/s, đạt ${achieved.toFixed(0)}/s, p95 ${fmt(sd.values['p(95)'])}`);
      }
    }
  } else {
    for (const op of ops) {
      const od = data.metrics[`http_req_duration{phase:measure,op:${op}}`];
      const oc = data.metrics[`http_reqs{phase:measure,op:${op}}`];
      if (od && oc && oc.values.count > 0) {
        lines.push(`  ${op.padEnd(14)} ${String(oc.values.count).padStart(8)}  p95 ${fmt(od.values['p(95)'])}`);
      }
    }
  }
  return lines.join('\n') + '\n';
}

export function summary(data, workload, ops) {
  const result = {
    workload,
    config: RUN_CONFIG,
    steps: STEPS,
    state: data.state,
    metrics: data.metrics,
  };
  return { [OUT]: JSON.stringify(result, null, 1), stdout: textSummary(data, ops) };
}
