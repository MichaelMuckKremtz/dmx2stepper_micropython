#!/usr/bin/env python3
"""Run the current motion diagnostics suite and write a compact summary."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
HIL_DIR = ROOT / "hil"
CAPTURE_DIR = HIL_DIR / "captures"


def timestamped_path(prefix: str, suffix: str) -> Path:
    stamp = time.strftime("%Y%m%d_%H%M%S")
    return CAPTURE_DIR / f"{prefix}_{stamp}{suffix}"


def run_step(name: str, cmd):
    started = time.time()
    proc = subprocess.run(cmd, cwd=str(HIL_DIR), text=True, capture_output=True)
    return {
        "name": name,
        "command": cmd,
        "returncode": int(proc.returncode),
        "elapsed_s": round(time.time() - started, 3),
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }


def build_argument_parser():
    parser = argparse.ArgumentParser(description="Run the current DMX motion diagnostics suite")
    parser.add_argument("--device", default="/dev/ttyACM0", help="MicroPython device path for mpremote")
    parser.add_argument("--skip-homing", action="store_true", help="Skip the homing benchmark")
    parser.add_argument("--skip-idle", action="store_true", help="Skip disabled-idle and fixed-target checks")
    parser.add_argument("--skip-jump-hold", action="store_true", help="Skip jump-hold runtime diagnostics")
    parser.add_argument("--with-vision", action="store_true", help="Enable raw/no-deadband vision for runtime checks")
    parser.add_argument("--axis", default="T1", choices=("T1", "T2"), help="Observed axis for vision checks")
    parser.add_argument("--upload", action="store_true", help="Upload firmware before the first run")
    return parser


def main() -> int:
    parser = build_argument_parser()
    args = parser.parse_args()

    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    steps = []
    upload_pending = bool(args.upload)

    if not args.skip_homing:
        cmd = [
            sys.executable,
            str(HIL_DIR / "verify_pio_homing.py"),
            "--device",
            args.device,
            "--axis",
            args.axis,
            "--vision-filter-window",
            "1",
            "--vision-deadband-deg",
            "0",
        ]
        if upload_pending:
            cmd.append("--upload")
            upload_pending = False
        steps.append(run_step("homing_benchmark_raw_vision", cmd))

    if not args.skip_idle:
        for mode in ("disabled-idle", "fixed-target"):
            cmd = [
                sys.executable,
                str(HIL_DIR / "verify_idle_no_steps.py"),
                "--device",
                args.device,
                "--mode",
                mode,
            ]
            if upload_pending:
                cmd.append("--upload")
                upload_pending = False
            steps.append(run_step(f"idle_{mode}", cmd))

    if not args.skip_jump_hold:
        cmd = [
            sys.executable,
            str(HIL_DIR / "verify_jump_hold_runtime.py"),
            "--device",
            args.device,
            "--vision-axis",
            args.axis,
        ]
        if args.with_vision:
            cmd.extend(
                [
                    "--with-vision",
                    "--vision-filter-window",
                    "1",
                    "--vision-deadband-deg",
                    "0",
                ]
            )
        if upload_pending:
            cmd.append("--upload")
            upload_pending = False
        steps.append(run_step("jump_hold_runtime", cmd))

    failures = [step["name"] for step in steps if step["returncode"] != 0]
    summary = {
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "steps": steps,
        "failures": failures,
        "success": not failures,
    }
    summary_path = timestamped_path("motion_diagnostics", ".json")
    summary_path.write_text(json.dumps(summary, indent=2))

    print(f"[INFO] Summary: {summary_path}")
    for step in steps:
        print("[INFO] {} rc={} elapsed_s={}".format(step["name"], step["returncode"], step["elapsed_s"]))
    if failures:
        for name in failures:
            print(f"[FAIL] {name}")
        return 1

    print("[PASS] Motion diagnostics suite passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
