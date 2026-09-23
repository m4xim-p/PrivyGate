#!/usr/bin/env bash
# Run the k6 /process load test together with the real-time server metrics
# collector. Reproduces the AlfaSonar evaluation profile.
#
# Usage:
#   scripts/k6/run_benchmark.sh [--duration 120] [--ramp 60] [--peak 1000]
#
# Requires: k6, python3 with psutil, a running gateway on :8000.
# The gateway must expose /metrics (see app/metrics.py).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

URL="${URL:-http://localhost:8000/process}"
METRICS_URL="${METRICS_URL:-http://localhost:8000/metrics}"
PEAK_HTTP_RPS="${PEAK_HTTP_RPS:-1000}"
RAMP_SECONDS="${RAMP_SECONDS:-60}"
HOLD_SECONDS="${HOLD_SECONDS:-60}"
MAX_VUS="${MAX_VUS:-200}"
GATEWAY_PID="${GATEWAY_PID:-}"
GATEWAY_CONTAINER="${GATEWAY_CONTAINER:-}"

# Parse --duration/--ramp/--peak overrides.
DURATION=120
while [[ $# -gt 0 ]]; do
  case "$1" in
    --duration) DURATION="$2"; shift 2 ;;
    --ramp) RAMP_SECONDS="$2"; shift 2 ;;
    --hold) HOLD_SECONDS="$2"; shift 2 ;;
    --peak) PEAK_HTTP_RPS="$2"; shift 2 ;;
    --vus) MAX_VUS="$2"; shift 2 ;;
    --container) GATEWAY_CONTAINER="$2"; shift 2 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

echo "==> PrivyGate /process benchmark (k6 + metrics collector)"
echo "    URL:        $URL"
echo "    Peak HTTP RPS: $PEAK_HTTP_RPS"
echo "    Ramp:       ${RAMP_SECONDS}s"
echo "    Hold:       ${HOLD_SECONDS}s"
echo "    Max VUs:    $MAX_VUS"
echo "    Duration:   ${DURATION}s (collector)"
echo ""

# Start the metrics collector in the background.
COLLECTOR_LOG="$ROOT_DIR/tmp/metrics_collector.log"
mkdir -p "$ROOT_DIR/tmp"
COLLECTOR_ARGS=(--metrics-url "$METRICS_URL" --duration "$DURATION")
if [[ -n "$GATEWAY_CONTAINER" ]]; then
  COLLECTOR_ARGS+=(--container "$GATEWAY_CONTAINER")
elif [[ -n "$GATEWAY_PID" ]]; then
  COLLECTOR_ARGS+=(--pid "$GATEWAY_PID")
fi
"$ROOT_DIR/.venv/bin/python" "$SCRIPT_DIR/metrics_collector.py" "${COLLECTOR_ARGS[@]}" \
  > "$COLLECTOR_LOG" 2>&1 &
COLLECTOR_PID=$!
trap 'kill "$COLLECTOR_PID" 2>/dev/null || true' EXIT

# Run k6.
RUN_ID="$(date +%s)"
k6 run \
  --summary-trend-stats="avg,p(50),p(95),p(99)" \
  -e URL="$URL" \
  -e RUN_ID="$RUN_ID" \
  -e TARGET_HTTP_RPS="$PEAK_HTTP_RPS" \
  -e RAMP_SECONDS="$RAMP_SECONDS" \
  -e HOLD_SECONDS="$HOLD_SECONDS" \
  -e MAX_VUS="$MAX_VUS" \
  "$SCRIPT_DIR/process_load.js"

echo ""
echo "==> Server metrics (real-time, from $METRICS_URL)"
cat "$COLLECTOR_LOG"