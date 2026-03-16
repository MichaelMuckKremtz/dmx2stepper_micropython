#!/usr/bin/env python3
"""Send direct or scripted DMX changes through OLA and log the transmitted values."""

from __future__ import annotations

import argparse
import array
import csv
import dataclasses
import datetime as dt
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional


DMX_CHANNELS = 512
DEFAULT_FPS = 40.0
DEFAULT_CAPTURE_DIR = Path(__file__).resolve().parent / "captures"


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
            moved = distance * (elapsed / self.fade_s)
        else:
            ramp = min(self.ramp_s, self.fade_s / 2.0)
            if self.fade_s <= ramp:
                moved = distance * (elapsed / self.fade_s)
            else:
                v_const = distance / (self.fade_s - ramp)
                if elapsed < ramp:
                    fraction = elapsed / ramp
                    moved = v_const * ramp * ((fraction ** 3) - 0.5 * (fraction ** 4))
                elif elapsed <= (self.fade_s - ramp):
                    moved = 0.5 * v_const * ramp + v_const * (elapsed - ramp)
                else:
                    fraction = (elapsed - (self.fade_s - ramp)) / ramp
                    moved_pre = distance - (0.5 * v_const * ramp)
                    moved = moved_pre + v_const * ramp * (fraction - (fraction ** 3) + 0.5 * (fraction ** 4))

        moved = min(moved, distance)
        return self.start_value + direction * moved

    def done(self, now: float) -> bool:
        return (now - self.start_time) >= self.fade_s


@dataclasses.dataclass
class ScheduledCommand:
    offset_s: float
    channel: int
    target: int
    fade_s: float
    ramp_s: float
    command_id: str
    scenario: str


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
        self._pending = None
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
        csv_payload = ",".join(str(value) for value in data)
        self._proc.stdin.write(csv_payload + "\n")
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


def timestamped_path(output_dir: Path, prefix: str) -> Path:
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    return output_dir / f"{prefix}_{stamp}.csv"


def parse_channel_assignment(text: str):
    if "=" not in text:
        raise ValueError(f"Invalid channel assignment: {text}")
    left, right = text.split("=", 1)
    channel = int(left)
    value = int(right)
    if not (1 <= channel <= DMX_CHANNELS):
        raise ValueError("Channel must be in range 1..512")
    if not (0 <= value <= 255):
        raise ValueError("Value must be in range 0..255")
    return channel, value


def load_scenario(path: Path) -> List[ScheduledCommand]:
    rows = []
    with path.open(newline="") as handle:
        reader = csv.DictReader(line for line in handle if not line.lstrip().startswith("#"))
        for index, row in enumerate(reader, start=1):
            offset_s = float(row["offset_s"])
            channel = int(row["channel"])
            target = int(row["target"])
            fade_s = float(row.get("fade_s", 0.0) or 0.0)
            ramp_s = float(row.get("ramp_s", 0.0) or 0.0)
            if not (1 <= channel <= DMX_CHANNELS):
                raise ValueError(f"Scenario channel out of range on row {index}: {channel}")
            if not (0 <= target <= 255):
                raise ValueError(f"Scenario target out of range on row {index}: {target}")
            if fade_s < 0:
                raise ValueError(f"Scenario fade must be >= 0 on row {index}")
            if ramp_s < 0:
                raise ValueError(f"Scenario ramp must be >= 0 on row {index}")
            command_id = row.get("command_id") or f"cmd{index:03d}"
            scenario = row.get("scenario") or path.stem
            rows.append(
                ScheduledCommand(
                    offset_s=offset_s,
                    channel=channel,
                    target=target,
                    fade_s=fade_s,
                    ramp_s=ramp_s,
                    command_id=command_id,
                    scenario=scenario,
                )
            )
    return sorted(rows, key=lambda item: item.offset_s)


def write_log_rows(writer, command_label: str, rows: Iterable[tuple[int, int, str]], now_monotonic: float) -> None:
    now_wall = dt.datetime.now().isoformat(timespec="milliseconds")
    for channel, value, command_id in rows:
        writer.writerow(
            {
                "t_monotonic": f"{now_monotonic:.6f}",
                "t_wall": now_wall,
                "scenario": command_label,
                "channel": channel,
                "value": value,
                "command_id": command_id,
            }
        )


def send_payload(backend: Backend, dmx_values: List[int], max_channel: int) -> None:
    payload = bytes(dmx_values[: max(max_channel, 1)])
    backend.send(payload)


def run_set_command(args, backend: Backend, writer) -> None:
    dmx_values = [0] * DMX_CHANNELS
    changed = []
    max_channel = 0
    for assignment in args.value:
        channel, value = parse_channel_assignment(assignment)
        dmx_values[channel - 1] = value
        changed.append((channel, value, "set"))
        max_channel = max(max_channel, channel)

    now = time.monotonic()
    send_payload(backend, dmx_values, max_channel)
    write_log_rows(writer, args.scenario_name, changed, now)
    if args.hold_s > 0:
        time.sleep(args.hold_s)


def run_timeline(commands: List[ScheduledCommand], args, backend: Backend, writer) -> None:
    dmx_values = [0] * DMX_CHANNELS
    active: Dict[int, tuple[Transition, str, str]] = {}
    pending = list(commands)
    start_time = time.monotonic()
    next_tick = start_time

    while pending or active:
        now = time.monotonic()

        send_rows = []
        while pending and (now - start_time) >= pending[0].offset_s:
            command = pending.pop(0)
            current_value = dmx_values[command.channel - 1]
            if command.channel in active:
                current_value = clamp_int(active[command.channel][0].value_at(now), 0, 255)

            if command.fade_s <= 0:
                dmx_values[command.channel - 1] = command.target
                send_rows.append((command.channel, command.target, command.command_id))
                active.pop(command.channel, None)
            else:
                transition = Transition(
                    channel=command.channel,
                    start_value=float(current_value),
                    target_value=float(command.target),
                    fade_s=command.fade_s,
                    ramp_s=min(command.ramp_s, command.fade_s),
                    start_time=now,
                )
                active[command.channel] = (transition, command.command_id, command.scenario)

        if now >= next_tick or send_rows:
            max_channel = 0
            label = args.scenario_name

            for channel, (transition, command_id, scenario) in list(active.items()):
                label = scenario
                value = clamp_int(transition.value_at(now), 0, 255)
                dmx_values[channel - 1] = value
                send_rows.append((channel, value, command_id))
                max_channel = max(max_channel, channel)
                if transition.done(now):
                    active.pop(channel, None)

            if send_rows:
                for channel, _, _ in send_rows:
                    max_channel = max(max_channel, channel)
                send_payload(backend, dmx_values, max_channel)
                write_log_rows(writer, label, send_rows, now)

            next_tick += 1.0 / args.fps
        else:
            time.sleep(min(0.01, max(0.0, next_tick - now)))


def build_argument_parser():
    parser = argparse.ArgumentParser(description="OLA-backed DMX stimulus sender with CSV logging")
    parser.add_argument("--universe", type=int, default=1, help="OLA universe")
    parser.add_argument("--backend", choices=("auto", "bindings", "cli"), default="auto", help="OLA backend")
    parser.add_argument("--sender-bin", default="ola_streaming_client", help="Path to ola_streaming_client")
    parser.add_argument("--fps", type=float, default=DEFAULT_FPS, help="Frame rate for fades and scenarios")
    parser.add_argument("--output", help="Explicit output CSV path")
    parser.add_argument("--output-dir", default=str(DEFAULT_CAPTURE_DIR), help="Directory for timestamped output CSV")
    parser.add_argument("--prefix", default="dmx", help="Filename prefix when --output is not set")
    parser.add_argument("--scenario-name", default="manual", help="Scenario label written to the CSV")

    subparsers = parser.add_subparsers(dest="command", required=True)

    set_parser = subparsers.add_parser("set", help="Send one immediate set of channel values")
    set_parser.add_argument("--value", action="append", required=True, help="Assignment in the form channel=value")
    set_parser.add_argument("--hold-s", type=float, default=0.5, help="Hold time before exit")

    fade_parser = subparsers.add_parser("fade", help="Run one channel fade")
    fade_parser.add_argument("--channel", type=int, required=True)
    fade_parser.add_argument("--target", type=int, required=True)
    fade_parser.add_argument("--fade-s", type=float, required=True)
    fade_parser.add_argument("--ramp-s", type=float, default=0.0)

    scenario_parser = subparsers.add_parser("scenario", help="Run a scripted scenario from CSV")
    scenario_parser.add_argument("--path", required=True, help="Scenario CSV path")

    return parser


def main() -> int:
    parser = build_argument_parser()
    args = parser.parse_args()

    output_path = Path(args.output) if args.output else timestamped_path(Path(args.output_dir), args.prefix)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    backend = build_backend(args.backend, args.universe, args.sender_bin)
    try:
        with output_path.open("w", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=["t_monotonic", "t_wall", "scenario", "channel", "value", "command_id"],
            )
            writer.writeheader()

            if args.command == "set":
                run_set_command(args, backend, writer)
            elif args.command == "fade":
                if not (1 <= args.channel <= DMX_CHANNELS):
                    raise ValueError("Fade channel must be in range 1..512")
                if not (0 <= args.target <= 255):
                    raise ValueError("Fade target must be in range 0..255")
                if args.fade_s < 0 or args.ramp_s < 0:
                    raise ValueError("Fade and ramp values must be >= 0")
                commands = [
                    ScheduledCommand(
                        offset_s=0.0,
                        channel=args.channel,
                        target=args.target,
                        fade_s=args.fade_s,
                        ramp_s=args.ramp_s,
                        command_id="fade001",
                        scenario=args.scenario_name,
                    )
                ]
                run_timeline(commands, args, backend, writer)
            elif args.command == "scenario":
                commands = load_scenario(Path(args.path))
                if commands:
                    args.scenario_name = commands[0].scenario
                run_timeline(commands, args, backend, writer)

        print(f"[INFO] DMX stimulus log written to {output_path}")
        return 0
    finally:
        backend.close()


if __name__ == "__main__":
    raise SystemExit(main())
