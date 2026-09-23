// k6 load test for the /process contract (AlfaSonar evaluation profile).
//
// Reproduces the profile from docs/evaluation-contract.md and
// docs/source/organizer-clarifications.txt:
//   - ramp-up to 1000 HTTP RPS with hold;
//   - up to 200 concurrent connections, each waits for its response (closed-loop);
//   - masking and demasking roughly equal (a pair per iteration);
//   - target latency <= 1s, reported as mean/p50/p95/p99;
//   - hard timeout 10s;
//   - up to two retries after the first attempt;
//   - 429 is not an error (waits on Retry-After) but is recorded.
//
// NOTE: "1000 RPS" in the contract means 1000 individual HTTP requests per
// second. Each iteration performs a masking + demasking pair (2 HTTP requests),
// so the arrival-rate target is TARGET_HTTP_RPS / 2 iterations per second.
//
// Run:
//   k6 run --summary-trend-stats="avg,p(50),p(95),p(99)" scripts/k6/process_load.js
//
// Options (via -e KEY=value):
//   URL             /process endpoint, default http://localhost:8000/process
//   TARGET_HTTP_RPS peak HTTP RPS after ramp, default 1000
//   RAMP_SECONDS    ramp-up duration, default 60
//   HOLD_SECONDS    hold duration at peak, default 60
//   MAX_VUS         max concurrent connections, default 200
//   MAX_RETRIES     retries after first attempt, default 2
//   TIMEOUT         per-request timeout, default 10

import http from "k6/http";
import { check, sleep } from "k6";
import { Counter, Rate, Trend } from "k6/metrics";

const URL = __ENV.URL || "http://localhost:8000/process";
const TARGET_HTTP_RPS = Number(__ENV.TARGET_HTTP_RPS || 1000);
const RAMP_SECONDS = Number(__ENV.RAMP_SECONDS || 60);
const HOLD_SECONDS = Number(__ENV.HOLD_SECONDS || 60);
const MAX_VUS = Number(__ENV.MAX_VUS || 200);
const MAX_RETRIES = Number(__ENV.MAX_RETRIES || 2);
const TIMEOUT = Number(__ENV.TIMEOUT || 10);

// Each iteration = 1 masking + 1 demasking = 2 HTTP requests.
const TARGET_ITERATIONS = TARGET_HTTP_RPS / 2;

// Per-operation latency trends (mask vs demask).
const maskLatency = new Trend("mask_latency", true);
const demaskLatency = new Trend("demask_latency", true);
const allLatency = new Trend("all_latency", true);

// Counters.
const maskCount = new Counter("mask_count");
const demaskCount = new Counter("demask_count");
const retryCount = new Counter("retry_count");
const newIdCount = new Counter("new_id_count");
const rateLimitedCount = new Counter("rate_limited_count");
const conflictCount = new Counter("conflict_count");
const errorCount = new Counter("error_count");

// Rates.
const maskSuccessRate = new Rate("mask_success_rate");
const demaskSuccessRate = new Rate("demask_success_rate");

export const options = {
  scenarios: {
    load: {
      // RPS-based ramp-up to TARGET_ITERATIONS (iterations/s) with hold,
      // bounded by MAX_VUS. Each VU waits for its response (closed-loop),
      // matching the checker. Dropped iterations are reported when the target
      // rate exceeds what MAX_VUS can sustain.
      executor: "ramping-arrival-rate",
      exec: "processPair",
      startRate: 0,
      timeUnit: "1s",
      preAllocatedVUs: MAX_VUS,
      maxVUs: MAX_VUS,
      stages: [
        { target: TARGET_ITERATIONS, duration: `${RAMP_SECONDS}s` },
        { target: TARGET_ITERATIONS, duration: `${HOLD_SECONDS}s` },
      ],
    },
  },
  thresholds: {
    // 429 is not an error; only 5xx/network errors count as failures.
    http_req_failed: ["rate<0.01"],
    // Report dropped iterations (target rate exceeded MAX_VUS capacity).
    dropped_iterations: ["count==0"],
  },
};

function syntheticPayload(requestNumber) {
  const phoneSuffix = requestNumber % 100;
  return (
    `Иванов Иван Иванович, паспорт 45 10 ${String(requestNumber % 1000000).padStart(6, "0")}, ` +
    `телефон +7 999 000-00-${String(phoneSuffix).padStart(2, "0")}, ` +
    `email loadtest-${requestNumber}@example.com`
  );
}

function sendWithRetries(payload, payloadId, op) {
  let attempt = 0;
  while (true) {
    const resp = http.post(URL, JSON.stringify({ payload, payload_id: payloadId }), {
      headers: { "Content-Type": "application/json" },
      timeout: `${TIMEOUT}s`,
      tags: { op },
    });
    const latency = resp.timings.duration;

    if (resp.status === 429) {
      rateLimitedCount.add(1);
      const retryAfter = Number(resp.headers["Retry-After"] || 1);
      if (attempt >= MAX_RETRIES) {
        return { resp, latency, attempt };
      }
      attempt += 1;
      retryCount.add(1);
      sleep(retryAfter);
      continue;
    }

    if (resp.status >= 500 && attempt < MAX_RETRIES) {
      attempt += 1;
      retryCount.add(1);
      sleep(0.1);
      continue;
    }

    return { resp, latency, attempt };
  }
}

export function processPair() {
  // Unique payload_id per pair (masking + demasking share the same id).
  // A run-unique prefix avoids collisions with IDs from previous runs.
  const runId = __ENV.RUN_ID || `${Date.now()}`;
  const requestNumber = __ITER + 1;
  const payloadId = `k6-${runId}-${__VU}-${__ITER}`;
  const payload = syntheticPayload(requestNumber);

  // Masking.
  const mask = sendWithRetries(payload, payloadId, "mask");
  maskLatency.add(mask.latency, { op: "mask" });
  allLatency.add(mask.latency, { op: "mask" });
  maskCount.add(1);
  newIdCount.add(1);

  if (mask.resp.status === 429) {
    // Rate-limited masking: no demasking for this pair.
    maskSuccessRate.add(false);
    return;
  }

  if (mask.resp.status !== 200) {
    maskSuccessRate.add(false);
    errorCount.add(1);
    return;
  }

  const masked = mask.resp.json().result;
  maskSuccessRate.add(true);

  // Demasking (same payload_id, masked text).
  const demask = sendWithRetries(masked, payloadId, "demask");
  demaskLatency.add(demask.latency, { op: "demask" });
  allLatency.add(demask.latency, { op: "demask" });
  demaskCount.add(1);

  if (demask.resp.status === 429) {
    demaskSuccessRate.add(false);
    return;
  }

  if (demask.resp.status !== 200) {
    demaskSuccessRate.add(false);
    errorCount.add(1);
    return;
  }

  const original = demask.resp.json().result;
  const ok = original === payload;
  demaskSuccessRate.add(ok);
  if (!ok) {
    errorCount.add(1);
  }
  check(demask.resp, {
    "demask round-trip matches": () => ok,
  });
}