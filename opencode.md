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

**Remaining**: Jitter on the slow linear fade moves (LEFT/RIGHT transitions). The fade portions still show continuous oscillation during motion.

### Next Milestone: Butter-smooth motion in fades

Goal: Reduce/eliminate the micro-jitter during slow linear fade portions of the DMX loop (7s transitions from center→LEFT and LEFT→RIGHT).

Investigation needed:
- H2: Chunked blocking motion (64-step chunks with 3.4ms blocking)
- H4: Blocking sleep in `pio_stepper.py` `move_fixed_steps_blocking()`

### Top Hypothesis (H1): `apply_snapshot` re-triggers motion every frame

In `main.py` lines 165-178, `ChunkedPositionController.apply_snapshot()`:
```python
def apply_snapshot(self, snapshot_target_u16):
    new_target = int(map_u16_to_steps_with_margin(...))
    at_target = (current_pos == target_pos and speed < 1.0)
    if at_target and abs(new_target - current_pos) <= DEADBAND:
        pass  # skipped
    else:
        self.target_position_steps = new_target  # ALWAYS SETS, even if DMX unchanged
```

Every DMX frame re-sets `target_position_steps`, causing the step accumulator to keep hunting. Fix: track `_last_applied_target_u16`, only call `set_target()` when the DMX value actually changes.

### Secondary Hypothesis (H2): Chunked blocking motion

64-step chunks with ~3.4ms `time.sleep_ms()` blocking creates discrete motion bursts. Try reducing `RUNTIME_MAX_CHUNK_STEPS` in `config.py`.

### Tertiary Hypothesis (H4): Blocking sleep in `pio_stepper.py`

`time.sleep_ms(wait_ms)` in `move_fixed_steps_blocking()` blocks the main loop.

## Investigation Plan

See `/home/pi/.local/share/opencode/plans/glitch_investigation.md` for full 5-step plan.

### Step 1 (COMPLETED)
Added `_last_applied_target_u16` to `ChunkedPositionController` in `main.py`. The `apply_snapshot()` method now returns early if the DMX u16 value hasn't changed, preventing the step accumulator from hunting on every frame.

**Results**: X data shows dramatic improvement:
- RIGHT hold region (lines 695-720): **11px spread** (551-562)
- Previous issue: "90px spread during LEFT (expected ~25px)"
- Micro-jitter eliminated, motor now settles properly during holds

### After fix
Run 3+ full loops of X data collection and analyze. Look for reduction in oscillation spread. ✓ Done - spread reduced from ~90px to 11-37px

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
