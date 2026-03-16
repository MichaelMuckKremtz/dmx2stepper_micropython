#!/usr/bin/env python3
"""Thin hardware-in-the-loop coordinator for firmware upload, DMX stimulus, and vision capture."""

from __future__ import annotations

import argparse
import csv
import math
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
HIL_DIR = ROOT / "hil"
FIRMWARE_DIR = ROOT / "firmware"
CAPTURE_DIR = HIL_DIR / "captures"
DEFAULT_SCENARIO = HIL_DIR / "scenarios" / "example_large_move.csv"


def timestamped_path(prefix: str) -> Path:
    stamp = time.strftime("%Y%m%d_%H%M%S")
    return CAPTURE_DIR / f"{prefix}_{stamp}.csv"


def run_command(cmd, cwd=None):
    return subprocess.run(cmd, cwd=cwd, check=True)


def deploy_firmware(device: str) -> None:
    py_files = sorted(str(path.name) for path in FIRMWARE_DIR.glob("*.py"))
    if not py_files:
        raise RuntimeError("No firmware files found to deploy")
    run_command(["mpremote", "connect", device, "fs", "cp", *py_files, ":"], cwd=str(FIRMWARE_DIR))


def load_vision_rows(path: Path, axis: str):
    rows = []
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            if row["axis"] != axis or row["visible"] != "1":
                continue
            filtered = row["filtered_angle_deg"]
            if not filtered:
                continue
            rows.append((float(row["t_monotonic"]), float(filtered)))
    return rows


def load_position_direction(path: Path, msb_channel: int = 1, lsb_channel: int = 2):
    current = {msb_channel: 0, lsb_channel: 0}
    first_value = None
    last_value = None

    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            channel = int(row["channel"])
            value = int(row["value"])
            if channel not in current:
                continue
            current[channel] = value
            combined = (current[msb_channel] << 8) | current[lsb_channel]
            if first_value is None:
                first_value = combined
            last_value = combined

    if first_value is None or last_value is None:
        raise RuntimeError("Stimulus log did not include both position channels")

    if last_value > first_value:
        return 1
    if last_value < first_value:
        return -1
    return 0


def unwrap_series(values):
    if not values:
        return []

    unwrapped = [values[0]]
    for value in values[1:]:
        candidates = [value - 360.0, value, value + 360.0]
        chosen = min(candidates, key=lambda item: abs(item - unwrapped[-1]))
        unwrapped.append(chosen)
    return unwrapped


def evaluate_large_move(vision_rows, expected_direction: int, settle_s: float, min_change_deg: float):
    if len(vision_rows) < 5:
        return False, "not enough visible vision samples"

    times = [row[0] for row in vision_rows]
    values = unwrap_series([row[1] for row in vision_rows])
    start_angle = values[0]
    end_cutoff = times[-1] - settle_s
    settled_values = [value for ts, value in zip(times, values) if ts >= end_cutoff]
    end_angle = sum(settled_values) / len(settled_values) if settled_values else values[-1]
    delta = end_angle - start_angle

    if abs(delta) < min_change_deg:
        return False, f"observed change too small ({delta:.2f} deg)"
    if expected_direction and math.copysign(1, delta) != expected_direction:
        return False, f"unexpected direction ({delta:.2f} deg)"
    return True, f"observed change {delta:.2f} deg"


def build_argument_parser():
    parser = argparse.ArgumentParser(description="Run a coarse DMX + vision HIL scenario")
    parser.add_argument("--device", default="/dev/ttyACM0", help="MicroPython device path for mpremote")
    parser.add_argument("--upload", action="store_true", help="Upload firmware before running the scenario")
    parser.add_argument("--axis", default="T1", choices=("T1", "T2"), help="Observed axis to evaluate")
    parser.add_argument("--scenario", default=str(DEFAULT_SCENARIO), help="Scenario CSV for dmx_stimulus.py")
    parser.add_argument("--vision-duration-s", type=float, default=12.0, help="Vision capture duration")
    parser.add_argument("--settle-s", type=float, default=0.5, help="Settle window before endpoint evaluation")
    parser.add_argument("--min-change-deg", type=float, default=10.0, help="Minimum observed angle change")
    parser.add_argument("--warmup-s", type=float, default=2.0, help="Camera warmup before DMX stimulus")
    return parser


def main() -> int:
    parser = build_argument_parser()
    args = parser.parse_args()

    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    vision_output = timestamped_path("vision")
    stimulus_output = timestamped_path("dmx")

    if args.upload:
        deploy_firmware(args.device)

    vision_cmd = [
        sys.executable,
        str(HIL_DIR / "vision_observer.py"),
        "--output",
        str(vision_output),
        "--duration-s",
        str(args.vision_duration_s),
    ]
    stimulus_cmd = [
        sys.executable,
        str(HIL_DIR / "dmx_stimulus.py"),
        "--output",
        str(stimulus_output),
        "scenario",
        "--path",
        str(Path(args.scenario)),
    ]

    vision_proc = subprocess.Popen(vision_cmd, cwd=str(HIL_DIR))
    try:
        time.sleep(args.warmup_s)
        stimulus_proc = subprocess.run(stimulus_cmd, cwd=str(HIL_DIR), check=False)
        if stimulus_proc.returncode != 0:
            return stimulus_proc.returncode
        vision_proc.wait(timeout=max(1.0, args.vision_duration_s + 2.0))
    finally:
        if vision_proc.poll() is None:
            vision_proc.terminate()
            try:
                vision_proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                vision_proc.kill()

    expected_direction = load_position_direction(stimulus_output)
    vision_rows = load_vision_rows(vision_output, args.axis)
    passed, message = evaluate_large_move(vision_rows, expected_direction, args.settle_s, args.min_change_deg)

    print(f"[INFO] Vision log: {vision_output}")
    print(f"[INFO] Stimulus log: {stimulus_output}")
    if passed:
        print(f"[PASS] {message}")
        return 0

    print(f"[FAIL] {message}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
