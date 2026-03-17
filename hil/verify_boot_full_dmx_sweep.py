#!/usr/bin/env python3
"""Capture startup homing and verify a full 16-bit DMX sweep against vision data."""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
HIL_DIR = ROOT / "hil"
CAPTURE_DIR = HIL_DIR / "captures"
REMOTE_HOMING_FILE = "homing_result.json"


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


def clear_remote_file(device: str, remote_path: str) -> None:
    run_command(
        ["mpremote", "connect", device, "fs", "rm", remote_path],
        check=False,
        capture_output=True,
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


def stream_process(proc: subprocess.Popen[str], prefix: str) -> str:
    lines = []
    if proc.stdout is None:
        return ""
    for line in proc.stdout:
        line = line.rstrip()
        if line:
            print(f"{prefix} {line}")
            lines.append(line)
    return "\n".join(lines)


def write_sweep_scenario(path: Path, preposition_s: float, sweep_duration_s: float, speed: int, accel: int, enable: int) -> None:
    fieldnames = ["offset_s", "channel", "target", "fade_s", "ramp_s", "command_id", "scenario"]
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow(
            {
                "offset_s": "0.0",
                "channel": "5",
                "target": str(speed),
                "fade_s": "0.0",
                "ramp_s": "0.0",
                "command_id": "speed",
                "scenario": "full_u16_sweep",
            }
        )
        writer.writerow(
            {
                "offset_s": "0.0",
                "channel": "6",
                "target": str(accel),
                "fade_s": "0.0",
                "ramp_s": "0.0",
                "command_id": "accel",
                "scenario": "full_u16_sweep",
            }
        )
        writer.writerow(
            {
                "offset_s": "0.0",
                "channel": "7",
                "target": str(enable),
                "fade_s": "0.0",
                "ramp_s": "0.0",
                "command_id": "enable",
                "scenario": "full_u16_sweep",
            }
        )
        writer.writerow(
            {
                "offset_s": "0.0",
                "channel": "1",
                "target": "0",
                "fade_s": "0.0",
                "ramp_s": "0.0",
                "command_id": "pos_msb_start",
                "scenario": "full_u16_sweep",
            }
        )
        writer.writerow(
            {
                "offset_s": "0.0",
                "channel": "2",
                "target": "0",
                "fade_s": "0.0",
                "ramp_s": "0.0",
                "command_id": "pos_lsb_start",
                "scenario": "full_u16_sweep",
            }
        )
        writer.writerow(
            {
                "offset_s": f"{preposition_s:.3f}",
                "channel": "1",
                "target": "255",
                "fade_s": f"{sweep_duration_s:.3f}",
                "ramp_s": "0.0",
                "command_id": "pos_msb_ramp",
                "scenario": "full_u16_sweep",
            }
        )
        writer.writerow(
            {
                "offset_s": f"{preposition_s:.3f}",
                "channel": "2",
                "target": "255",
                "fade_s": f"{sweep_duration_s:.3f}",
                "ramp_s": "0.0",
                "command_id": "pos_lsb_ramp",
                "scenario": "full_u16_sweep",
            }
        )


def load_visible_angles(path: Path, axis: str):
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


def analyze_frame_rate(rows):
    if len(rows) < 3:
        return {"visible_samples": len(rows), "fps_mean": None, "fps_median": None, "dt_median_ms": None, "dt_p95_ms": None}

    deltas = []
    previous_ts = rows[0][0]
    for ts, _ in rows[1:]:
        delta = ts - previous_ts
        if delta > 0:
            deltas.append(delta)
        previous_ts = ts

    if not deltas:
        return {"visible_samples": len(rows), "fps_mean": None, "fps_median": None, "dt_median_ms": None, "dt_p95_ms": None}

    ordered = sorted(deltas)
    p95_index = min(len(ordered) - 1, max(0, int(round(0.95 * (len(ordered) - 1)))))
    return {
        "visible_samples": len(rows),
        "fps_mean": round(1.0 / statistics.mean(deltas), 3),
        "fps_median": round(1.0 / statistics.median(deltas), 3),
        "dt_median_ms": round(statistics.median(deltas) * 1000.0, 3),
        "dt_p95_ms": round(ordered[p95_index] * 1000.0, 3),
    }


def unwrap_series(values: list[float]) -> list[float]:
    if not values:
        return []

    unwrapped = [values[0]]
    for value in values[1:]:
        candidates = [value - 360.0, value, value + 360.0]
        unwrapped.append(min(candidates, key=lambda item: abs(item - unwrapped[-1])))
    return unwrapped


def sample_window(rows, start_s: float, end_s: float):
    window = [angle for ts, angle in rows if start_s <= ts <= end_s]
    if len(window) < 2:
        return None
    return float(statistics.median(window))


def analyze_boot_motion(rows, observer_start_s: float, homing_done_s: float):
    values = [angle for ts, angle in rows if observer_start_s <= ts <= homing_done_s]
    if len(values) < 3:
        return {"visible_samples": len(values), "boot_span_deg": None}
    unwrapped = unwrap_series(values)
    return {
        "visible_samples": len(unwrapped),
        "boot_span_deg": round(max(unwrapped) - min(unwrapped), 3),
    }


def analyze_sweep(rows, ramp_start_s: float, ramp_end_s: float, tolerance_deg: float):
    visible_rows = [(ts, angle) for ts, angle in rows if ramp_start_s <= ts <= ramp_end_s]
    if len(visible_rows) < 40:
        return {
            "ok": False,
            "reason": "not enough visible samples during continuous sweep",
            "visible_samples": len(visible_rows),
        }

    times = [ts for ts, _ in visible_rows]
    values = unwrap_series([angle for _, angle in visible_rows])
    edge_count = min(5, len(values) // 4)
    start_angle = statistics.median(values[:edge_count])
    end_angle = statistics.median(values[-edge_count:])
    total_delta = end_angle - start_angle
    if abs(total_delta) < 40.0:
        return {
            "ok": False,
            "reason": "observed sweep span too small",
            "visible_samples": len(visible_rows),
            "total_delta_deg": round(total_delta, 3),
        }

    sign = 1.0 if total_delta >= 0 else -1.0
    normalized_errors = []
    monotonic_violations = 0
    monotonic_deltas = []
    previous_value = None
    total_span = abs(total_delta)

    for ts, value in zip(times, values):
        expected = (ts - ramp_start_s) / max(1e-6, ramp_end_s - ramp_start_s)
        expected = max(0.0, min(1.0, expected))
        actual = ((value - start_angle) * sign) / total_span
        normalized_errors.append(abs(actual - expected))
        if previous_value is not None:
            delta = (value - previous_value) * sign
            monotonic_deltas.append(round(delta, 3))
            if delta < -abs(tolerance_deg):
                monotonic_violations += 1
        previous_value = value

    mae = statistics.mean(normalized_errors)
    max_error = max(normalized_errors)
    response_span = max(values) - min(values)

    if monotonic_violations > 1 or max_error > 0.08 or mae > 0.03:
        return {
            "ok": False,
            "reason": "continuous sweep was not monotonic",
            "visible_samples": len(visible_rows),
            "total_delta_deg": round(total_delta, 3),
            "response_span_deg": round(response_span, 3),
            "linearity_mae": round(mae, 4),
            "linearity_max_error": round(max_error, 4),
            "monotonic_violations": int(monotonic_violations),
            "monotonic_deltas_deg": monotonic_deltas,
        }

    return {
        "ok": True,
        "reason": "continuous sweep response was monotonic",
        "visible_samples": len(visible_rows),
        "total_delta_deg": round(total_delta, 3),
        "response_span_deg": round(response_span, 3),
        "linearity_mae": round(mae, 4),
        "linearity_max_error": round(max_error, 4),
        "monotonic_violations": int(monotonic_violations),
        "monotonic_deltas_deg": monotonic_deltas,
    }


def build_argument_parser():
    parser = argparse.ArgumentParser(description="Observe boot homing and verify a full 16-bit DMX sweep")
    parser.add_argument("--device", default="/dev/ttyACM0", help="MicroPython device path for mpremote")
    parser.add_argument("--axis", default="T1", choices=("T1", "T2"), help="Observed axis to evaluate")
    parser.add_argument("--startup-script", default=str(ROOT / "start_firmware.sh"), help="Shell script used to upload/reset the Pico")
    parser.add_argument("--vision-filter-window", type=int, default=1, help="Vision median filter window")
    parser.add_argument("--vision-deadband-deg", type=float, default=0.0, help="Vision deadband in degrees")
    parser.add_argument("--startup-wait-s", type=float, default=15.0, help="Time reserved for boot homing before the sweep starts")
    parser.add_argument("--post-home-settle-s", type=float, default=1.0, help="Extra settle time before the DMX sweep")
    parser.add_argument("--preposition-s", type=float, default=1.0, help="Time to hold DMX 0x0000 before the linear sweep starts")
    parser.add_argument("--sweep-duration-s", type=float, default=12.0, help="Duration of the continuous linear DMX sweep")
    parser.add_argument("--dmx-fps", type=float, default=44.0, help="DMX send rate")
    parser.add_argument("--speed-channel-value", type=int, default=220, help="DMX channel 5 value during the sweep")
    parser.add_argument("--accel-channel-value", type=int, default=220, help="DMX channel 6 value during the sweep")
    parser.add_argument("--enable-channel-value", type=int, default=255, help="DMX channel 7 value during the sweep")
    parser.add_argument("--monotonic-tolerance-deg", type=float, default=2.0, help="Allowed backwards step per hold in degrees")
    return parser


def main() -> int:
    args = build_argument_parser().parse_args()
    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)

    observer_output = timestamped_path("vision_boot_sweep", ".csv")
    stimulus_output = timestamped_path("dmx_boot_sweep", ".csv")
    scenario_output = timestamped_path("full_u16_sweep", ".csv")
    homing_output = timestamped_path("boot_homing_result", ".json")
    summary_output = timestamped_path("boot_sweep_summary", ".json")

    write_sweep_scenario(
        scenario_output,
        preposition_s=float(args.preposition_s),
        sweep_duration_s=float(args.sweep_duration_s),
        speed=int(args.speed_channel_value),
        accel=int(args.accel_channel_value),
        enable=int(args.enable_channel_value),
    )

    vision_duration_s = float(args.startup_wait_s) + float(args.post_home_settle_s) + float(args.preposition_s) + float(args.sweep_duration_s) + 3.0
    vision_cmd = [
        sys.executable,
        str(HIL_DIR / "vision_observer.py"),
        "--output",
        str(observer_output),
        "--duration-s",
        str(vision_duration_s),
        "--filter-window",
        str(args.vision_filter_window),
        "--deadband-deg",
        str(args.vision_deadband_deg),
        "--prefix",
        "vision_boot_sweep",
    ]

    print(f"[INFO] Vision capture: {observer_output}")
    print(f"[INFO] Sweep scenario: {scenario_output}")
    vision_proc = subprocess.Popen(
        vision_cmd,
        cwd=str(HIL_DIR),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    observer_start_s = time.monotonic()

    try:
        time.sleep(0.5)
        clear_remote_file(args.device, REMOTE_HOMING_FILE)

        print(f"[INFO] Starting firmware with {args.startup_script}")
        startup_begin_s = time.monotonic()
        startup_proc = subprocess.Popen(
            [str(args.startup_script)],
            cwd=str(ROOT),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        startup_output = stream_process(startup_proc, "[FW]")
        startup_returncode = startup_proc.wait(timeout=30.0)
        if startup_returncode != 0:
            raise RuntimeError(f"startup script failed with exit code {startup_returncode}")

        print("[INFO] Waiting {:.1f}s for autonomous homing to finish".format(float(args.startup_wait_s)))
        time.sleep(float(args.startup_wait_s))
        homing_done_s = startup_begin_s + float(args.startup_wait_s)
        time.sleep(float(args.post_home_settle_s))
        sweep_start_s = time.monotonic()
        print(
            "[INFO] Sending one continuous linear DMX sweep from 0x0000 to 0xFFFF on channels 1+2"
        )
        stimulus_cmd = [
            sys.executable,
            str(HIL_DIR / "dmx_stimulus.py"),
            "--output",
            str(stimulus_output),
            "--fps",
            str(args.dmx_fps),
            "scenario",
            "--path",
            str(scenario_output),
        ]
        stimulus_proc = subprocess.run(
            stimulus_cmd,
            cwd=str(HIL_DIR),
            text=True,
            capture_output=True,
            check=False,
        )
        if stimulus_proc.stdout.strip():
            print(stimulus_proc.stdout.strip())
        if stimulus_proc.stderr.strip():
            print(stimulus_proc.stderr.strip())
        if stimulus_proc.returncode != 0:
            raise RuntimeError(f"dmx stimulus failed with exit code {stimulus_proc.returncode}")

        if vision_proc.poll() is None:
            vision_output = stream_process(vision_proc, "[CV]")
            vision_returncode = vision_proc.wait(timeout=5.0)
            if vision_returncode != 0:
                raise RuntimeError(f"vision observer failed with exit code {vision_returncode}")
        else:
            vision_output = ""

        rows = load_visible_angles(observer_output, args.axis)
        if not rows:
            raise RuntimeError(f"no visible samples recorded for axis {args.axis}")
        frame_metrics = analyze_frame_rate(rows)

        homing_result = read_remote_json(args.device, REMOTE_HOMING_FILE, timeout_s=5.0, require_done=True)
        homing_output.write_text(json.dumps(homing_result, indent=2))

        selected_trial_index = homing_result.get("selected_trial")
        if selected_trial_index is None:
            raise RuntimeError("homing finished without selecting a trial")
        selected_trial = homing_result["trials"][int(selected_trial_index)]
        if not homing_result.get("runtime_ready"):
            raise RuntimeError("homing finished but runtime is not ready")
        if bool(homing_result.get("home_measure_travel_steps", True)):
            raise RuntimeError("firmware booted with measured-span homing enabled")
        if int(selected_trial.get("runtime_travel_steps", -1)) != 24000:
            raise RuntimeError(f"unexpected runtime span {selected_trial.get('runtime_travel_steps')}")
        if int(selected_trial.get("runtime_min_position_steps", -1)) != 500:
            raise RuntimeError(f"unexpected runtime minimum {selected_trial.get('runtime_min_position_steps')}")
        if int(selected_trial.get("runtime_max_position_steps", -1)) != 23500:
            raise RuntimeError(f"unexpected runtime maximum {selected_trial.get('runtime_max_position_steps')}")
        if selected_trial.get("stop_reason") != "fixed_span_centered":
            raise RuntimeError(f"unexpected homing stop_reason {selected_trial.get('stop_reason')}")

        print(
            "[INFO] Homing result: trial={} stop_reason={} step=GP{} dir=GP{} center={}/{}".format(
                homing_result["selected_trial"],
                selected_trial["stop_reason"],
                selected_trial["step_pin"],
                selected_trial["dir_pin"],
                selected_trial["center_steps_moved"],
                selected_trial["center_steps_requested"],
            )
        )

        boot_metrics = analyze_boot_motion(rows, observer_start_s, homing_done_s)
        ramp_start_s = sweep_start_s + float(args.preposition_s)
        ramp_end_s = ramp_start_s + float(args.sweep_duration_s)
        sweep_metrics = analyze_sweep(
            rows,
            ramp_start_s,
            ramp_end_s,
            tolerance_deg=float(args.monotonic_tolerance_deg),
        )

        summary = {
            "observer_output": str(observer_output),
            "stimulus_output": str(stimulus_output),
            "scenario_output": str(scenario_output),
            "homing_output": str(homing_output),
            "startup_output": startup_output,
            "vision_output": vision_output,
            "homing_result": homing_result,
            "frame_metrics": frame_metrics,
            "boot_metrics": boot_metrics,
            "sweep_metrics": sweep_metrics,
        }
        summary_output.write_text(json.dumps(summary, indent=2))

        print(f"[INFO] Summary written to {summary_output}")
        print(
            "[INFO] Raw CV fps mean={} median={} dt_p95={}ms".format(
                frame_metrics["fps_mean"],
                frame_metrics["fps_median"],
                frame_metrics["dt_p95_ms"],
            )
        )
        print(
            "[INFO] Boot motion span: {} deg across {} visible samples".format(
                boot_metrics["boot_span_deg"],
                boot_metrics["visible_samples"],
            )
        )
        if not sweep_metrics["ok"]:
            print(json.dumps(sweep_metrics, indent=2))
            raise RuntimeError(sweep_metrics["reason"])

        print(
            "[INFO] Sweep verified: span={} deg across {} visible samples, MAE={}, max_err={}".format(
                sweep_metrics["response_span_deg"],
                sweep_metrics["visible_samples"],
                sweep_metrics["linearity_mae"],
                sweep_metrics["linearity_max_error"],
            )
        )
        return 0
    finally:
        if vision_proc.poll() is None:
            vision_proc.terminate()
            try:
                vision_proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                vision_proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
