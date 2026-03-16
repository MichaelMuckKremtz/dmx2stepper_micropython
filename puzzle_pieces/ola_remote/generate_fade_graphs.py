#!/usr/bin/env python3
"""
Generate PNG graphs to visualize fade/ramp styles for ola_fader.py.
"""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Iterable, List, Tuple

import matplotlib.pyplot as plt
import numpy as np
import matplotlib.image as mpimg


ROOT = Path(__file__).resolve().parent
GRAPH_DIR = ROOT / "graphs"
DMX_LOG = Path("/tmp/dmx_receiver_run.log")
CAPTURE_DIR = ROOT / "captures"

LINE_RE = re.compile(
    r"^\[(\d{2}):(\d{2}):(\d{2})\].*Frame:\s*(\d+)\s+\(([\d.]+)\s+fps\).*CH1:\s*(\d+)"
)


def dmx_value_at(elapsed: float, start: float, target: float, fade: float, ramp: float) -> float:
    if elapsed <= 0:
        return start
    if elapsed >= fade:
        return target

    delta = target - start
    distance = abs(delta)
    if distance == 0:
        return target
    direction = 1 if delta >= 0 else -1

    if ramp <= 0:
        moved = distance * (elapsed / fade)
    else:
        ramp = min(ramp, fade / 2.0)
        v_const = distance / (fade - ramp)
        if elapsed < ramp:
            u = elapsed / ramp
            moved = v_const * ramp * ((u ** 3) - 0.5 * (u ** 4))
        elif elapsed <= (fade - ramp):
            moved = 0.5 * v_const * ramp + v_const * (elapsed - ramp)
        else:
            q = (elapsed - (fade - ramp)) / ramp
            moved_pre = distance - (0.5 * v_const * ramp)
            moved = moved_pre + v_const * ramp * (q - (q ** 3) + 0.5 * (q ** 4))
    return start + direction * min(distance, moved)


def model_curve(fade: float, ramp: float, dt: float = 0.02) -> Tuple[np.ndarray, np.ndarray]:
    t = np.arange(0, fade + dt, dt)
    y = np.array([dmx_value_at(tt, 0, 255, fade, ramp) for tt in t], dtype=float)
    return t, y


def parse_measured(log_path: Path) -> Tuple[np.ndarray, np.ndarray]:
    if not log_path.exists():
        raise FileNotFoundError(f"Missing log file: {log_path}")
    points: List[Tuple[int, float, int]] = []
    for line in log_path.read_text().splitlines():
        m = LINE_RE.match(line.strip())
        if not m:
            continue
        _hh, _mm, _ss, frame, fps, ch1 = m.groups()
        points.append((int(frame), float(fps), int(ch1)))
    if len(points) < 4:
        raise ValueError("Not enough CH1 points found in measured log")

    frame0 = points[0][0]
    fps_med = float(np.median(np.array([p[1] for p in points], dtype=float)))
    if fps_med <= 1e-6:
        fps_med = 30.0
    t = np.array([(p[0] - frame0) / fps_med for p in points], dtype=float)
    y = np.array([p[2] for p in points], dtype=float)
    return t, y


def parse_measured_window(log_path: Path) -> Tuple[np.ndarray, np.ndarray]:
    t, y = parse_measured(log_path)
    # Find the strongest non-decreasing fade-up segment that starts near 0.
    best = None
    n = len(y)
    for start in range(n):
        if y[start] > 5:
            continue
        end = start
        max_y = y[start]
        while end + 1 < n:
            # Allow small quantization noise but stop on obvious reset/drop.
            if y[end + 1] < y[end] - 6:
                break
            end += 1
            max_y = max(max_y, y[end])
            if y[end] >= 250:
                break
        length = end - start + 1
        score = (max_y, length)
        if best is None or score > best[0]:
            best = (score, start, end)
    if best is None:
        return t, y
    _, start, end = best
    t2 = t[start : end + 1] - t[start]
    y2 = y[start : end + 1]
    return t2, y2


def plot_ramp_family() -> None:
    ramps = [0.0, 0.5, 1.0, 2.0, 4.0]
    fade = 10.0
    fig, ax = plt.subplots(figsize=(10, 5.5), dpi=140)
    for ramp in ramps:
        t, y = model_curve(fade=fade, ramp=ramp)
        ax.plot(t, y, linewidth=2.0, label=f"ramp={ramp:g}s")
    ax.set_title("Ramp Style Comparison (fade=10s, target=255)")
    ax.set_xlabel("Time (s)")
    ax.set_ylabel("DMX Value (CH1)")
    ax.set_xlim(0, fade)
    ax.set_ylim(0, 260)
    ax.grid(alpha=0.25)
    ax.legend()
    fig.tight_layout()
    fig.savefig(GRAPH_DIR / "ramp_style_fade10.png")
    plt.close(fig)


def plot_fade_family() -> None:
    fades = [2.0, 5.0, 10.0, 15.0]
    ramp = 1.0
    fig, ax = plt.subplots(figsize=(10, 5.5), dpi=140)
    for fade in fades:
        t, y = model_curve(fade=fade, ramp=ramp)
        ax.plot(t, y, linewidth=2.0, label=f"fade={fade:g}s")
    ax.set_title("Fade Time Comparison (ramp=1s, target=255)")
    ax.set_xlabel("Time (s)")
    ax.set_ylabel("DMX Value (CH1)")
    ax.set_xlim(0, max(fades))
    ax.set_ylim(0, 260)
    ax.grid(alpha=0.25)
    ax.legend()
    fig.tight_layout()
    fig.savefig(GRAPH_DIR / "fade_time_style_ramp1.png")
    plt.close(fig)


def plot_velocity_family() -> None:
    ramps = [0.0, 0.5, 1.0, 2.0, 4.0]
    fade = 10.0
    fig, ax = plt.subplots(figsize=(10, 5.5), dpi=140)
    for ramp in ramps:
        t, y = model_curve(fade=fade, ramp=ramp, dt=0.01)
        v = np.gradient(y, t)
        ax.plot(t, v, linewidth=2.0, label=f"ramp={ramp:g}s")
    ax.set_title("Velocity Shape Comparison (fade=10s)")
    ax.set_xlabel("Time (s)")
    ax.set_ylabel("Approx. Velocity (DMX/s)")
    ax.set_xlim(0, fade)
    ax.grid(alpha=0.25)
    ax.legend()
    fig.tight_layout()
    fig.savefig(GRAPH_DIR / "ramp_velocity_fade10.png")
    plt.close(fig)


def plot_measured_overlay() -> None:
    t_meas, y_meas = parse_measured(DMX_LOG)
    t_model, y_model = model_curve(fade=10.0, ramp=1.0, dt=0.05)

    # Align model start to first sample at/near zero.
    start_idx = int(np.argmin(np.abs(y_meas)))
    t_aligned = t_meas - t_meas[start_idx]

    fig, ax = plt.subplots(figsize=(10, 5.5), dpi=140)
    ax.plot(t_model, y_model, linewidth=2.2, label="Model: fade=10 ramp=1")
    ax.plot(t_aligned, y_meas, "o-", linewidth=1.8, markersize=4, label="Measured: dmx_in CH1")
    ax.set_title("Measured vs Model (dmx_in capture)")
    ax.set_xlabel("Time From Fade Start (s)")
    ax.set_ylabel("DMX Value (CH1)")
    ax.set_xlim(0, 12)
    ax.set_ylim(0, 260)
    ax.grid(alpha=0.25)
    ax.legend()
    fig.tight_layout()
    fig.savefig(GRAPH_DIR / "measured_vs_model_fade10_ramp1.png")
    plt.close(fig)


def plot_measured_ramp_comparison() -> None:
    csvs = {
        "ramp 0 (measured)": CAPTURE_DIR / "ramp0_clean.csv",
        "ramp 1 (measured)": CAPTURE_DIR / "ramp1_clean.csv",
        "ramp 3 (measured)": CAPTURE_DIR / "ramp3_clean.csv",
    }
    fig, ax = plt.subplots(figsize=(10, 5.5), dpi=140)
    plotted = 0
    for label, path in csvs.items():
        if not path.exists():
            continue
        try:
            data = np.loadtxt(path, delimiter=",", skiprows=1)
            if data.ndim == 1:
                data = data.reshape(1, -1)
            t = data[:, 0]
            y = data[:, 1]
        except Exception:
            continue
        if len(t) < 4:
            continue
        ax.plot(t, y, "o-", linewidth=2.0, markersize=4, label=label)
        plotted += 1

    if plotted == 0:
        plt.close(fig)
        return

    ax.set_title("Measured Ramp Comparison from dmx_in (fade=10s)")
    ax.set_xlabel("Time From Fade Start (s)")
    ax.set_ylabel("DMX Value (CH1)")
    ax.set_xlim(0, 12)
    ax.set_ylim(0, 260)
    ax.grid(alpha=0.25)
    ax.legend()
    fig.tight_layout()
    fig.savefig(GRAPH_DIR / "measured_ramp_comparison_dmxin.png")
    plt.close(fig)


def build_montage() -> None:
    order = [
        ("ramp_style_fade10.png", "Ramp Styles"),
        ("ramp_velocity_fade10.png", "Ramp Velocity"),
        ("fade_time_style_ramp1.png", "Fade Times"),
        ("measured_vs_model_fade10_ramp1.png", "Measured vs Model"),
        ("measured_ramp_comparison_dmxin.png", "Measured Ramp Compare"),
    ]
    existing = [(GRAPH_DIR / name, title) for name, title in order if (GRAPH_DIR / name).exists()]
    if not existing:
        return

    cols = 3
    rows = int(math.ceil(len(existing) / cols))
    fig, axes = plt.subplots(rows, cols, figsize=(16, 9), dpi=140)
    axes_arr = np.array(axes).reshape(rows, cols)

    for idx, (path, title) in enumerate(existing):
        r, c = divmod(idx, cols)
        ax = axes_arr[r, c]
        img = mpimg.imread(path)
        ax.imshow(img)
        ax.set_title(title, fontsize=10)
        ax.axis("off")

    for idx in range(len(existing), rows * cols):
        r, c = divmod(idx, cols)
        axes_arr[r, c].axis("off")

    fig.suptitle("OLA Fader Style Overview", fontsize=14)
    fig.tight_layout()
    fig.savefig(GRAPH_DIR / "fade_style_montage.png")
    plt.close(fig)


def main() -> None:
    GRAPH_DIR.mkdir(exist_ok=True)
    plot_ramp_family()
    plot_fade_family()
    plot_velocity_family()
    try:
        plot_measured_overlay()
    except Exception as exc:
        print(f"[WARN] Skipped measured overlay: {exc}")
    plot_measured_ramp_comparison()
    build_montage()
    print(f"[OK] Graphs saved to {GRAPH_DIR}")


if __name__ == "__main__":
    main()
