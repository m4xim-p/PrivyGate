#!/usr/bin/env bash
# Capture a CPU profile of the gateway during a k6 load test using py-spy
# inside the gateway container, then analyze it by category.
#
# Usage:
#   scripts/k6/cpu_profile.sh --container privygate-bench [k6 options...]
#
# Requires: k6, a running gateway container with py-spy installed
#   (docker exec <container> pip install py-spy).
#
# Produces:
#   tmp/cpu_profile_raw.txt  - py-spy raw flamegraph dump
#   tmp/cpu_profile.svg      - flamegraph SVG (if flamegraph.pl available)
#   stdout                   - category breakdown (ProcessStore/Detectors/HTTP/Other)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

CONTAINER=""
PEAK_HTTP_RPS="${PEAK_HTTP_RPS:-1000}"
RAMP_SECONDS="${RAMP_SECONDS:-60}"
HOLD_SECONDS="${HOLD_SECONDS:-60}"
MAX_VUS="${MAX_VUS:-200}"

# Parse args.
while [[ $# -gt 0 ]]; do
  case "$1" in
    --container) CONTAINER="$2"; shift 2 ;;
    --peak) PEAK_HTTP_RPS="$2"; shift 2 ;;
    --ramp) RAMP_SECONDS="$2"; shift 2 ;;
    --hold) HOLD_SECONDS="$2"; shift 2 ;;
    --vus) MAX_VUS="$2"; shift 2 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

if [[ -z "$CONTAINER" ]]; then
  echo "Error: --container is required" >&2
  exit 1
fi

mkdir -p "$ROOT_DIR/tmp"
RAW="$ROOT_DIR/tmp/cpu_profile_raw.txt"
SVG="$ROOT_DIR/tmp/cpu_profile.svg"

# Total k6 duration (ramp + hold + graceful stop).
TOTAL_SECONDS=$((RAMP_SECONDS + HOLD_SECONDS + 30))

echo "==> CPU profile capture (py-spy in container $CONTAINER)"
echo "    Peak HTTP RPS: $PEAK_HTTP_RPS"
echo "    Ramp: ${RAMP_SECONDS}s, Hold: ${HOLD_SECONDS}s, Max VUs: $MAX_VUS"
echo "    Profile duration: ${TOTAL_SECONDS}s"
echo ""

# Start py-spy record inside the container in the background.
docker exec "$CONTAINER" py-spy record \
  --pid 1 \
  --duration "$TOTAL_SECONDS" \
  --format raw \
  -o /tmp/cpu_profile_raw.txt \
  > /dev/null 2>&1 &
PYSPY_PID=$!

# Run k6 load.
RUN_ID="$(date +%s)"
k6 run \
  --summary-trend-stats="avg,p(50),p(95),p(99)" \
  -e URL="${URL:-http://localhost:8000/process}" \
  -e RUN_ID="$RUN_ID" \
  -e TARGET_HTTP_RPS="$PEAK_HTTP_RPS" \
  -e RAMP_SECONDS="$RAMP_SECONDS" \
  -e HOLD_SECONDS="$HOLD_SECONDS" \
  -e MAX_VUS="$MAX_VUS" \
  "$SCRIPT_DIR/process_load.js"

# Wait for py-spy to finish.
wait "$PYSPY_PID" 2>/dev/null || true

# Ensure the raw dump exists in the container before copying.
for _ in $(seq 1 10); do
  if docker exec "$CONTAINER" test -s /tmp/cpu_profile_raw.txt 2>/dev/null; then
    break
  fi
  sleep 1
done

# Copy the raw dump out of the container.
docker cp "$CONTAINER:/tmp/cpu_profile_raw.txt" "$RAW"

echo ""
echo "==> CPU profile breakdown"
"$ROOT_DIR/.venv/bin/python" "$SCRIPT_DIR/analyze_profile.py" "$RAW"

# Optionally generate a flamegraph SVG if flamegraph.pl is available.
if command -v flamegraph.pl >/dev/null 2>&1; then
  flamegraph.pl "$RAW" > "$SVG"
  echo ""
  echo "Flamegraph written to $SVG"
fi