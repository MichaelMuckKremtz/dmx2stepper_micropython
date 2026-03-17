#!/usr/bin/env python3
"""Verify runtime motion on jump-and-hold DMX targets with Pico status polling."""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
HIL_DIR = ROOT / "hil"
FIRMWARE_DIR = ROOT / "firmware"
CAPTURE_DIR = HIL_DIR / "captures"
DEFAULT_SCENARIO = HIL_DIR / "scenarios" / "jump_hold_positions.csv"
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


def load_scenario_rows(path: Path):
    rows = []
    with path.open(newline="") as handle:
        reader = csv.DictReader(line for line in handle if not line.lstrip().startswith("#"))
        for row in reader:
            rows.append(
                {
                    "offset_s": float(row["offset_s"]),
                    "channel": int(row["channel"]),
                    "target": int(row["target"]),
                    "fade_s": float(row.get("fade_s", 0.0) or 0.0),
                    "command_id": row.get("command_id") or "",
                }
            )
    return rows


def scenario_duration_s(rows) -> float:
    finish_s = 0.0
    for row in rows:
        finish_s = max(finish_s, float(row["offset_s"]) + float(row["fade_s"]))
    return finish_s


def extract_hold_windows(rows, min_hold_s: float = 1.0):
    by_command = {}
    for row in rows:
        command_id = str(row["command_id"])
        if not command_id.startswith("hold_"):
            continue
        stem = command_id
        if stem.endswith("_msb"):
            stem = stem[: -len("_msb")]
        elif stem.endswith("_lsb"):
            stem = stem[: -len("_lsb")]
        window = by_command.setdefault(
            stem,
            {
                "name": stem,
                "start_s": float(row["offset_s"]),
                "end_s": float(row["offset_s"]) + float(row["fade_s"]),
            },
        )
        window["start_s"] = min(window["start_s"], float(row["offset_s"]))
        window["end_s"] = max(window["end_s"], float(row["offset_s"]) + float(row["fade_s"]))

    windows = [item for item in by_command.values() if (item["end_s"] - item["start_s"]) >= float(min_hold_s)]
    windows.sort(key=lambda item: item["start_s"])
    return windows


def poll_status_worker(device: str, stop_event: threading.Event, sink, poll_interval_s: float) -> None:
    while not stop_event.is_set():
        try:
            payload = read_remote_json(device, REMOTE_STATUS_FILE, timeout_s=max(0.2, poll_interval_s), require_done=False)
        except Exception:
            time.sleep(poll_interval_s)
            continue
        payload["_host_poll_t"] = time.monotonic()
        sink.append(payload)
        time.sleep(poll_interval_s)


def write_telemetry_log(path: Path, samples) -> None:
    fieldnames = [
        "host_poll_t",
        "dmx_frame_count",
        "dmx_complete_frame_count",
        "dmx_short_frame_count",
        "dmx_last_bytes_received",
        "enabled",
        "applied_enabled",
        "current_position_steps",
        "target_position_steps",
        "current_speed_hz",
        "moved_last_update",
        "total_steps_emitted",
        "steps_emitted_while_disabled",
        "steps_emitted_while_stable_target",
        "stable_target_since_ms",
        "idle_since_ms",
    ]
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for sample in samples:
            writer.writerow(
                {
                    "host_poll_t": f"{sample.get('_host_poll_t', 0.0):.6f}",
                    "dmx_frame_count": sample.get("dmx_frame_count"),
                    "dmx_complete_frame_count": sample.get("dmx_complete_frame_count"),
                    "dmx_short_frame_count": sample.get("dmx_short_frame_count"),
                    "dmx_last_bytes_received": sample.get("dmx_last_bytes_received"),
                    "enabled": sample.get("enabled"),
                    "applied_enabled": sample.get("applied_enabled"),
                    "current_position_steps": sample.get("current_position_steps"),
                    "target_position_steps": sample.get("target_position_steps"),
                    "current_speed_hz": sample.get("current_speed_hz"),
                    "moved_last_update": sample.get("moved_last_update"),
                    "total_steps_emitted": sample.get("total_steps_emitted"),
                    "steps_emitted_while_disabled": sample.get("steps_emitted_while_disabled"),
                    "steps_emitted_while_stable_target": sample.get("steps_emitted_while_stable_target"),
                    "stable_target_since_ms": sample.get("stable_target_since_ms"),
                    "idle_since_ms": sample.get("idle_since_ms"),
                }
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


def summarize_angle_segment(segment):
    if len(segment) < 2:
        return {
            "visible_samples": len(segment),
            "travel_span_deg": None,
            "delta_deg": None,
            "mean_deg": None,
            "stddev_deg": None,
            "mean_abs_step_deg": None,
            "max_abs_step_deg": None,
        }

    step_deltas = [abs(segment[index] - segment[index - 1]) for index in range(1, len(segment))]
    return {
        "visible_samples": len(segment),
        "travel_span_deg": round(max(segment) - min(segment), 3),
        "delta_deg": round(segment[-1] - segment[0], 3),
        "mean_deg": round(statistics.mean(segment), 3),
        "stddev_deg": round(statistics.pstdev(segment), 3),
        "mean_abs_step_deg": round(statistics.mean(step_deltas), 3),
        "max_abs_step_deg": round(max(step_deltas), 3),
    }


def analyze_vision_hold_windows(vision_path: Path, axis: str, windows, warmup_s: float, settle_s: float):
    rows = load_vision_rows(vision_path, axis)
    if not rows:
        return []
    t0 = rows[0][0]
    values = unwrap_series([value for _, value in rows])
    series = [(timestamp - t0, values[index]) for index, (timestamp, _) in enumerate(rows)]
    summaries = []
    for window in windows:
        start_s = float(window["start_s"]) + float(warmup_s) + float(settle_s)
        end_s = float(window["end_s"]) + float(warmup_s)
        segment = [angle for timestamp, angle in series if start_s <= timestamp <= end_s]
        summary = summarize_angle_segment(segment)
        summary["name"] = window["name"]
        summaries.append(summary)
    return summaries


def analyze_vision_precision(vision_path: Path, axis: str, hold_summaries):
    rows = load_vision_rows(vision_path, axis)
    if len(rows) < 2:
        return {}

    timestamps = [timestamp for timestamp, _ in rows]
    values = unwrap_series([value for _, value in rows])
    frame_deltas_ms = [
        (timestamps[index] - timestamps[index - 1]) * 1000.0
        for index in range(1, len(timestamps))
        if timestamps[index] > timestamps[index - 1]
    ]
    unique_values = sorted(set(values))
    unique_steps = [
        round(unique_values[index] - unique_values[index - 1], 3)
        for index in range(1, len(unique_values))
        if (unique_values[index] - unique_values[index - 1]) > 0
    ]
    hold_stddevs = [item["stddev_deg"] for item in hold_summaries if item.get("stddev_deg") is not None]
    hold_spans = [item["travel_span_deg"] for item in hold_summaries if item.get("travel_span_deg") is not None]

    duration_s = timestamps[-1] - timestamps[0]
    return {
        "visible_samples": len(rows),
        "capture_duration_s": round(duration_s, 3),
        "mean_frame_dt_ms": round(statistics.mean(frame_deltas_ms), 3) if frame_deltas_ms else None,
        "median_frame_dt_ms": round(statistics.median(frame_deltas_ms), 3) if frame_deltas_ms else None,
        "p95_frame_dt_ms": round(sorted(frame_deltas_ms)[int(0.95 * (len(frame_deltas_ms) - 1))], 3)
        if frame_deltas_ms
        else None,
        "effective_fps": round(len(frame_deltas_ms) / duration_s, 3) if duration_s > 0 else None,
        "angle_quantization_floor_deg": min(unique_steps) if unique_steps else None,
        "median_hold_stddev_deg": round(statistics.median(hold_stddevs), 3) if hold_stddevs else None,
        "max_hold_stddev_deg": round(max(hold_stddevs), 3) if hold_stddevs else None,
        "median_hold_span_deg": round(statistics.median(hold_spans), 3) if hold_spans else None,
        "max_hold_span_deg": round(max(hold_spans), 3) if hold_spans else None,
    }


def evaluate_telemetry(telemetry_samples, runtime_status, min_frame_count: int, expect_live_telemetry: bool):
    failures = []
    warnings = []
    if expect_live_telemetry and not telemetry_samples:
        warnings.append("no live telemetry samples were captured during runtime; using final Pico status only")
    if int(runtime_status.get("dmx_frame_count", 0)) < int(min_frame_count):
        failures.append("insufficient DMX frame count")
    if int(runtime_status.get("dmx_short_frame_count", 0)) != 0:
        warnings.append("short DMX frames were observed but rejected before reaching control state")
    if int(runtime_status.get("steps_emitted_while_disabled", 0)) != 0:
        failures.append("steps_emitted_while_disabled is nonzero")
    if int(runtime_status.get("steps_emitted_while_stable_target", 0)) != 0:
        failures.append("steps_emitted_while_stable_target is nonzero")
    return failures, warnings


def build_argument_parser():
    parser = argparse.ArgumentParser(description="Verify jump-and-hold runtime behavior with Pico telemetry")
    parser.add_argument("--device", default="/dev/ttyACM0", help="MicroPython device path for mpremote")
    parser.add_argument("--upload", action="store_true", help="Upload firmware before running verification")
    parser.add_argument("--scenario", default=str(DEFAULT_SCENARIO), help="Scenario CSV for dmx_stimulus.py")
    parser.add_argument("--dmx-fps", type=float, default=44.0, help="DMX send rate during the scenario")
    parser.add_argument("--startup-wait-s", type=float, default=9.0, help="Time reserved for startup homing before stimulus starts")
    parser.add_argument("--runtime-margin-s", type=float, default=4.0, help="Extra runtime after the scenario finishes")
    parser.add_argument("--poll-interval-s", type=float, default=0.25, help="Host polling interval for controller_status.json")
    parser.add_argument("--min-frame-count", type=int, default=150, help="Minimum DMX frames the Pico must report")
    parser.add_argument("--debug-firmware", action="store_true", help="Enable Pico debug prints during the run")
    parser.add_argument("--status-stream", action="store_true", help="Keep periodic controller_status.json writes enabled during runtime")
    parser.add_argument("--with-vision", action="store_true", help="Run the camera observer alongside the jump-hold test")
    parser.add_argument("--vision-axis", default="T1", choices=("T1", "T2"), help="Observed axis for optional vision capture")
    parser.add_argument("--vision-warmup-s", type=float, default=0.5, help="Camera warmup before stimulus starts")
    parser.add_argument("--vision-filter-window", type=int, default=1, help="Median filter window for optional vision capture")
    parser.add_argument("--vision-deadband-deg", type=float, default=0.0, help="Deadband for optional vision capture")
    parser.add_argument("--hold-settle-s", type=float, default=0.8, help="Time trimmed off the start of each hold window before raw vision scoring")
    return parser


def main() -> int:
    parser = build_argument_parser()
    args = parser.parse_args()

    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    scenario_path = Path(args.scenario)
    scenario_rows = load_scenario_rows(scenario_path)
    hold_windows = extract_hold_windows(scenario_rows)
    scenario_finish_s = scenario_duration_s(scenario_rows)
    runtime_window_ms = int((args.startup_wait_s + scenario_finish_s + args.runtime_margin_s) * 1000)
    firmware_wait_s = (runtime_window_ms / 1000.0) + 6.0

    stimulus_output = timestamped_path("jump_hold_dmx", ".csv")
    homing_output = timestamped_path("jump_hold_homing_result", ".json")
    status_output = timestamped_path("jump_hold_runtime_status", ".json")
    telemetry_output = timestamped_path("jump_hold_telemetry", ".csv")
    vision_output = timestamped_path("vision_jump_hold", ".csv") if args.with_vision else None

    if args.upload:
        deploy_firmware(args.device)

    clear_remote_file(args.device, REMOTE_HOMING_FILE)
    clear_remote_file(args.device, REMOTE_STATUS_FILE)

    telemetry_samples = []
    telemetry_stop = threading.Event()
    poll_thread = None
    if args.status_stream:
        poll_thread = threading.Thread(
            target=poll_status_worker,
            args=(args.device, telemetry_stop, telemetry_samples, args.poll_interval_s),
            daemon=True,
        )

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
            "vision_jump_hold",
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
        if poll_thread is not None:
            poll_thread.start()
        stimulus_proc = subprocess.run(stimulus_cmd, cwd=str(HIL_DIR), check=False, text=True, capture_output=True)
        if stimulus_proc.returncode != 0:
            print(stimulus_proc.stdout)
            print(stimulus_proc.stderr)
            return stimulus_proc.returncode
        if vision_proc is not None:
            vision_proc.wait(timeout=max(1.0, scenario_finish_s + args.runtime_margin_s + 2.0))
        firmware_output, _ = firmware_proc.communicate(timeout=max(1.0, firmware_wait_s))
    finally:
        telemetry_stop.set()
        if poll_thread is not None:
            poll_thread.join(timeout=2.0)
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
    write_telemetry_log(telemetry_output, telemetry_samples)

    failures, warnings = evaluate_telemetry(
        telemetry_samples,
        runtime_status,
        args.min_frame_count,
        args.status_stream,
    )
    if not bool(homing_result.get("success")):
        failures.insert(0, "startup homing failed")

    vision_hold_summaries = []
    vision_precision_summary = {}
    if vision_output is not None:
        vision_hold_summaries = analyze_vision_hold_windows(
            vision_output,
            args.vision_axis,
            hold_windows,
            args.vision_warmup_s,
            args.hold_settle_s,
        )
        vision_precision_summary = analyze_vision_precision(
            vision_output,
            args.vision_axis,
            vision_hold_summaries,
        )

    summary = {
        "homing_result_path": str(homing_output),
        "runtime_status_path": str(status_output),
        "telemetry_log_path": str(telemetry_output),
        "stimulus_log_path": str(stimulus_output),
        "vision_log_path": None if vision_output is None else str(vision_output),
        "hold_windows": hold_windows,
        "runtime_summary": {
            "dmx_frame_count": int(runtime_status.get("dmx_frame_count", 0)),
            "dmx_short_frame_count": int(runtime_status.get("dmx_short_frame_count", 0)),
            "total_steps_emitted": int(runtime_status.get("total_steps_emitted", 0)),
            "steps_emitted_while_disabled": int(runtime_status.get("steps_emitted_while_disabled", 0)),
            "steps_emitted_while_stable_target": int(runtime_status.get("steps_emitted_while_stable_target", 0)),
            "current_position_steps": runtime_status.get("current_position_steps"),
            "target_position_steps": runtime_status.get("target_position_steps"),
        },
        "vision_precision_summary": vision_precision_summary,
        "vision_hold_summaries": vision_hold_summaries,
        "warnings": warnings,
        "failures": failures,
    }
    summary_output = timestamped_path("jump_hold_summary", ".json")
    summary_output.write_text(json.dumps(summary, indent=2))

    print(f"[INFO] Scenario: {scenario_path}")
    print(f"[INFO] Homing result: {homing_output}")
    print(f"[INFO] Runtime status: {status_output}")
    print(f"[INFO] Telemetry log: {telemetry_output}")
    print(f"[INFO] Stimulus log: {stimulus_output}")
    print(f"[INFO] Summary: {summary_output}")
    if vision_output is not None:
        print(f"[INFO] Vision log: {vision_output}")
    if vision_precision_summary:
        print(
            "[INFO] Vision precision: fps={} median_dt_ms={} quant_floor_deg={} median_hold_stddev={} max_hold_span={}".format(
                vision_precision_summary.get("effective_fps"),
                vision_precision_summary.get("median_frame_dt_ms"),
                vision_precision_summary.get("angle_quantization_floor_deg"),
                vision_precision_summary.get("median_hold_stddev_deg"),
                vision_precision_summary.get("max_hold_span_deg"),
            )
        )
    if firmware_output.strip():
        print("[INFO] Firmware console captured")
    print(
        "[INFO] Runtime summary: frames={} short_frames={} total_steps={} stable_target_steps={} pos={}/{}".format(
            runtime_status.get("dmx_frame_count"),
            runtime_status.get("dmx_short_frame_count"),
            runtime_status.get("total_steps_emitted"),
            runtime_status.get("steps_emitted_while_stable_target"),
            runtime_status.get("current_position_steps"),
            runtime_status.get("target_position_steps"),
        )
    )
    if vision_hold_summaries:
        for item in vision_hold_summaries:
            print(
                "[INFO] Hold {}: visible_samples={} travel_span={} delta={} stddev={} max_abs_step={}".format(
                    item["name"],
                    item["visible_samples"],
                    item["travel_span_deg"],
                    item["delta_deg"],
                    item["stddev_deg"],
                    item["max_abs_step_deg"],
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
    print("[PASS] Jump-hold runtime verification passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
