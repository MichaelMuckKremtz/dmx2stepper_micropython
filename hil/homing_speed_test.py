#!/usr/bin/env python3
"""Automated homing speed test: iterate over speeds, deploy, capture, analyze."""

import argparse
import atexit
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
FIRMWARE_DIR = os.path.join(os.path.dirname(SCRIPT_DIR), 'firmware')
CONFIG_PATH = os.path.join(FIRMWARE_DIR, 'config.py')
CAPTURES_DIR = os.path.join(SCRIPT_DIR, 'captures')
ANALYZE_SCRIPT = os.path.join(SCRIPT_DIR, 'analyze_x_data.py')

DEFAULT_SPEEDS = [100, 150, 200, 300, 400, 500, 600, 800]
DEFAULT_DURATION = 180
DEFAULT_DEVICE = '/dev/ttyACM0'
TCP_PORT = 9999
PROBE_DURATION_S = 15
PROBE_MIN_VALID_POINTS = 5

# Homing time estimate: 2 * MAX_STEPS_SCALED / speed_hz + overhead
# MAX_STEPS = 4800, microstep_adjustment = 8 -> 38400 steps
HOME_MAX_STEPS_SCALED = 38400
HOMING_OVERHEAD_S = 10

# Saved for restoration
_original_config = None


def read_config():
    with open(CONFIG_PATH, 'r') as f:
        return f.read()


def write_config(content):
    with open(CONFIG_PATH, 'w') as f:
        f.write(content)


def restore_config():
    global _original_config
    if _original_config is not None:
        write_config(_original_config)
        print('\n[restore] config.py restored to original state')
        _original_config = None


def patch_config(original, speed):
    """Patch config.py with the given homing speed value."""
    patched = re.sub(
        r'HOME_SPEED_TRIALS_1_8_HZ\s*=\s*\([^)]*\)',
        f'HOME_SPEED_TRIALS_1_8_HZ = (\n    {speed},\n)',
        original,
    )
    patched = re.sub(
        r'HOME_MIN_FREQ_1_8_HZ\s*=\s*\d+',
        f'HOME_MIN_FREQ_1_8_HZ = {min(300, speed)}',
        patched,
    )
    patched = re.sub(
        r'HOME_MAX_FREQ_1_8_HZ\s*=\s*\d+',
        f'HOME_MAX_FREQ_1_8_HZ = {max(1200, speed)}',
        patched,
    )
    return patched


def estimate_homing_time(speed_base):
    """Estimate how long homing takes at a given base speed."""
    actual_hz = max(1, speed_base * 16)
    traverse_s = HOME_MAX_STEPS_SCALED / actual_hz
    return int(2 * traverse_s + HOMING_OVERHEAD_S)


def deploy_firmware(device, dry_run=False):
    """Deploy firmware to Pico via mpremote (non-interactive)."""
    cmds = [
        ['mpremote', 'connect', device, 'exec',
         "import os; [os.remove(f) for f in os.listdir() if f not in ('boot.py',)]"],
        ['mpremote', 'connect', device, 'fs', 'cp',
         'config.py', 'dmx_receiver.py', 'pio_stepper.py',
         'tmc2209_uart.py', 'tmc2209.py', 'main.py', ':'],
        ['mpremote', 'connect', device, 'reset'],
    ]
    for cmd in cmds:
        if dry_run:
            print(f'  [dry-run] {" ".join(cmd)}')
        else:
            result = subprocess.run(cmd, cwd=FIRMWARE_DIR, capture_output=True, text=True)
            if result.returncode != 0:
                print(f'  [warn] mpremote failed: {" ".join(cmd)}')
                print(f'  stderr: {result.stderr.strip()}')
                return False
    return True


def count_valid_points(raw_data):
    """Count non-null data points in capture output."""
    count = 0
    for line in raw_data.decode('utf-8', errors='replace').split('\n'):
        line = line.strip()
        if line and line != 'null' and ',' in line:
            try:
                parts = line.split(',')
                int(parts[0])
                float(parts[1])
                count += 1
            except (ValueError, IndexError):
                pass
    return count


def probe_capture(dry_run=False):
    """Do a short probe capture to verify beam is visible (homing worked)."""
    if dry_run:
        print(f'  [dry-run] Probe capture {PROBE_DURATION_S}s')
        return True

    try:
        result = subprocess.run(
            ['timeout', str(PROBE_DURATION_S), 'nc', 'localhost', str(TCP_PORT)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=PROBE_DURATION_S + 10,
        )
        valid = count_valid_points(result.stdout)
        print(f'  Probe: {valid} valid points in {PROBE_DURATION_S}s')
        return valid >= PROBE_MIN_VALID_POINTS
    except Exception as e:
        print(f'  Probe failed: {e}')
        return False


def run_capture(output_file, duration_s, dry_run=False):
    """Capture TCP stream from OpenCV streamer."""
    if dry_run:
        print(f'  [dry-run] nc localhost {TCP_PORT} for {duration_s}s -> {output_file}')
        return True

    try:
        result = subprocess.run(
            ['timeout', str(duration_s), 'nc', 'localhost', str(TCP_PORT)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=duration_s + 10,
        )
        with open(output_file, 'wb') as f:
            f.write(result.stdout)

        valid = count_valid_points(result.stdout)
        total = len(result.stdout.strip().split(b'\n')) if result.stdout.strip() else 0
        print(f'  Captured {total} total, {valid} valid data points')
        return valid > 0
    except (subprocess.TimeoutExpired, Exception) as e:
        print(f'  [warn] Capture failed: {e}')
        return False


def run_analysis(capture_file, output_png, dry_run=False):
    """Run analyze_x_data.py on a capture file."""
    if dry_run:
        print(f'  [dry-run] analyze_x_data.py {capture_file} -> {output_png}')
        return True

    try:
        subprocess.run(
            ['python3', ANALYZE_SCRIPT, capture_file, output_png],
            check=True,
            cwd=SCRIPT_DIR,
        )
        return True
    except Exception as e:
        print(f'  [warn] Analysis failed: {e}')
        return False


def main():
    parser = argparse.ArgumentParser(description='Automated homing speed accuracy test')
    parser.add_argument('--speeds', type=str, default=None,
                        help=f'Comma-separated speed values (default: {",".join(map(str, DEFAULT_SPEEDS))})')
    parser.add_argument('--duration', type=int, default=DEFAULT_DURATION,
                        help=f'Capture duration in seconds (default: {DEFAULT_DURATION})')
    parser.add_argument('--device', type=str, default=DEFAULT_DEVICE,
                        help=f'Pico device path (default: {DEFAULT_DEVICE})')
    parser.add_argument('--dry-run', action='store_true',
                        help='Show what would be done without executing')
    args = parser.parse_args()

    speeds = [int(s) for s in args.speeds.split(',')] if args.speeds else DEFAULT_SPEEDS

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    run_dir = os.path.join(CAPTURES_DIR, f'homing_speed_{timestamp}')

    # Estimate total time
    total_s = sum(estimate_homing_time(s) + PROBE_DURATION_S + args.duration + 5 for s in speeds)

    print(f'=== Homing Speed Accuracy Test ===')
    print(f'Speeds:   {speeds}')
    print(f'Duration: {args.duration}s capture per speed')
    print(f'Device:   {args.device}')
    print(f'Output:   {run_dir}')
    print(f'Estimated time: ~{total_s // 60} min')
    print(f'Homing check: {PROBE_DURATION_S}s probe capture (need {PROBE_MIN_VALID_POINTS}+ valid points)')
    print()

    global _original_config
    _original_config = read_config()
    atexit.register(restore_config)

    if not args.dry_run:
        os.makedirs(run_dir, exist_ok=True)

    results = []
    try:
        for i, speed in enumerate(speeds):
            actual_hz = speed * 16.0
            settle_s = estimate_homing_time(speed)
            print(f'[{i+1}/{len(speeds)}] Speed {speed} (1/8 Hz) = {actual_hz:.0f} Hz scaled')

            # 1. Patch config
            patched = patch_config(_original_config, speed)
            if args.dry_run:
                print(f'  [dry-run] Patch config: HOME_SPEED_TRIALS_1_8_HZ = ({speed},)')
            else:
                write_config(patched)

            # 2. Deploy
            print(f'  Deploying firmware...')
            if not deploy_firmware(args.device, dry_run=args.dry_run):
                print(f'  [SKIP] Deploy failed for speed {speed}\n')
                results.append({'speed': speed, 'success': False, 'reason': 'deploy_failed'})
                continue

            # 3. Wait for homing
            if not args.dry_run:
                print(f'  Waiting {settle_s}s for homing at {actual_hz:.0f} Hz...')
                time.sleep(settle_s)
            else:
                print(f'  [dry-run] Wait {settle_s}s for homing')

            # 4. Probe capture to verify homing succeeded (beam visible)
            print(f'  Verifying homing via probe capture...')
            if not probe_capture(dry_run=args.dry_run):
                print(f'  [SKIP] Beam not visible - homing likely failed for speed {speed}\n')
                speed_dir = os.path.join(run_dir, f'speed_{speed}')
                if not args.dry_run:
                    os.makedirs(speed_dir, exist_ok=True)
                    with open(os.path.join(speed_dir, 'FAILED.txt'), 'w') as f:
                        f.write(f'Homing probe failed: no valid points in {PROBE_DURATION_S}s\n')
                results.append({'speed': speed, 'success': False, 'reason': 'beam_not_visible'})
                continue

            # 5. Full capture
            speed_dir = os.path.join(run_dir, f'speed_{speed}')
            if not args.dry_run:
                os.makedirs(speed_dir, exist_ok=True)

            capture_file = os.path.join(speed_dir, 'capture.txt')
            print(f'  Capturing {args.duration}s...')
            if not run_capture(capture_file, args.duration, dry_run=args.dry_run):
                print(f'  [SKIP] Capture returned no valid data for speed {speed}\n')
                results.append({'speed': speed, 'success': False, 'reason': 'capture_no_data'})
                continue

            # 6. Analyze
            analysis_png = os.path.join(speed_dir, 'capture_analysis.png')
            print(f'  Analyzing...')
            run_analysis(capture_file, analysis_png, dry_run=args.dry_run)

            results.append({'speed': speed, 'success': True})
            print(f'  Done.\n')

    finally:
        restore_config()

    # Summary
    print('\n=== Test Complete ===')
    for r in results:
        status = 'OK' if r['success'] else f'FAIL ({r.get("reason", "?")})'
        print(f'  Speed {r["speed"]}: {status}')

    ok = sum(1 for r in results if r['success'])
    failed = [r for r in results if not r['success']]
    print(f'\n{ok}/{len(speeds)} speeds tested successfully')
    if failed:
        print(f'Failed speeds: {", ".join(str(r["speed"]) for r in failed)}')
    if ok > 0:
        print(f'\nRun comparison analysis:')
        print(f'  python3 {os.path.join(SCRIPT_DIR, "compare_homing_speeds.py")} {run_dir}')


if __name__ == '__main__':
    main()
