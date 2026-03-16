#!/usr/bin/env python3
"""
Capture clean DMX fade windows from dmx_in receiver output.
"""

from __future__ import annotations

import re
import subprocess
import threading
import time
from pathlib import Path
from typing import List, Tuple


ROOT = Path(__file__).resolve().parent
DEPLOY_SCRIPT = ROOT / "dmx_in" / "deploy.sh"
FADER_SCRIPT = ROOT / "ola_fader.py"
OUT_DIR = ROOT / "captures"

CH1_RE = re.compile(r"CH1:\s*(\d+)")


def terminate_process(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            proc.kill()


def reader_thread(proc: subprocess.Popen, sink: List[Tuple[float, str]], stop_evt: threading.Event) -> None:
    assert proc.stdout is not None
    while not stop_evt.is_set():
        line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                break
            continue
        sink.append((time.monotonic(), line.rstrip("\n")))


def wait_for_receiver_ready(lines: List[Tuple[float, str]], timeout_s: float = 20.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for _, line in lines[-80:]:
            if "Waiting for DMX signal" in line:
                return
        time.sleep(0.1)
    raise RuntimeError("Receiver did not become ready")


def send_command(proc: subprocess.Popen, text: str) -> float:
    assert proc.stdin is not None
    ts = time.monotonic()
    proc.stdin.write(text + "\n")
    proc.stdin.flush()
    return ts


def extract_ch1(lines: List[Tuple[float, str]], t0: float) -> List[Tuple[float, int]]:
    out: List[Tuple[float, int]] = []
    for t, line in lines:
        if t < t0:
            continue
        m = CH1_RE.search(line)
        if m:
            out.append((t, int(m.group(1))))
    return out


def trim_fade_window(points: List[Tuple[float, int]]) -> List[Tuple[float, int]]:
    if not points:
        return []
    # Start at first sample near zero.
    start = None
    for i, (_, v) in enumerate(points):
        if v <= 3:
            start = i
            break
    if start is None:
        start = 0

    # End at first target hit after start.
    end = len(points) - 1
    for i in range(start, len(points)):
        if points[i][1] >= 255:
            end = i
            break

    seg = points[start : end + 1]
    if not seg:
        return []

    t_base = seg[0][0]
    return [(t - t_base, v) for t, v in seg]


def run_capture(ramp: float, fade_s: float = 10.0) -> List[Tuple[float, int]]:
    receiver_lines: List[Tuple[float, str]] = []
    stop_reader = threading.Event()
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
    sender = None
    try:
        wait_for_receiver_ready(receiver_lines)
        sender = subprocess.Popen(
            ["python3", str(FADER_SCRIPT), "--universe", "1", "--backend", "auto", "--status-interval", "0"],
            cwd=str(ROOT),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        send_command(sender, "ch1 at 0 fade 0.4 ramp 0")
        time.sleep(0.8)
        cmd_time = send_command(sender, f"ch1 at 255 fade {fade_s:g} ramp {ramp:g}")

        deadline = time.monotonic() + fade_s + 6.0
        reached = False
        while time.monotonic() < deadline:
            pts = extract_ch1(receiver_lines, cmd_time)
            if pts and pts[-1][1] >= 255:
                reached = True
                break
            time.sleep(0.1)
        if not reached:
            raise RuntimeError(f"Target not reached for ramp={ramp:g}")

        time.sleep(0.3)
        points = extract_ch1(receiver_lines, cmd_time)
        cleaned = trim_fade_window(points)
        if len(cleaned) < 20:
            raise RuntimeError(f"Too few cleaned samples for ramp={ramp:g}: {len(cleaned)}")
        return cleaned
    finally:
        if sender is not None:
            try:
                send_command(sender, "quit")
            except Exception:
                pass
            terminate_process(sender)
        terminate_process(receiver)
        stop_reader.set()


def main() -> None:
    OUT_DIR.mkdir(exist_ok=True)
    for ramp in (0, 1, 3):
        clean = run_capture(float(ramp))
        log_path = OUT_DIR / f"ramp{ramp}_clean.csv"
        with log_path.open("w") as f:
            f.write("t_s,ch1\n")
            for t, v in clean:
                f.write(f"{t:.4f},{v}\n")
        print(f"[OK] ramp={ramp} samples={len(clean)} -> {log_path}")


if __name__ == "__main__":
    main()
