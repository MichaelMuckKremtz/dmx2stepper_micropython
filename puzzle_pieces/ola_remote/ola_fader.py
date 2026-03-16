#!/usr/bin/env python3
"""
Interactive DMX fader for OLA.

Command format:
  ch1 at 255 fade 10 ramp 1
"""

from __future__ import annotations

import argparse
import array
import dataclasses
import queue
import re
import subprocess
import sys
import threading
import time
from typing import Dict, Optional


DMX_CHANNELS = 512
DEFAULT_FPS = 40.0
COMMAND_RE = re.compile(
    r"^\s*ch(?P<channel>\d{1,3})\s+at\s+(?P<target>\d{1,3})\s+fade\s+"
    r"(?P<fade>\d+(?:\.\d+)?)\s+ramp\s+(?P<ramp>\d+(?:\.\d+)?)\s*$",
    re.IGNORECASE,
)


def clamp_int(value: float, low: int, high: int) -> int:
    return max(low, min(high, int(round(value))))


@dataclasses.dataclass
class Transition:
    channel: int
    start_value: float
    target_value: float
    fade_s: float
    ramp_s: float
    start_time: float

    def value_at(self, now: float) -> float:
        elapsed = max(0.0, now - self.start_time)
        if elapsed >= self.fade_s:
            return self.target_value

        delta = self.target_value - self.start_value
        distance = abs(delta)
        if distance == 0.0:
            return self.target_value

        direction = 1.0 if delta >= 0 else -1.0

        if self.ramp_s <= 0:
            progress = elapsed / self.fade_s
            moved = distance * progress
        else:
            # Symmetric start/end ramp while preserving total fade duration.
            ramp = min(self.ramp_s, self.fade_s / 2.0)
            v_const = distance / (self.fade_s - ramp)
            if elapsed < ramp:
                # Start acceleration ramp (S-curve in velocity).
                u = elapsed / ramp
                moved = v_const * ramp * ((u ** 3) - 0.5 * (u ** 4))
            elif elapsed <= (self.fade_s - ramp):
                # Constant-speed middle section.
                moved = 0.5 * v_const * ramp + v_const * (elapsed - ramp)
            else:
                # End deceleration ramp (mirrors start ramp).
                q = (elapsed - (self.fade_s - ramp)) / ramp
                moved_pre = distance - (0.5 * v_const * ramp)
                moved = moved_pre + v_const * ramp * (q - (q ** 3) + 0.5 * (q ** 4))

        moved = min(moved, distance)
        return self.start_value + direction * moved

    def done(self, now: float) -> bool:
        return (now - self.start_time) >= self.fade_s


class Backend:
    def send(self, data: bytes) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    @property
    def name(self) -> str:
        raise NotImplementedError


class BindingsBackend(Backend):
    def __init__(self, universe: int):
        self._universe = universe
        self._lock = threading.Lock()
        self._pending: Optional[tuple[bytes, threading.Event, dict]] = None
        self._closing = False
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run_wrapper, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=2.0):
            raise RuntimeError("OLA bindings loop failed to initialize")

    @property
    def name(self) -> str:
        return "bindings"

    def _run_wrapper(self) -> None:
        from ola.ClientWrapper import ClientWrapper  # type: ignore

        wrapper = ClientWrapper()
        client = wrapper.Client()

        def pump() -> None:
            pending = None
            closing = False
            with self._lock:
                pending = self._pending
                self._pending = None
                closing = self._closing
            if pending is not None:
                data, done_evt, state_box = pending

                def sent_cb(state) -> None:
                    state_box["ok"] = bool(state.Succeeded())
                    done_evt.set()

                client.SendDmx(self._universe, array.array("B", data), sent_cb)
            if closing:
                wrapper.Stop()
                return
            wrapper.AddEvent(1, pump)

        wrapper.AddEvent(1, pump)
        self._ready.set()
        wrapper.Run()

    def send(self, data: bytes) -> None:
        done_evt = threading.Event()
        state_box = {"ok": False}
        with self._lock:
            self._pending = (bytes(data), done_evt, state_box)
        if not done_evt.wait(timeout=1.0):
            raise RuntimeError("OLA SendDmx timeout")
        if not state_box["ok"]:
            raise RuntimeError("OLA SendDmx failed")

    def close(self) -> None:
        with self._lock:
            self._closing = True
        self._thread.join(timeout=1.0)


class CLIBackend(Backend):
    def __init__(self, universe: int, sender_bin: str):
        self._proc = subprocess.Popen(
            [sender_bin, "-u", str(universe)],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

    @property
    def name(self) -> str:
        return "cli"

    def send(self, data: bytes) -> None:
        if self._proc.poll() is not None:
            stderr = self._proc.stderr.read() if self._proc.stderr else ""
            raise RuntimeError(
                f"ola_streaming_client exited with code {self._proc.returncode}: {stderr.strip()}"
            )
        if self._proc.stdin is None:
            raise RuntimeError("ola_streaming_client stdin unavailable")
        csv = ",".join(str(v) for v in data)
        self._proc.stdin.write(csv + "\n")
        self._proc.stdin.flush()

    def close(self) -> None:
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                self._proc.kill()


def build_backend(mode: str, universe: int, sender_bin: str) -> Backend:
    if mode in ("auto", "bindings"):
        try:
            return BindingsBackend(universe)
        except Exception as exc:
            if mode == "bindings":
                raise RuntimeError(f"Failed to initialize bindings backend: {exc}") from exc
            print(f"[WARN] Bindings unavailable ({exc}); using CLI backend", file=sys.stderr)
    return CLIBackend(universe, sender_bin)


def parse_transition(line: str) -> Transition:
    m = COMMAND_RE.match(line)
    if not m:
        raise ValueError("Invalid format. Example: ch1 at 255 fade 10 ramp 1")

    channel = int(m.group("channel"))
    target = int(m.group("target"))
    fade_s = float(m.group("fade"))
    ramp_s = float(m.group("ramp"))

    if not (1 <= channel <= DMX_CHANNELS):
        raise ValueError("Channel must be in range 1..512")
    if not (0 <= target <= 255):
        raise ValueError("Target value must be in range 0..255")
    if fade_s <= 0:
        raise ValueError("Fade must be > 0")
    if ramp_s < 0:
        raise ValueError("Ramp must be >= 0")

    return Transition(
        channel=channel,
        start_value=0.0,
        target_value=float(target),
        fade_s=fade_s,
        ramp_s=min(ramp_s, fade_s),
        start_time=0.0,
    )


def input_worker(cmd_q: "queue.Queue[str]", stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        try:
            line = input("> ")
        except EOFError:
            cmd_q.put("quit")
            break
        except KeyboardInterrupt:
            cmd_q.put("quit")
            break
        cmd_q.put(line.strip())


def progress_line(transitions: Dict[int, Transition], now: float) -> str:
    # Show a compact, single-line progress preview for up to 3 channels.
    items = []
    for ch in sorted(transitions.keys())[:3]:
        tr = transitions[ch]
        pct = min(1.0, max(0.0, (now - tr.start_time) / tr.fade_s))
        filled = int(round(pct * 10))
        bar = "#" * filled + "-" * (10 - filled)
        items.append(f"ch{ch}[{bar}] {int(round(pct * 100)):3d}%")
    suffix = " ..." if len(transitions) > 3 else ""
    return " | ".join(items) + suffix


def run(args: argparse.Namespace) -> int:
    cmd_q: "queue.Queue[str]" = queue.Queue()
    stop_event = threading.Event()
    dmx_values = [0] * DMX_CHANNELS
    transitions: Dict[int, Transition] = {}
    lock = threading.Lock()
    max_touched = 1

    backend = build_backend(args.backend, args.universe, args.sender_bin)
    print(f"[INFO] Backend: {backend.name} | Universe: {args.universe} | FPS: {args.fps}")
    print("[INFO] Commands: chN at V fade S ramp R | status | help | quit")

    in_thread = threading.Thread(target=input_worker, args=(cmd_q, stop_event), daemon=True)
    in_thread.start()

    tick_s = 1.0 / args.fps
    next_tick = time.monotonic()
    last_progress = 0.0
    progress_visible = False

    def handle_line(line: str) -> bool:
        nonlocal max_touched
        lc = line.lower()
        if not line:
            return True
        if lc in ("quit", "exit"):
            return False
        if lc == "help":
            print("Format: ch<1..512> at <0..255> fade <seconds> ramp <seconds>")
            print("Examples: ch1 at 255 fade 10 ramp 1")
            return True
        if lc == "status":
            with lock:
                active = sorted(transitions.keys())
                print(
                    f"[STATUS] active={len(active)} channels={active[:12]}"
                    + ("..." if len(active) > 12 else "")
                )
            return True
        try:
            parsed = parse_transition(line)
        except ValueError as exc:
            print(f"[ERROR] {exc}")
            return True

        now = time.monotonic()
        channel_idx = parsed.channel - 1
        with lock:
            current = (
                transitions[parsed.channel].value_at(now)
                if parsed.channel in transitions
                else float(dmx_values[channel_idx])
            )
            parsed.start_value = current
            parsed.start_time = now
            transitions[parsed.channel] = parsed
            max_touched = max(max_touched, parsed.channel)
        print(
            f"[ACCEPT] ch{parsed.channel} -> {int(parsed.target_value)} "
            f"fade={parsed.fade_s:.3f}s ramp={parsed.ramp_s:.3f}s from={current:.2f}"
        )
        return True

    try:
        running = True
        while running:
            # Drain pending commands.
            while True:
                try:
                    line = cmd_q.get_nowait()
                except queue.Empty:
                    break
                running = handle_line(line)
                if not running:
                    break

            now = time.monotonic()
            with lock:
                finished = []
                for ch, tr in transitions.items():
                    value = tr.value_at(now)
                    dmx_values[ch - 1] = clamp_int(value, 0, 255)
                    if tr.done(now):
                        dmx_values[ch - 1] = clamp_int(tr.target_value, 0, 255)
                        finished.append(ch)
                for ch in finished:
                    transitions.pop(ch, None)
                payload = bytes(dmx_values[: max_touched or 1])

            try:
                backend.send(payload)
            except Exception as exc:
                print(f"[ERROR] Backend send failed: {exc}")
                return 1

            if (now - last_progress) >= 0.1:
                with lock:
                    active_copy = dict(transitions)
                if active_copy:
                    line = progress_line(active_copy, now)
                    sys.stdout.write("\r[PROGRESS] " + line + " " * 8)
                    sys.stdout.flush()
                    progress_visible = True
                elif progress_visible:
                    sys.stdout.write("\r" + " " * 120 + "\r")
                    sys.stdout.flush()
                    progress_visible = False
                last_progress = now

            next_tick += tick_s
            sleep_s = next_tick - time.monotonic()
            if sleep_s > 0:
                time.sleep(sleep_s)
            else:
                next_tick = time.monotonic()
    except KeyboardInterrupt:
        print("\n[INFO] Keyboard interrupt, shutting down.")
    finally:
        stop_event.set()
        backend.close()
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Interactive OLA DMX fader")
    parser.add_argument("--universe", type=int, default=1, help="OLA universe id (default: 1)")
    parser.add_argument("--fps", type=float, default=DEFAULT_FPS, help="Send rate (default: 40)")
    parser.add_argument(
        "--backend",
        choices=("auto", "bindings", "cli"),
        default="auto",
        help="Sender backend (default: auto)",
    )
    parser.add_argument(
        "--sender-bin",
        default="ola_streaming_client",
        help="CLI sender binary name/path",
    )
    parser.add_argument(
        "--status-interval",
        type=float,
        default=0.0,
        help="Deprecated, ignored (kept for compatibility)",
    )
    return parser.parse_args(argv)


def main() -> int:
    args = parse_args(sys.argv[1:])
    if args.fps <= 0:
        print("[ERROR] --fps must be > 0", file=sys.stderr)
        return 2
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
