# DMX-to-Stepper Firmware

Raspberry Pi Pico (RP2040) running MicroPython. Controls a stepper motor via TMC2209 from DMX-512 input.

**Goal**: Reduce firmware clutter, simplify codebase, investigate and fix motion glitches (constant micro-jitter during fades, occasional larger oscillations during holds).

## Repository Structure

- `firmware/` — active slimmed firmware (development target)
- `firmware_v1/` — archived original (reference, do not modify)
- `hil/` — OpenCV hardware-in-loop vision system (Raspberry Pi side)

## Key Files

| File | Purpose |
|---|---|
| `firmware/main.py` | Runtime loop, `SharedDMXState`, `ChunkedPositionController` |
| `firmware/config.py` | Motor constants, chunk size, deadband |
| `firmware/dmx_receiver.py` | PIO DMX512 receiver (unchanged from v1) |
| `firmware/tmc2209.py` | TMC2209 driver wrapper (unchanged from v1) |
| `firmware/tmc2209_uart.py` | TMC2209 UART transport (unchanged from v1) |
| `firmware/pio_stepper.py` | PIO stepper pulse generation (unchanged from v1) |
| `firmware/deploy.sh` | Deploy script using `mpremote` |

## Lighting Desk Loop (~60s cycle)

CH8=255 (reset/homing) → DMX=0 (center) → DMX=558 (LEFT, 7s fade) → DMX=558 hold → DMX=6521 (RIGHT, 7s fade) → DMX=6521 hold → repeat

## X Reference (port 9999, OpenCV vision stream)

| Phase | X value |
|---|---|
| Homing (CH8=255) | 199 → 17-35 |
| DMX=0 (soft-end left) | 222 |
| DMX=558 (LEFT) | 250-270 |
| DMX=6521 (RIGHT) | 580-592 |

## What Was Done

1. **Clutter removal** — deleted `puzzle_pieces/` (17 files), `motor_smoke.py`, `uart_velocity_smoke.py`, `dmx_diag.py`
2. **Firmware slimming** — rewrote `config.py` and `main.py`, hardcoded motor params, stripped all DMX metadata tracking
3. **Git commit** `afd8b87` — pushed manually by user
4. **Verification** — X data confirmed matching reference after hard reset

## Open Issues

### Motion Glitches - RESOLVED

**Fixed**: Holds now stay fixed with 11-37px spread (was 90px). Position really stays fixed.

**Improved**: Fade smoothness with linear fade-aware controller:
- Added fade detection (rolling window of DMX changes)
- Smaller chunks during detected linear fades (4 steps vs 64)
- Tracking deadband (5 steps) reduces hunting during motion
- Results: good fades avg stdev reduced from 2.30 to 1.33

### Linear Fade-Aware Controller (IMPLEMENTED)

**Approach**: Detect when DMX is changing linearly, use smaller step chunks during fades.

**Detection**: Rolling 16-sample window, detects consistent DMX rate changes.

**Results**:
| Metric | Baseline | With Linear Fade Controller |
|--------|----------|----------------------------|
| Good fades avg stdev | 2.30 | **1.33** |
| Best fade stdev | 1.09 | **1.22** |
| Worst good fade | 4.02 | 1.44 |

**Config additions**:
```python
LINEAR_FADE_WINDOW = 16
LINEAR_FADE_VARIANCE_THRESH = 5.0
LINEAR_FADE_CHUNK_STEPS = 4
LINEAR_FADE_MIN_STEPS_SEC = 30
LINEAR_FADE_BLEND_MS = 200
VELOCITY_DEADBAND_HZ = 500
POSITION_TRACKING_DEADBAND = 5
```

**What didn't work**: Constant velocity mode (tried to match DMX rate exactly) - caused overshoot and correction bursts.

## Dual-Core Architecture

- **Core 0**: Main loop (homing + runtime), `ChunkedPositionController`, file I/O
- **Core 1**: DMX worker via `_thread.start_new_thread()`
- **SharedDMXState**: Lock-protected bridge between cores using `_thread.allocate_lock()`
- Communication: worker writes shared state under lock, main loop reads via `snapshot()` — lock-and-copy pattern

## Known Quirks

- **Pico needs hard reset** (`mpremote reset`) after firmware deploy — old code may persist otherwise
- **Motor must be enabled at all times** via UART (no enable management in slimmed firmware)
- **Port 9999** is the OpenCV streamer's TCP server running on the Raspberry Pi, not the Pico. X data captured with `nc localhost 9999`

## Commands

```bash
# Deploy firmware
cd firmware && ./deploy.sh

# Hard reset Pico
mpremote reset

# Capture X data
nc localhost 9999

# Collect X data to file (in another terminal)
nc localhost 9999 > x_data.txt
```
