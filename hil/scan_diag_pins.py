#!/usr/bin/env python3
"""Scan DIAG GPIO candidates with optical full-span verification."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
HIL_DIR = ROOT / "hil"
DEFAULT_PINS = tuple(range(8, 14))


def build_argument_parser():
    parser = argparse.ArgumentParser(description="Scan DIAG pin candidates with the OpenCV homing verifier")
    parser.add_argument("--device", default="/dev/ttyACM0", help="MicroPython device path for mpremote")
    parser.add_argument("--axis", default="T1", choices=("T1", "T2"), help="Observed axis to evaluate")
    parser.add_argument("--pins", nargs="*", type=int, default=list(DEFAULT_PINS), help="Candidate GPIO numbers to test")
    parser.add_argument("--vision-duration-s", type=float, default=18.0, help="Vision capture duration per candidate")
    parser.add_argument("--warmup-s", type=float, default=1.0, help="Camera warmup before each candidate run")
    parser.add_argument("--result-timeout-s", type=float, default=22.0, help="Timeout for reading each candidate result")
    parser.add_argument("--min-travel-deg", type=float, default=270.0, help="Minimum optical travel span required")
    parser.add_argument("--max-end-span-deg", type=float, default=4.0, help="Maximum allowed final settle spread")
    parser.add_argument("--max-center-error-deg", type=float, default=8.0, help="Maximum allowed final midpoint error")
    parser.add_argument("--upload", action="store_true", help="Upload firmware before scanning")
    return parser


def run_candidate(args, diag_pin: int):
    cmd = [
        sys.executable,
        str(HIL_DIR / "verify_pio_homing.py"),
        "--device",
        args.device,
        "--axis",
        args.axis,
        "--diag-pin",
        str(diag_pin),
        "--vision-duration-s",
        str(args.vision_duration_s),
        "--warmup-s",
        str(args.warmup_s),
        "--result-timeout-s",
        str(args.result_timeout_s),
        "--min-travel-deg",
        str(args.min_travel_deg),
        "--max-end-span-deg",
        str(args.max_end_span_deg),
        "--max-center-error-deg",
        str(args.max_center_error_deg),
    ]
    if args.upload:
        cmd.append("--upload")

    proc = subprocess.run(cmd, cwd=str(HIL_DIR), text=True, capture_output=True)
    return {
        "diag_pin": int(diag_pin),
        "returncode": int(proc.returncode),
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }


def extract_summary(stdout: str):
    summary = {}
    for line in stdout.splitlines():
        if line.startswith("[INFO] Vision log: "):
            summary["vision_log"] = line.split(": ", 1)[1]
        elif line.startswith("[INFO] Firmware result: "):
            summary["firmware_result"] = line.split(": ", 1)[1]
        elif line.startswith("[INFO] Firmware success: "):
            summary["firmware_summary"] = line[len("[INFO] "):]
        elif line.startswith("[INFO] Vision summary: "):
            summary["vision_summary"] = line[len("[INFO] "):]
        elif line.startswith("[PASS] ") or line.startswith("[FAIL] "):
            summary["verdict"] = line
    return summary


def main() -> int:
    parser = build_argument_parser()
    args = parser.parse_args()

    results = []
    winning = None

    for diag_pin in args.pins:
        result = run_candidate(args, diag_pin)
        result["summary"] = extract_summary(result["stdout"])
        results.append(result)

        print("=" * 72)
        print("DIAG candidate GP{}".format(diag_pin))
        if result["summary"].get("firmware_summary"):
            print(result["summary"]["firmware_summary"])
        if result["summary"].get("vision_summary"):
            print(result["summary"]["vision_summary"])
        if result["summary"].get("verdict"):
            print(result["summary"]["verdict"])
        elif result["stderr"]:
            print(result["stderr"].strip())
        else:
            print("No verifier summary produced")

        if result["returncode"] == 0 and winning is None:
            winning = diag_pin

    print("=" * 72)
    if winning is not None:
        print("Winning DIAG pin: GP{}".format(winning))
    else:
        print("No DIAG pin in the tested range produced a valid full-span homing pass")

    report = {
        "winning_diag_pin": winning,
        "results": results,
    }
    print(json.dumps(report, indent=2))
    return 0 if winning is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
