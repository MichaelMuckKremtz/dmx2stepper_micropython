#!/usr/bin/env python3
"""
Hardware-in-the-loop validator for ola_fader.py using dmx_in receiver output.
"""

from __future__ import annotations

import dataclasses
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import List, Tuple


ROOT = Path(__file__).resolve().parent
DMX_CONFIG = ROOT / "dmx_in" / "config.py"
DEPLOY_SCRIPT = ROOT / "dmx_in" / "deploy.sh"
FADER_SCRIPT = ROOT / "ola_fader.py"

CH1_RE = re.compile(r"CH1:\s*(\d+)")
NO_SIGNAL_RE = re.compile(r"NO SIGNAL")


@dataclasses.dataclass
class Sample:
    t: float
    value: int
    raw: str


class ValidationError(RuntimeError):
    pass


def require_command(cmd: str) -> None:
    if subprocess.run(["bash", "-lc", f"command -v {cmd}"], capture_output=True).returncode != 0:
        raise ValidationError(f"Required command not found: {cmd}")


def ensure_olad() -> None:
    if subprocess.run(["pgrep", "-x", "olad"], capture_output=True).returncode == 0:
        return
    subprocess.run(["olad"], check=False)
    for _ in range(20):
        if subprocess.run(["pgrep", "-x", "olad"], capture_output=True).returncode == 0:
            return
        time.sleep(0.1)
    raise ValidationError("olad is not running")


def patch_print_interval(new_value: float) -> str:
    old = DMX_CONFIG.read_text()
    new = re.sub(
        r"(?m)^PRINT_INTERVAL\s*=\s*.*$",
        f"PRINT_INTERVAL = {new_value}",
        old,
    )
    DMX_CONFIG.write_text(new)
    return old


def parse_samples(lines: List[Tuple[float, str]]) -> List[Sample]:
    out: List[Sample] = []
    for t, line in lines:
        m = CH1_RE.search(line)
        if m:
            out.append(Sample(t=t, value=int(m.group(1)), raw=line))
    return out


def assert_monotonic(samples: List[Sample], increasing: bool) -> None:
    if len(samples) < 5:
        raise ValidationError("Not enough samples to validate monotonic trend")
    violations = 0
    for i in range(1, len(samples)):
        if increasing and samples[i].value < samples[i - 1].value:
            violations += 1
        if not increasing and samples[i].value > samples[i - 1].value:
            violations += 1
    if violations > max(2, len(samples) // 20):
        raise ValidationError(f"Monotonic check failed, violations={violations}")


def assert_target_timing(
    samples: List[Sample],
    start_time: float,
    target: int,
    expected_s: float,
    tolerance_s: float,
) -> None:
    reached = None
    for s in samples:
        if s.value == target:
            reached = s.t
            break
    if reached is None:
        raise ValidationError(f"Target {target} never reached")
    actual = reached - start_time
    if abs(actual - expected_s) > tolerance_s:
        raise ValidationError(
            f"Target timing off: expected {expected_s:.2f}s +/- {tolerance_s:.2f}s, actual {actual:.2f}s"
        )


def assert_ramp_acceleration(samples: List[Sample], ramp_window_s: float) -> None:
    if len(samples) < 8:
        raise ValidationError("Not enough samples for ramp check")
    t0 = samples[0].t
    in_window = [s for s in samples if (s.t - t0) <= ramp_window_s]
    if len(in_window) < 6:
        raise ValidationError("Insufficient samples inside ramp window")
    deltas = [in_window[i].value - in_window[i - 1].value for i in range(1, len(in_window))]
    mid = max(2, len(deltas) // 2)
    first = sum(deltas[:mid]) / len(deltas[:mid])
    second = sum(deltas[mid:]) / len(deltas[mid:])
    if second <= first:
        raise ValidationError(
            f"Ramp acceleration check failed: early avg delta={first:.3f}, late avg delta={second:.3f}"
        )


def assert_end_deceleration(samples: List[Sample], ramp_window_s: float) -> None:
    if len(samples) < 8:
        raise ValidationError("Not enough samples for end-ramp check")

    # Trim trailing hold-at-target samples.
    trimmed = list(samples)
    while len(trimmed) > 3 and trimmed[-1].value == trimmed[-2].value:
        trimmed.pop()
    t_end = trimmed[-1].t
    in_window = [s for s in trimmed if (t_end - s.t) <= ramp_window_s]
    if len(in_window) < 6:
        raise ValidationError("Insufficient samples inside end-ramp window")
    deltas = [in_window[i].value - in_window[i - 1].value for i in range(1, len(in_window))]
    mid = max(2, len(deltas) // 2)
    first = sum(deltas[:mid]) / len(deltas[:mid])
    second = sum(deltas[mid:]) / len(deltas[mid:])
    if second >= first:
        raise ValidationError(
            f"End deceleration check failed: early avg delta={first:.3f}, late avg delta={second:.3f}"
        )


def reader_thread(proc: subprocess.Popen, sink: List[Tuple[float, str]], stop_evt: threading.Event) -> None:
    assert proc.stdout is not None
    while not stop_evt.is_set():
        line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                break
            continue
        sink.append((time.monotonic(), line.rstrip("\n")))


def wait_for_receiver_boot(lines: List[Tuple[float, str]], timeout_s: float = 20.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for _, line in lines[-50:]:
            if "Waiting for DMX signal" in line or "receiver initialized" in line:
                return
        time.sleep(0.1)
    raise ValidationError("Receiver did not initialize in time")


def send_command(proc: subprocess.Popen, text: str) -> float:
    assert proc.stdin is not None
    ts = time.monotonic()
    proc.stdin.write(text + "\n")
    proc.stdin.flush()
    return ts


def terminate_process(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            proc.kill()


def run_hardware_validation(original_config: str) -> None:
    receiver_lines: List[Tuple[float, str]] = []
    stop_reader = threading.Event()
    receiver = None
    sender = None

    try:
        receiver = subprocess.Popen(
            ["bash", str(DEPLOY_SCRIPT)],
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        t = threading.Thread(target=reader_thread, args=(receiver, receiver_lines, stop_reader), daemon=True)
        t.start()
        wait_for_receiver_boot(receiver_lines)

        sender = subprocess.Popen(
            [sys.executable, str(FADER_SCRIPT), "--universe", "1", "--backend", "auto", "--status-interval", "0"],
            cwd=str(ROOT),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        # Baseline reset before scenarios.
        send_command(sender, "ch1 at 0 fade 0.3 ramp 0")
        time.sleep(0.6)

        # Scenario 1: forward fade with ramp.
        start1 = send_command(sender, "ch1 at 255 fade 10 ramp 1")
        time.sleep(11.0)
        samples1 = [s for s in parse_samples(receiver_lines) if s.t >= start1]
        if len(samples1) < 5 or len({s.value for s in samples1}) <= 1:
            recent = "\n".join(line for _, line in receiver_lines[-30:])
            if NO_SIGNAL_RE.search(recent):
                raise ValidationError("No physical DMX input detected from receiver")
            raise ValidationError("Insufficient receiver samples for scenario 1")
        assert_monotonic(samples1, increasing=True)
        assert_ramp_acceleration(samples1, ramp_window_s=1.2)
        assert_end_deceleration(samples1, ramp_window_s=1.2)
        assert_target_timing(samples1, start1, target=255, expected_s=10.0, tolerance_s=0.6)
        print("[PASS] Scenario 1: forward ramp fade")

        # Scenario 2: reverse fade with ramp.
        start2 = send_command(sender, "ch1 at 0 fade 10 ramp 1")
        time.sleep(11.0)
        samples2 = [s for s in parse_samples(receiver_lines) if s.t >= start2]
        assert_monotonic(samples2, increasing=False)
        assert_target_timing(samples2, start2, target=0, expected_s=10.0, tolerance_s=0.6)
        print("[PASS] Scenario 2: reverse ramp fade")

        # Scenario 3: interrupt and retarget.
        start3a = send_command(sender, "ch1 at 255 fade 10 ramp 1")
        time.sleep(3.0)
        start3b = send_command(sender, "ch1 at 64 fade 2 ramp 0.5")
        time.sleep(3.0)
        samples3 = [s for s in parse_samples(receiver_lines) if s.t >= start3a]
        post_retarget = [s for s in samples3 if s.t >= start3b]
        if not post_retarget:
            raise ValidationError("No samples collected after interrupt command")
        assert_target_timing(post_retarget, start3b, target=64, expected_s=2.0, tolerance_s=0.5)
        print("[PASS] Scenario 3: interrupt/retarget")

        send_command(sender, "quit")
    finally:
        if sender is not None:
            terminate_process(sender)
        if receiver is not None:
            terminate_process(receiver)
        stop_reader.set()
        DMX_CONFIG.write_text(original_config)


def parse_ola_show(path: Path) -> List[Sample]:
    if not path.exists():
        raise ValidationError(f"Missing OLA recorder file: {path}")
    lines = [ln.strip() for ln in path.read_text().splitlines() if ln.strip()]
    if not lines or lines[0] != "OLA Show":
        raise ValidationError("Unexpected OLA recorder file format")

    samples: List[Sample] = []
    current_t = 0.0
    i = 1
    while i < len(lines):
        frame_line = lines[i]
        if " " not in frame_line:
            i += 1
            continue
        parts = frame_line.split(" ", 1)
        if len(parts) != 2:
            i += 1
            continue
        values = parts[1].split(",")
        ch1 = int(values[0]) if values and values[0] else 0
        samples.append(Sample(t=current_t, value=ch1, raw=frame_line))
        if i + 1 < len(lines):
            try:
                delay_ms = int(lines[i + 1])
            except ValueError:
                delay_ms = 0
            current_t += max(0, delay_ms) / 1000.0
            i += 2
        else:
            i += 1
    return samples


def run_recorder_scenario(command: str, duration_s: float, show_path: Path) -> List[Sample]:
    if show_path.exists():
        show_path.unlink()

    recorder = subprocess.Popen(
        ["ola_recorder", "-r", str(show_path), "-u", "1"],
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    sender = subprocess.Popen(
        [sys.executable, str(FADER_SCRIPT), "--universe", "1", "--backend", "auto", "--status-interval", "0"],
        cwd=str(ROOT),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    try:
        time.sleep(0.4)
        send_command(sender, command)
        time.sleep(duration_s + 0.5)
        send_command(sender, "quit")
        terminate_process(sender)
        time.sleep(0.4)
        terminate_process(recorder)
        return parse_ola_show(show_path)
    finally:
        terminate_process(sender)
        terminate_process(recorder)


def run_recorder_validation() -> None:
    require_command("ola_recorder")
    show1 = ROOT / ".tmp_ola_show_forward.txt"
    show2 = ROOT / ".tmp_ola_show_reverse.txt"
    show3 = ROOT / ".tmp_ola_show_interrupt.txt"

    samples1 = run_recorder_scenario("ch1 at 255 fade 10 ramp 1", 10.0, show1)
    assert_monotonic(samples1, increasing=True)
    assert_ramp_acceleration(samples1, ramp_window_s=1.2)
    assert_end_deceleration(samples1, ramp_window_s=1.2)
    assert_target_timing(samples1, start_time=0.0, target=255, expected_s=10.0, tolerance_s=0.8)
    print("[PASS] Recorder scenario 1: forward ramp fade")

    if show2.exists():
        show2.unlink()
    recorder = subprocess.Popen(
        ["ola_recorder", "-r", str(show2), "-u", "1"],
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    sender = subprocess.Popen(
        [sys.executable, str(FADER_SCRIPT), "--universe", "1", "--backend", "auto", "--status-interval", "0"],
        cwd=str(ROOT),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    try:
        time.sleep(0.5)
        send_command(sender, "ch1 at 255 fade 0.5 ramp 0")
        time.sleep(0.8)
        send_command(sender, "ch1 at 0 fade 10 ramp 1")
        time.sleep(10.6)
        send_command(sender, "quit")
        terminate_process(sender)
        time.sleep(0.4)
        terminate_process(recorder)
    finally:
        terminate_process(sender)
        terminate_process(recorder)

    samples2 = parse_ola_show(show2)
    down_samples = [s for s in samples2 if s.t >= 0.8]
    assert_monotonic(down_samples, increasing=False)
    assert_target_timing(down_samples, start_time=0.8, target=0, expected_s=10.0, tolerance_s=1.0)
    print("[PASS] Recorder scenario 2: reverse ramp fade")

    if show3.exists():
        show3.unlink()
    recorder = subprocess.Popen(
        ["ola_recorder", "-r", str(show3), "-u", "1"],
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    sender = subprocess.Popen(
        [sys.executable, str(FADER_SCRIPT), "--universe", "1", "--backend", "auto", "--status-interval", "0"],
        cwd=str(ROOT),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    try:
        time.sleep(0.5)
        send_command(sender, "ch1 at 255 fade 10 ramp 1")
        time.sleep(3.0)
        send_command(sender, "ch1 at 10 fade 2 ramp 0.5")
        time.sleep(3.0)
        send_command(sender, "quit")
        terminate_process(sender)
        time.sleep(0.4)
        terminate_process(recorder)
    finally:
        terminate_process(sender)
        terminate_process(recorder)

    samples3 = parse_ola_show(show3)
    post = [s for s in samples3 if s.t >= 3.0]
    assert_target_timing(post, start_time=3.0, target=10, expected_s=2.0, tolerance_s=0.7)
    print("[PASS] Recorder scenario 3: interrupt/retarget")

    for p in (show1, show2, show3):
        if p.exists():
            p.unlink()


def main() -> int:
    require_command("mpremote")
    require_command("ola_uni_info")
    ensure_olad()

    original_config = patch_print_interval(0.05)
    try:
        run_hardware_validation(original_config)
        print("[PASS] All hardware validation scenarios")
        return 0
    except ValidationError as exc:
        print(f"[WARN] Hardware validation failed: {exc}")
        print("[INFO] Falling back to OLA recorder validation.")
        try:
            run_recorder_validation()
            print("[PASS] All recorder validation scenarios")
            return 0
        except ValidationError as rec_exc:
            print(f"[FAIL] Recorder validation failed: {rec_exc}")
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
