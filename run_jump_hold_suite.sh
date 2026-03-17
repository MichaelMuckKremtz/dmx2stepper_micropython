#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HIL_DIR="$ROOT_DIR/hil"
AXIS="${AXIS:-T1}"
WITH_VISION="${WITH_VISION:-1}"
FPS="${FPS:-44}"
SCENARIO="${SCENARIO:-$HIL_DIR/scenarios/jump_hold_positions.csv}"

scenario_duration() {
  python3 - "$1" <<'PY'
import csv
import sys
from pathlib import Path

path = Path(sys.argv[1])
finish = 0.0
with path.open(newline="") as handle:
    reader = csv.DictReader(line for line in handle if not line.lstrip().startswith("#"))
    for row in reader:
        finish = max(finish, float(row["offset_s"]) + float(row.get("fade_s", 0.0) or 0.0))
print(finish)
PY
}

DURATION="$(scenario_duration "$SCENARIO")"

echo "[INFO] Running jump-hold DMX suite against the already running Pico"
echo "[INFO] Scenario: $SCENARIO"
echo "[INFO] Duration: ${DURATION}s"
echo "[INFO] Vision enabled: $WITH_VISION"
echo "[INFO] DMX FPS: $FPS"

cd "$ROOT_DIR"

VISION_PID=""
if [[ "$WITH_VISION" == "1" ]]; then
  echo "[INFO] Starting raw vision observer on axis $AXIS"
  python3 "$HIL_DIR/vision_observer.py" \
    --duration-s "$DURATION" \
    --prefix "vision_jump_hold" \
    --filter-window 1 \
    --deadband-deg 0 &
  VISION_PID=$!
  sleep 0.5
fi

python3 "$HIL_DIR/dmx_stimulus.py" \
  --fps "$FPS" \
  --prefix "jump_hold_dmx" \
  scenario \
  --path "$SCENARIO"

if [[ -n "$VISION_PID" ]]; then
  wait "$VISION_PID"
fi

echo "[PASS] Jump-hold DMX suite completed"
