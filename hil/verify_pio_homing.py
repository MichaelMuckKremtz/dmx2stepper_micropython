#!/usr/bin/env python3
"""Capture a homing run and verify it against the OpenCV observer output."""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
HIL_DIR = ROOT / "hil"
FIRMWARE_DIR = ROOT / "firmware"
CAPTURE_DIR = HIL_DIR / "captures"
REMOTE_RESULT_FILE = "homing_result.json"


def timestamped_path(prefix: str, suffix: str) -> Path:
    stamp = time.strftime("%Y%m%d_%H%M%S")
    return CAPTURE_DIR / f"{prefix}_{stamp}{suffix}"


def run_command(cmd, cwd=None, check=True, capture_output=False):
    return subprocess.run(
        cmd,
        cwd=cwd,
        check=check,
        text=True,
        capture_output=capture_output,
    )


def deploy_firmware(device: str) -> None:
    py_files = sorted(str(path.name) for path in FIRMWARE_DIR.glob("*.py"))
    if not py_files:
        raise RuntimeError("No firmware files found to deploy")
    run_command(["mpremote", "connect", device, "fs", "cp", *py_files, ":"], cwd=str(FIRMWARE_DIR))


def clear_remote_result(device: str) -> None:
    run_command(
        ["mpremote", "connect", device, "fs", "rm", REMOTE_RESULT_FILE],
        check=False,
        capture_output=True,
    )


def hard_reset(device: str) -> None:
    run_command(["mpremote", "connect", device, "reset"])


def build_exec_code(diag_pin: int | None, run_runtime_after_homing: bool, runtime_exit_after_ms: int) -> str:
    parts = [
        "import sys",
        "sys.modules.pop('main', None)",
        "sys.modules.pop('config', None)",
        "import config",
        f"config.RUN_RUNTIME_AFTER_HOMING={bool(run_runtime_after_homing)}",
        f"config.RUNTIME_EXIT_AFTER_MS={int(runtime_exit_after_ms)}",
    ]
    if diag_pin is not None:
        parts.append(f"config.DIAG_PIN={int(diag_pin)}")
    parts.extend(
        [
            "import main",
            "main.main()",
        ]
    )
    return "; ".join(parts)


def launch_firmware(device: str, diag_pin: int | None = None, run_runtime_after_homing: bool = False, runtime_exit_after_ms: int = 0):
    code = build_exec_code(diag_pin, run_runtime_after_homing, runtime_exit_after_ms)
    return subprocess.Popen(
        ["mpremote", "connect", device, "exec", code],
        cwd=str(FIRMWARE_DIR),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


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


def unwrap_series(values):
    if not values:
        return []

    unwrapped = [values[0]]
    for value in values[1:]:
        candidates = [value - 360.0, value, value + 360.0]
        chosen = min(candidates, key=lambda item: abs(item - unwrapped[-1]))
        unwrapped.append(chosen)
    return unwrapped


def evaluate_homing_trace(
    vision_rows,
    settle_s: float,
    min_travel_deg: float,
    max_end_span_deg: float,
    max_center_error_deg: float,
):
    if len(vision_rows) < 8:
        return False, "not enough visible vision samples", {
            "start_angle_deg": None,
            "end_angle_deg": None,
            "delta_deg": None,
            "travel_span_deg": None,
            "end_span_deg": None,
            "midpoint_angle_deg": None,
            "center_error_deg": None,
            "visible_samples": len(vision_rows),
        }

    times = [row[0] for row in vision_rows]
    values = unwrap_series([row[1] for row in vision_rows])
    head_count = min(5, len(values))
    start_angle = sum(values[:head_count]) / head_count

    end_cutoff = times[-1] - settle_s
    settled_values = [value for ts, value in zip(times, values) if ts >= end_cutoff]
    if len(settled_values) < 3:
        settled_values = values[-3:]

    end_angle = sum(settled_values) / len(settled_values)
    delta = end_angle - start_angle
    travel_span = max(values) - min(values)
    end_span = max(settled_values) - min(settled_values)
    midpoint_angle = (max(values) + min(values)) / 2.0
    center_error = abs(end_angle - midpoint_angle)

    details = {
        "start_angle_deg": round(start_angle, 3),
        "end_angle_deg": round(end_angle, 3),
        "delta_deg": round(delta, 3),
        "travel_span_deg": round(travel_span, 3),
        "end_span_deg": round(end_span, 3),
        "midpoint_angle_deg": round(midpoint_angle, 3),
        "center_error_deg": round(center_error, 3),
        "visible_samples": len(values),
    }

    if travel_span < min_travel_deg:
        return False, "observed homing excursion too small", details
    if end_span > max_end_span_deg:
        return False, "final homing position did not settle cleanly", details
    if center_error > max_center_error_deg:
        return False, "final position did not settle near the midpoint", details
    return True, "vision trace shows a large move followed by a stable centered stop", details


def read_remote_result(device: str, timeout_s: float):
    deadline = time.monotonic() + timeout_s
    last_error = None
    while time.monotonic() < deadline:
        proc = run_command(
            ["mpremote", "connect", device, "fs", "cat", f":{REMOTE_RESULT_FILE}"],
            check=False,
            capture_output=True,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            try:
                payload = json.loads(proc.stdout)
            except json.JSONDecodeError as exc:
                last_error = f"invalid JSON in remote result: {exc}"
            else:
                if payload.get("status") == "done":
                    return payload
                last_error = "remote result still marked running"
        else:
            last_error = proc.stderr.strip() or "remote result not available yet"
        time.sleep(0.5)
    raise RuntimeError(last_error or "timed out waiting for remote homing result")


def build_argument_parser():
    parser = argparse.ArgumentParser(description="Verify the PIO homing milestone with the OpenCV observer")
    parser.add_argument("--device", default="/dev/ttyACM0", help="MicroPython device path for mpremote")
    parser.add_argument("--upload", action="store_true", help="Upload firmware before running verification")
    parser.add_argument("--axis", default="T1", choices=("T1", "T2"), help="Observed axis to evaluate")
    parser.add_argument("--diag-pin", type=int, default=None, help="Optional DIAG GPIO override for candidate scanning")
    parser.add_argument("--vision-duration-s", type=float, default=12.0, help="Vision capture duration")
    parser.add_argument("--warmup-s", type=float, default=2.0, help="Camera warmup before Pico reset")
    parser.add_argument("--result-timeout-s", type=float, default=16.0, help="Timeout for reading the firmware result file")
    parser.add_argument("--settle-s", type=float, default=1.0, help="Tail window used to confirm the final stop")
    parser.add_argument("--min-travel-deg", type=float, default=270.0, help="Minimum observed total travel span")
    parser.add_argument("--max-end-span-deg", type=float, default=4.0, help="Maximum spread allowed in the final settle window")
    parser.add_argument(
        "--max-center-error-deg",
        type=float,
        default=8.0,
        help="Maximum allowed distance between the final settled angle and the observed midpoint",
    )
    return parser


def main() -> int:
    parser = build_argument_parser()
    args = parser.parse_args()

    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    vision_output = timestamped_path("vision_homing", ".csv")
    result_output = timestamped_path("homing_result", ".json")

    if args.upload:
        deploy_firmware(args.device)

    clear_remote_result(args.device)

    vision_cmd = [
        sys.executable,
        str(HIL_DIR / "vision_observer.py"),
        "--output",
        str(vision_output),
        "--duration-s",
        str(args.vision_duration_s),
        "--prefix",
        "vision_homing",
    ]

    vision_proc = subprocess.Popen(vision_cmd, cwd=str(HIL_DIR))
    firmware_proc = None
    firmware_output = ""
    try:
        time.sleep(args.warmup_s)
        firmware_proc = launch_firmware(
            args.device,
            diag_pin=args.diag_pin,
            run_runtime_after_homing=False,
            runtime_exit_after_ms=0,
        )
        vision_proc.wait(timeout=max(1.0, args.vision_duration_s + 2.0))
        if firmware_proc is not None:
            firmware_output, _ = firmware_proc.communicate(timeout=max(1.0, args.result_timeout_s))
    finally:
        if vision_proc.poll() is None:
            vision_proc.terminate()
            try:
                vision_proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                vision_proc.kill()
        if firmware_proc is not None and firmware_proc.poll() is None:
            firmware_proc.terminate()
            try:
                firmware_output, _ = firmware_proc.communicate(timeout=2.0)
            except subprocess.TimeoutExpired:
                firmware_proc.kill()
                firmware_output, _ = firmware_proc.communicate()

    remote_result = read_remote_result(args.device, args.result_timeout_s)
    result_output.write_text(json.dumps(remote_result, indent=2))

    vision_rows = load_vision_rows(vision_output, args.axis)
    vision_ok, vision_message, vision_details = evaluate_homing_trace(
        vision_rows,
        settle_s=args.settle_s,
        min_travel_deg=args.min_travel_deg,
        max_end_span_deg=args.max_end_span_deg,
        max_center_error_deg=args.max_center_error_deg,
    )

    selected_trial = None
    if remote_result.get("selected_trial") is not None:
        try:
            selected_trial = remote_result["trials"][int(remote_result["selected_trial"])]
        except (IndexError, KeyError, TypeError, ValueError):
            selected_trial = None

    firmware_ok = (
        bool(remote_result.get("success"))
        and bool(remote_result.get("centered"))
        and remote_result.get("selected_trial") is not None
    )

    print(f"[INFO] Vision log: {vision_output}")
    print(f"[INFO] Firmware result: {result_output}")
    if firmware_output.strip():
        print("[INFO] Firmware console captured")
    print(
        "[INFO] Firmware success: {} centered={} selected_trial={} stop_reason={}".format(
            firmware_ok,
            remote_result.get("centered"),
            remote_result.get("selected_trial"),
            remote_result.get("stop_reason"),
        )
    )
    print(
        "[INFO] Vision summary: delta={delta_deg} deg travel_span={travel_span_deg} deg end_span={end_span_deg} deg midpoint={midpoint_angle_deg} deg center_error={center_error_deg} deg visible_samples={visible_samples}".format(
            **vision_details
        )
    )

    if firmware_ok and vision_ok:
        print(f"[PASS] {vision_message}")
        return 0

    if not firmware_ok:
        if firmware_output.strip():
            print(firmware_output.strip())
        print(f"[FAIL] Firmware reported failure: {remote_result.get('stop_reason')}")
        return 1

    if firmware_output.strip():
        print(firmware_output.strip())
    print(f"[FAIL] {vision_message}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
