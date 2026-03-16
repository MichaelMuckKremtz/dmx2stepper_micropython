#!/usr/bin/env python3
"""Verify one-axis DMX runtime after startup homing."""

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
DEFAULT_SCENARIO = HIL_DIR / "scenarios" / "one_axis_runtime.csv"
REMOTE_HOMING_FILE = "homing_result.json"
REMOTE_STATUS_FILE = "controller_status.json"


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


def clear_remote_file(device: str, remote_path: str) -> None:
    run_command(
        ["mpremote", "connect", device, "fs", "rm", remote_path],
        check=False,
        capture_output=True,
    )


def hard_reset(device: str) -> None:
    run_command(["mpremote", "connect", device, "reset"])


def build_exec_code(run_runtime_after_homing: bool, runtime_exit_after_ms: int) -> str:
    return "; ".join(
        [
            "import sys",
            "sys.modules.pop('main', None)",
            "sys.modules.pop('config', None)",
            "import config",
            f"config.RUN_RUNTIME_AFTER_HOMING={bool(run_runtime_after_homing)}",
            f"config.RUNTIME_EXIT_AFTER_MS={int(runtime_exit_after_ms)}",
            "import main",
            "main.main()",
        ]
    )


def launch_firmware(device: str, run_runtime_after_homing: bool, runtime_exit_after_ms: int):
    return subprocess.Popen(
        ["mpremote", "connect", device, "exec", build_exec_code(run_runtime_after_homing, runtime_exit_after_ms)],
        cwd=str(FIRMWARE_DIR),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def read_remote_json(device: str, remote_path: str, timeout_s: float, require_done: bool = False):
    deadline = time.monotonic() + timeout_s
    last_error = None
    while time.monotonic() < deadline:
        proc = run_command(
            ["mpremote", "connect", device, "fs", "cat", f":{remote_path}"],
            check=False,
            capture_output=True,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            try:
                payload = json.loads(proc.stdout)
            except json.JSONDecodeError as exc:
                last_error = f"invalid JSON in {remote_path}: {exc}"
            else:
                if not require_done or payload.get("status") == "done":
                    return payload
                last_error = f"{remote_path} still marked running"
        else:
            last_error = proc.stderr.strip() or f"{remote_path} not available yet"
        time.sleep(0.5)
    raise RuntimeError(last_error or f"timed out waiting for {remote_path}")


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


def evaluate_large_move(vision_rows, settle_s: float, min_change_deg: float):
    if len(vision_rows) < 5:
        return False, "not enough visible vision samples", {
            "delta_deg": None,
            "travel_span_deg": None,
            "end_span_deg": None,
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

    details = {
        "delta_deg": round(delta, 3),
        "travel_span_deg": round(travel_span, 3),
        "end_span_deg": round(end_span, 3),
        "visible_samples": len(values),
    }

    if abs(delta) < min_change_deg:
        return False, "observed change too small", details
    return True, "vision trace shows a clear DMX-driven move", details


def build_argument_parser():
    parser = argparse.ArgumentParser(description="Verify one-axis DMX runtime after startup homing")
    parser.add_argument("--device", default="/dev/ttyACM0", help="MicroPython device path for mpremote")
    parser.add_argument("--upload", action="store_true", help="Upload firmware before running verification")
    parser.add_argument("--axis", default="T1", choices=("T1", "T2"), help="Observed axis to evaluate")
    parser.add_argument("--scenario", default=str(DEFAULT_SCENARIO), help="Scenario CSV for dmx_stimulus.py")
    parser.add_argument("--dmx-fps", type=float, default=44.0, help="DMX send rate during the scenario")
    parser.add_argument("--vision-duration-s", type=float, default=8.0, help="Vision capture duration for the DMX move")
    parser.add_argument("--vision-warmup-s", type=float, default=0.5, help="Camera warmup before stimulus starts")
    parser.add_argument("--startup-wait-s", type=float, default=9.0, help="Time reserved for startup homing before stimulus starts")
    parser.add_argument("--settle-s", type=float, default=0.5, help="Tail window for final motion evaluation")
    parser.add_argument("--min-change-deg", type=float, default=20.0, help="Minimum visible change required")
    parser.add_argument("--min-frame-count", type=int, default=100, help="Minimum DMX frames the Pico must report")
    parser.add_argument("--skip-vision", action="store_true", help="Skip the OpenCV observer and verify runtime headlessly")
    return parser


def main() -> int:
    parser = build_argument_parser()
    args = parser.parse_args()

    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    stimulus_output = timestamped_path("dmx_runtime", ".csv")
    homing_output = timestamped_path("runtime_homing_result", ".json")
    status_output = timestamped_path("runtime_status", ".json")
    vision_output = None
    if not args.skip_vision:
        vision_output = timestamped_path("vision_runtime", ".csv")

    if args.upload:
        deploy_firmware(args.device)

    clear_remote_file(args.device, REMOTE_HOMING_FILE)
    clear_remote_file(args.device, REMOTE_STATUS_FILE)
    runtime_window_ms = int((args.startup_wait_s + args.vision_duration_s + 3.0) * 1000)
    firmware_wait_s = max(args.startup_wait_s + args.vision_duration_s + 6.0, (runtime_window_ms / 1000.0) + 4.0)
    firmware_proc = launch_firmware(
        args.device,
        run_runtime_after_homing=True,
        runtime_exit_after_ms=runtime_window_ms,
    )
    firmware_output = ""

    time.sleep(args.startup_wait_s)

    stimulus_cmd = [
        sys.executable,
        str(HIL_DIR / "dmx_stimulus.py"),
        "--output",
        str(stimulus_output),
        "--fps",
        str(args.dmx_fps),
        "scenario",
        "--path",
        str(Path(args.scenario)),
    ]

    vision_proc = None
    if not args.skip_vision:
        vision_cmd = [
            sys.executable,
            str(HIL_DIR / "vision_observer.py"),
            "--output",
            str(vision_output),
            "--duration-s",
            str(args.vision_duration_s),
            "--prefix",
            "vision_runtime",
        ]
        vision_proc = subprocess.Popen(vision_cmd, cwd=str(HIL_DIR))
    try:
        if vision_proc is not None:
            time.sleep(args.vision_warmup_s)
        stimulus_proc = subprocess.run(stimulus_cmd, cwd=str(HIL_DIR), check=False, text=True, capture_output=True)
        if stimulus_proc.returncode != 0:
            print(stimulus_proc.stdout)
            print(stimulus_proc.stderr)
            return stimulus_proc.returncode
        if vision_proc is not None:
            vision_proc.wait(timeout=max(1.0, args.vision_duration_s + 2.0))
        firmware_output, _ = firmware_proc.communicate(timeout=max(1.0, firmware_wait_s))
    finally:
        if vision_proc is not None and vision_proc.poll() is None:
            vision_proc.terminate()
            try:
                vision_proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                vision_proc.kill()
        if firmware_proc.poll() is None:
            firmware_proc.terminate()
            try:
                firmware_output, _ = firmware_proc.communicate(timeout=2.0)
            except subprocess.TimeoutExpired:
                firmware_proc.kill()
                firmware_output, _ = firmware_proc.communicate()

    homing_result = read_remote_json(args.device, REMOTE_HOMING_FILE, 8.0, require_done=True)
    homing_output.write_text(json.dumps(homing_result, indent=2))
    runtime_status = read_remote_json(args.device, REMOTE_STATUS_FILE, 8.0, require_done=False)
    status_output.write_text(json.dumps(runtime_status, indent=2))

    vision_ok = True
    vision_message = "vision skipped"
    vision_details = None
    if not args.skip_vision:
        vision_rows = load_vision_rows(vision_output, args.axis)
        vision_ok, vision_message, vision_details = evaluate_large_move(
            vision_rows,
            settle_s=args.settle_s,
            min_change_deg=args.min_change_deg,
        )

    firmware_ok = (
        bool(runtime_status.get("runtime_active"))
        and bool(homing_result.get("success"))
        and int(runtime_status.get("dmx_frame_count", 0)) >= int(args.min_frame_count)
    )

    print(f"[INFO] Homing result: {homing_output}")
    print(f"[INFO] Runtime status: {status_output}")
    if vision_output is not None:
        print(f"[INFO] Vision log: {vision_output}")
    print(f"[INFO] Stimulus log: {stimulus_output}")
    if firmware_output.strip():
        print("[INFO] Firmware console captured")
    print(
        "[INFO] Firmware summary: runtime_active={} dmx_frames={} selected_trial={} pos={}/{}".format(
            runtime_status.get("runtime_active"),
            runtime_status.get("dmx_frame_count"),
            runtime_status.get("selected_trial"),
            runtime_status.get("current_position_steps"),
            runtime_status.get("target_position_steps"),
        )
    )
    if vision_details is not None:
        print(
            "[INFO] Vision summary: delta={delta_deg} deg travel_span={travel_span_deg} deg end_span={end_span_deg} deg visible_samples={visible_samples}".format(
                **vision_details
            )
        )
    else:
        print("[INFO] Vision summary: skipped")

    if firmware_ok and vision_ok:
        if firmware_output.strip():
            print(firmware_output.strip())
        print(f"[PASS] {vision_message}")
        return 0

    if not firmware_ok:
        if firmware_output.strip():
            print(firmware_output.strip())
        print("[FAIL] Firmware did not report stable DMX runtime activity")
        return 1

    if firmware_output.strip():
        print(firmware_output.strip())
    print(f"[FAIL] {vision_message}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
