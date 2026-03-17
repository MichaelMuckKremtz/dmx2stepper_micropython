#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HIL_DIR="$ROOT_DIR/hil"
AXIS="${AXIS:-T1}"
WITH_VISION="${WITH_VISION:-1}"
FPS="${FPS:-44}"

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

run_scenario() {
  local label="$1"
  local scenario_path="$2"
  local duration
  duration="$(scenario_duration "$scenario_path")"

  echo "[INFO] Running scenario: $label"
  echo "[INFO] Scenario file: $scenario_path"
  echo "[INFO] Duration: ${duration}s"

  local vision_pid=""
  if [[ "$WITH_VISION" == "1" ]]; then
    echo "[INFO] Starting raw vision observer on axis $AXIS"
    python3 "$HIL_DIR/vision_observer.py" \
      --duration-s "$duration" \
      --prefix "vision_${label}" \
      --filter-window 1 \
      --deadband-deg 0 &
    vision_pid=$!
    sleep 0.5
  fi

  python3 "$HIL_DIR/dmx_stimulus.py" \
    --fps "$FPS" \
    --prefix "${label}_dmx" \
    scenario \
    --path "$scenario_path"

  if [[ -n "$vision_pid" ]]; then
    wait "$vision_pid"
  fi
}

echo "[INFO] Running idle DMX suite against the already running Pico"
echo "[INFO] Vision enabled: $WITH_VISION"
echo "[INFO] DMX FPS: $FPS"

cd "$ROOT_DIR"
run_scenario "disabled_idle" "$HIL_DIR/scenarios/disabled_idle_hold.csv"
run_scenario "fixed_target" "$HIL_DIR/scenarios/fixed_target_hold.csv"

echo "[PASS] Idle DMX suite completed"
