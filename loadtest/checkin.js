// k6 load test for the student check-in endpoint (POST /checkin/scan).
// All N virtual users start at the same moment and each scans once (a burst).
// The rotating QR token is computed here from the session secret, exactly as the
// server does: HMAC-SHA256(secret, "<session_id>:<25-second step>")[:16].
import http from 'k6/http';
import crypto from 'k6/crypto';
import { SharedArray } from 'k6/data';
import { Trend, Counter } from 'k6/metrics';

const data = new SharedArray('load', function () {
  return [JSON.parse(open('/loadtest/data.json'))];
});
const d = data[0];
const BASE = __ENV.BASE_URL || 'http://localhost:8000';
const STEP_MS = 25000;

const latency = new Trend('scan_latency', true);
const ok = new Counter('scan_ok');
const dup = new Counter('scan_dup');
const bad = new Counter('scan_err');

export const options = {
  scenarios: {
    burst: { executor: 'per-vu-iterations', vus: d.tokens.length, iterations: 1, maxDuration: '3m' },
  },
  thresholds: { scan_latency: ['p(95)<1000'], scan_err: ['count==0'] },
  summaryTrendStats: ['avg', 'min', 'med', 'max', 'p(90)', 'p(95)', 'p(99)'],
};

export default function () {
  const jwt = d.tokens[(__VU - 1) % d.tokens.length];
  const step = Math.floor(Date.now() / STEP_MS);
  const token = crypto.hmac('sha256', d.secret, d.session_id + ':' + step, 'hex').substring(0, 16);
  const res = http.post(
    BASE + '/checkin/scan',
    JSON.stringify({ session_id: d.session_id, token: token }),
    {
      headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + jwt },
      tags: { endpoint: 'scan' },
      timeout: '30s',
    }
  );
  latency.add(res.timings.duration);
  if (res.status === 201) { ok.add(1); }
  else if (res.status === 409) { dup.add(1); }
  else { bad.add(1); }
}

function r1(x) { return Math.round(x * 10) / 10; }

export function handleSummary(summary) {
  const m = summary.metrics;
  const val = function (name, key) {
    return (m[name] && m[name].values && m[name].values[key] !== undefined) ? m[name].values[key] : 0;
  };
  const created = val('scan_ok', 'count');
  const dups = val('scan_dup', 'count');
  const errs = val('scan_err', 'count');
  const out = {
    level: d.tokens.length,
    requests: created + dups + errs,
    created_201: created,
    already_409: dups,
    other_errors: errs,
    avg_ms: r1(val('scan_latency', 'avg')),
    med_ms: r1(val('scan_latency', 'med')),
    p90_ms: r1(val('scan_latency', 'p(90)')),
    p95_ms: r1(val('scan_latency', 'p(95)')),
    p99_ms: r1(val('scan_latency', 'p(99)')),
    max_ms: r1(val('scan_latency', 'max')),
    burst_seconds: r1(((summary.state && summary.state.testRunDurationMs) || 0) / 1000),
  };
  const text = 'students=' + out.level + ' created=' + created + ' dup=' + dups + ' errors=' + errs +
    ' p95=' + out.p95_ms + 'ms\n';
  return { stdout: text + 'RESULT_JSON:' + JSON.stringify(out) + '\n' };
}
