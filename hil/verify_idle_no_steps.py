#!/usr/bin/env python3
"""Verify that idle DMX states emit no step pulses after startup homing."""

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
REMOTE_HOMING_FILE = "homing_result.json"
REMOTE_STATUS_FILE = "controller_status.json"
DEFAULT_SCENARIOS = {
    "disabled-idle": HIL_DIR / "scenarios" / "disabled_idle_hold.csv",
    "fixed-target": HIL_DIR / "scenarios" / "fixed_target_hold.csv",
}


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


def build_exec_code(runtime_exit_after_ms: int, debug_logging: bool, status_stream_enabled: bool) -> str:
    return "; ".join(
        [
            "import sys",
            "sys.modules.pop('main', None)",
            "sys.modules.pop('config', None)",
            "import config",
            "config.RUN_RUNTIME_AFTER_HOMING=True",
            f"config.RUNTIME_EXIT_AFTER_MS={int(runtime_exit_after_ms)}",
            f"config.DEBUG_LOGGING={bool(debug_logging)}",
            f"config.RUNTIME_STATUS_STREAM_ENABLED={bool(status_stream_enabled)}",
            "import main",
            "main.main()",
        ]
    )


def launch_firmware(device: str, runtime_exit_after_ms: int, debug_logging: bool, status_stream_enabled: bool):
    return subprocess.Popen(
        [
            "mpremote",
            "connect",
            device,
            "exec",
            build_exec_code(runtime_exit_after_ms, debug_logging, status_stream_enabled),
        ],
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


def scenario_duration_s(path: Path) -> float:
    finish_s = 0.0
    with path.open(newline="") as handle:
        reader = csv.DictReader(line for line in handle if not line.lstrip().startswith("#"))
        for row in reader:
            offset_s = float(row["offset_s"])
            fade_s = float(row.get("fade_s", 0.0) or 0.0)
            finish_s = max(finish_s, offset_s + fade_s)
    return finish_s


def resolve_scenario(args) -> Path:
    if args.scenario:
        return Path(args.scenario)
    if args.mode == "replay":
        raise ValueError("--scenario is required for replay mode")
    return DEFAULT_SCENARIOS[args.mode]


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


def summarize_vision_idle(path: Path, axis: str):
    rows = load_vision_rows(path, axis)
    if not rows:
        return {"visible_samples": 0, "travel_span_deg": None, "delta_deg": None}
    values = unwrap_series([value for _, value in rows])
    return {
        "visible_samples": len(values),
        "travel_span_deg": round(max(values) - min(values), 3),
        "delta_deg": round(values[-1] - values[0], 3),
    }


def assert_required_fields(runtime_status):
    required = (
        "total_steps_emitted",
        "steps_emitted_while_disabled",
        "steps_emitted_while_stable_target",
        "dmx_short_frame_count",
        "dmx_last_bytes_received",
        "dmx_frame_complete",
        "recent_motion_events",
    )
    missing = [field for field in required if field not in runtime_status]
    if missing:
        raise RuntimeError(f"runtime status missing fields: {', '.join(missing)}")


def recent_event_mismatches(runtime_status, expected_enabled: bool | None, expected_target_u16: int | None):
    mismatches = []
    since_ms = runtime_status.get("stable_target_since_ms")
    for event in runtime_status.get("recent_motion_events", []):
        if since_ms is not None and event.get("t_ms") is not None and int(event.get("t_ms")) < int(since_ms):
            continue
        event_enabled = event.get("requested_enabled")
        event_target = event.get("target_u16")
        if expected_enabled is not None and event_enabled is not None and bool(event_enabled) != bool(expected_enabled):
            mismatches.append(f"recent event requested_enabled={event_enabled}")
            continue
        if expected_target_u16 is not None and event_target is not None and int(event_target) != int(expected_target_u16):
            mismatches.append(f"recent event target_u16={event_target}")
    return mismatches


def evaluate_mode(mode: str, runtime_status, min_frame_count: int):
    assert_required_fields(runtime_status)

    failures = []
    warnings = []
    if not bool(runtime_status.get("runtime_active")):
        failures.append("runtime_active is false")
    if int(runtime_status.get("dmx_frame_count", 0)) < int(min_frame_count):
        failures.append("insufficient DMX frame count")
    if int(runtime_status.get("dmx_short_frame_count", 0)) != 0:
        warnings.append(
            "short DMX frames were observed but rejected before reaching control state"
        )

    if mode == "disabled-idle":
        if bool(runtime_status.get("enabled")):
            failures.append("DMX enable did not stay false")
        if int(runtime_status.get("total_steps_emitted", 0)) != 0:
            failures.append("runtime emitted steps during disabled idle hold")
        if int(runtime_status.get("steps_emitted_while_disabled", 0)) != 0:
            failures.append("steps_emitted_while_disabled is nonzero")
        mismatches = recent_event_mismatches(runtime_status, expected_enabled=False, expected_target_u16=32768)
        if mismatches:
            failures.append("unexpected enable/target transitions were observed during disabled hold")
    elif mode == "fixed-target":
        if not bool(runtime_status.get("enabled")):
            failures.append("DMX enable did not stay true")
        if int(runtime_status.get("total_steps_emitted", 0)) <= 0:
            failures.append("fixed-target run never moved away from center")
        if int(runtime_status.get("steps_emitted_while_stable_target", 0)) != 0:
            failures.append("steps_emitted_while_stable_target is nonzero")
        if int(runtime_status.get("current_position_steps", -1)) != int(runtime_status.get("target_position_steps", -2)):
            failures.append("axis did not finish at target")
        mismatches = recent_event_mismatches(
            runtime_status,
            expected_enabled=True,
            expected_target_u16=int(runtime_status.get("applied_target_u16", 0)),
        )
        if mismatches:
            failures.append("unexpected enable/target transitions were observed during fixed-target hold")
    elif mode == "replay":
        if int(runtime_status.get("steps_emitted_while_disabled", 0)) != 0:
            failures.append("steps_emitted_while_disabled is nonzero")
        if int(runtime_status.get("steps_emitted_while_stable_target", 0)) != 0:
            failures.append("steps_emitted_while_stable_target is nonzero")
    else:
        raise ValueError(f"Unsupported mode: {mode}")

    return failures, warnings


def build_argument_parser():
    parser = argparse.ArgumentParser(description="Verify that idle DMX states emit no step pulses")
    parser.add_argument("--device", default="/dev/ttyACM0", help="MicroPython device path for mpremote")
    parser.add_argument("--upload", action="store_true", help="Upload firmware before running verification")
    parser.add_argument(
        "--mode",
        choices=("disabled-idle", "fixed-target", "replay"),
        default="disabled-idle",
        help="Verification mode",
    )
    parser.add_argument("--scenario", help="Override scenario CSV path or provide replay scenario")
    parser.add_argument("--dmx-fps", type=float, default=44.0, help="DMX send rate during the scenario")
    parser.add_argument("--startup-wait-s", type=float, default=9.0, help="Time reserved for startup homing before stimulus starts")
    parser.add_argument("--runtime-margin-s", type=float, default=4.0, help="Extra runtime after the scenario finishes")
    parser.add_argument("--min-frame-count", type=int, default=100, help="Minimum DMX frames the Pico must report")
    parser.add_argument("--debug-firmware", action="store_true", help="Enable Pico debug prints during the run")
    parser.add_argument("--status-stream", action="store_true", help="Keep periodic controller_status.json writes enabled during runtime")
    parser.add_argument("--with-vision", action="store_true", help="Run the camera observer in raw-mode alongside the stimulus")
    parser.add_argument("--vision-axis", default="T1", choices=("T1", "T2"), help="Observed axis for optional vision capture")
    parser.add_argument("--vision-warmup-s", type=float, default=0.5, help="Camera warmup before stimulus starts")
    parser.add_argument("--vision-filter-window", type=int, default=1, help="Median filter window for optional idle vision capture")
    parser.add_argument("--vision-deadband-deg", type=float, default=0.0, help="Deadband for optional idle vision capture")
    return parser


def main() -> int:
    parser = build_argument_parser()
    args = parser.parse_args()

    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    scenario_path = resolve_scenario(args)
    scenario_finish_s = scenario_duration_s(scenario_path)
    runtime_window_ms = int((args.startup_wait_s + scenario_finish_s + args.runtime_margin_s) * 1000)
    firmware_wait_s = (runtime_window_ms / 1000.0) + 6.0

    stimulus_output = timestamped_path("idle_dmx", ".csv")
    homing_output = timestamped_path("idle_homing_result", ".json")
    status_output = timestamped_path("idle_runtime_status", ".json")
    vision_output = timestamped_path("vision_idle", ".csv") if args.with_vision else None

    if args.upload:
        deploy_firmware(args.device)

    clear_remote_file(args.device, REMOTE_HOMING_FILE)
    clear_remote_file(args.device, REMOTE_STATUS_FILE)

    firmware_proc = launch_firmware(
        args.device,
        runtime_window_ms,
        args.debug_firmware,
        args.status_stream,
    )
    firmware_output = ""
    time.sleep(args.startup_wait_s)

    vision_proc = None
    if args.with_vision:
        vision_cmd = [
            sys.executable,
            str(HIL_DIR / "vision_observer.py"),
            "--output",
            str(vision_output),
            "--duration-s",
            str(scenario_finish_s + args.runtime_margin_s),
            "--prefix",
            "vision_idle",
            "--filter-window",
            str(args.vision_filter_window),
            "--deadband-deg",
            str(args.vision_deadband_deg),
        ]
        vision_proc = subprocess.Popen(vision_cmd, cwd=str(HIL_DIR))

    stimulus_cmd = [
        sys.executable,
        str(HIL_DIR / "dmx_stimulus.py"),
        "--output",
        str(stimulus_output),
        "--fps",
        str(args.dmx_fps),
        "scenario",
        "--path",
        str(scenario_path),
    ]

    try:
        if vision_proc is not None:
            time.sleep(args.vision_warmup_s)
        stimulus_proc = subprocess.run(stimulus_cmd, cwd=str(HIL_DIR), check=False, text=True, capture_output=True)
        if stimulus_proc.returncode != 0:
            print(stimulus_proc.stdout)
            print(stimulus_proc.stderr)
            return stimulus_proc.returncode
        if vision_proc is not None:
            vision_proc.wait(timeout=max(1.0, scenario_finish_s + args.runtime_margin_s + 2.0))
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
    runtime_status = read_remote_json(args.device, REMOTE_STATUS_FILE, 8.0, require_done=False)
    homing_output.write_text(json.dumps(homing_result, indent=2))
    status_output.write_text(json.dumps(runtime_status, indent=2))

    vision_summary = None
    if vision_output is not None:
        vision_summary = summarize_vision_idle(vision_output, args.vision_axis)

    failures, warnings = evaluate_mode(args.mode, runtime_status, args.min_frame_count)
    if not bool(homing_result.get("success")):
        failures.insert(0, "startup homing failed")

    print(f"[INFO] Mode: {args.mode}")
    print(f"[INFO] Scenario: {scenario_path}")
    print(f"[INFO] Homing result: {homing_output}")
    print(f"[INFO] Runtime status: {status_output}")
    print(f"[INFO] Stimulus log: {stimulus_output}")
    if vision_output is not None:
        print(f"[INFO] Vision log: {vision_output}")
    if firmware_output.strip():
        print("[INFO] Firmware console captured")
    print(
        "[INFO] Firmware summary: frames={} short_frames={} total_steps={} disabled_steps={} stable_target_steps={} pos={}/{}".format(
            runtime_status.get("dmx_frame_count"),
            runtime_status.get("dmx_short_frame_count"),
            runtime_status.get("total_steps_emitted"),
            runtime_status.get("steps_emitted_while_disabled"),
            runtime_status.get("steps_emitted_while_stable_target"),
            runtime_status.get("current_position_steps"),
            runtime_status.get("target_position_steps"),
        )
    )
    if vision_summary is not None:
        print(
            "[INFO] Vision idle summary: visible_samples={} travel_span={} delta={}".format(
                vision_summary["visible_samples"],
                vision_summary["travel_span_deg"],
                vision_summary["delta_deg"],
            )
        )
    for warning in warnings:
        print(f"[WARN] {warning}")

    if failures:
        if firmware_output.strip():
            print(firmware_output.strip())
        for failure in failures:
            print(f"[FAIL] {failure}")
        return 1

    if firmware_output.strip():
        print(firmware_output.strip())
    print("[PASS] Idle no-step verification passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
