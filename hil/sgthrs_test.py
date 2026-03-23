#!/usr/bin/env python3
"""Test SGTHRS sensitivity: vary stallguard threshold, observe DIAG false positives."""

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

DEFAULT_DEVICE = '/dev/ttyACM0'
TCP_PORT = 9999
SETTLE_S = 22
PROBE_S = 15
CYCLES_PER_VALUE = 3  # multiple homing cycles per SGTHRS value

SGTHRS_VALUES = [0, 1, 2, 3, 4, 5, 6, 8, 10, 15, 20]

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


def patch_sgthrs(original, sgthrs):
    return re.sub(r'HOME_SGTHRS\s*=\s*\d+', f'HOME_SGTHRS = {sgthrs}', original)


def deploy(device):
    cmds = [
        ['mpremote', 'connect', device, 'exec',
         "import os; [os.remove(f) for f in os.listdir() if f not in ('boot.py',)]"],
        ['mpremote', 'connect', device, 'fs', 'cp',
         'config.py', 'dmx_receiver.py', 'pio_stepper.py',
         'tmc2209_uart.py', 'tmc2209.py', 'main.py', ':'],
        ['mpremote', 'connect', device, 'reset'],
    ]
    for cmd in cmds:
        r = subprocess.run(cmd, cwd=FIRMWARE_DIR, capture_output=True, text=True)
        if r.returncode != 0:
            print(f'  [warn] mpremote failed: {r.stderr.strip()}')
            return False
    return True


def read_homing_result(device):
    try:
        r = subprocess.run(
            ['mpremote', 'connect', device, 'fs', 'cat', 'homing_result.json'],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return None
        return json.loads(r.stdout)
    except Exception:
        return None


def trigger_reset(device):
    """Reset the Pico to force a new homing cycle."""
    subprocess.run(
        ['mpremote', 'connect', device, 'reset'],
        capture_output=True, text=True, timeout=10,
    )


def probe_valid_points():
    try:
        r = subprocess.run(
            ['timeout', str(PROBE_S), 'nc', 'localhost', str(TCP_PORT)],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=PROBE_S + 5,
        )
        count = 0
        for line in r.stdout.decode('utf-8', errors='replace').split('\n'):
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
    except Exception:
        return 0


def extract_diag_data(result):
    """Extract DIAG metrics from homing_result.json."""
    if not result or not result.get('trials'):
        return None
    trial = result['trials'][0]
    fe = trial.get('first_end')
    if not fe:
        return None
    return {
        'success': fe.get('success', False),
        'stop_reason': fe.get('stop_reason', '?'),
        'search_steps': fe.get('search_steps', 0),
        'search_elapsed_ms': fe.get('search_elapsed_ms', 0),
        'sgthrs': fe.get('sgthrs', 0),
        'diag_triggers': fe.get('diag_triggers', 0),
        'diag_first_trigger_steps': fe.get('diag_first_trigger_steps'),
        'uart_threshold': fe.get('uart_threshold'),
        'startup_sg_values': fe.get('startup_sg_values', []),
        'sg_history': fe.get('sg_history', []),
    }


def main():
    device = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DEVICE
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    run_dir = os.path.join(CAPTURES_DIR, f'sgthrs_test_{timestamp}')
    os.makedirs(run_dir, exist_ok=True)

    print(f'=== SGTHRS Sensitivity Test (Speed 500 fixed) ===')
    print(f'Values:  {SGTHRS_VALUES}')
    print(f'Cycles:  {CYCLES_PER_VALUE} per value')
    print(f'Output:  {run_dir}')
    est_min = len(SGTHRS_VALUES) * CYCLES_PER_VALUE * (SETTLE_S + PROBE_S + 5) // 60
    print(f'Est time: ~{est_min} min')
    print()

    global _original_config
    _original_config = read_config()
    atexit.register(restore_config)

    all_results = {}
    try:
        for i, sgthrs in enumerate(SGTHRS_VALUES):
            print(f'[{i+1}/{len(SGTHRS_VALUES)}] SGTHRS = {sgthrs} (DIAG triggers at SG_RESULT < {sgthrs * 2})')

            patched = patch_sgthrs(_original_config, sgthrs)
            write_config(patched)

            print(f'  Deploying...')
            if not deploy(device):
                print(f'  [SKIP] Deploy failed\n')
                continue

            cycles = []
            for c in range(CYCLES_PER_VALUE):
                if c > 0:
                    print(f'  Cycle {c+1}: resetting...')
                    trigger_reset(device)

                print(f'  Cycle {c+1}: waiting {SETTLE_S}s for homing...')
                time.sleep(SETTLE_S)

                # Read homing result
                result = read_homing_result(device)
                diag = extract_diag_data(result)
                if diag is None:
                    print(f'  Cycle {c+1}: failed to read homing result')
                    continue

                if not diag['success']:
                    print(f'  Cycle {c+1}: homing failed ({diag["stop_reason"]})')
                    cycles.append(diag)
                    continue

                # Probe for position check
                valid = probe_valid_points()
                diag['probe_valid_points'] = valid

                delta = None
                if diag['diag_first_trigger_steps'] is not None:
                    delta = diag['search_steps'] - diag['diag_first_trigger_steps']

                false_pos = diag['diag_triggers'] - (1 if delta is not None and delta < 50 else 0)

                print(f'  Cycle {c+1}: steps={diag["search_steps"]} '
                      f'diag_triggers={diag["diag_triggers"]} '
                      f'diag_first={diag["diag_first_trigger_steps"]} '
                      f'delta={delta} '
                      f'probe={valid}pts')

                cycles.append(diag)

            all_results[sgthrs] = cycles
            print()

    finally:
        restore_config()

    # Save raw data
    with open(os.path.join(run_dir, 'raw_results.json'), 'w') as f:
        json.dump(all_results, f, indent=2, default=str)

    # Summary
    print('\n=== Results ===')
    print(f'{"SGTHRS":>6} {"Thresh":>6} {"Cycles":>6} {"AvgTrig":>8} '
          f'{"AvgFirst":>10} {"AvgStop":>10} {"AvgDelta":>10} {"Beam":>5}')
    print('-' * 75)

    for sgthrs in SGTHRS_VALUES:
        cycles = all_results.get(sgthrs, [])
        ok = [c for c in cycles if c.get('success')]
        if not ok:
            print(f'{sgthrs:>6} {sgthrs*2:>6} {"0":>6}     --- no successful cycles ---')
            continue

        avg_trig = sum(c['diag_triggers'] for c in ok) / len(ok)
        firsts = [c['diag_first_trigger_steps'] for c in ok if c['diag_first_trigger_steps'] is not None]
        avg_first = sum(firsts) / len(firsts) if firsts else 0
        avg_stop = sum(c['search_steps'] for c in ok) / len(ok)
        avg_delta = avg_stop - avg_first if firsts else 0
        beam_ok = sum(1 for c in ok if c.get('probe_valid_points', 0) > 5)

        print(f'{sgthrs:>6} {sgthrs*2:>6} {len(ok):>6} {avg_trig:>8.1f} '
              f'{avg_first:>10.0f} {avg_stop:>10.0f} {avg_delta:>10.0f} '
              f'{beam_ok}/{len(ok):>3}')

    print(f'\nFull data: {os.path.join(run_dir, "raw_results.json")}')


if __name__ == '__main__':
    main()
