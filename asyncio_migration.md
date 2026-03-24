# Asyncio Migration Plan

## Migration steps — incremental with validation

Each step produces a deployable firmware that is validated against the camera before proceeding. No step changes more than one subsystem.

### Step 1: Add non-blocking PIO API

**Change:** `pio_stepper.py` only — add `start_counted()`, `finalize_counted()`, `pending_wait_ms`

**Validation:** Deploy, run homing + runtime with existing blocking code. Camera capture must show no regression (the new methods aren't called yet, just added).

### Step 2: Add async DMX reader

**Change:** `dmx_receiver.py` only — add `async_read_frame()` alongside existing `read_frame()`

**Validation:** Same as step 1 — new code exists but isn't called yet.

### Step 3: Split controller into compute + record

**Change:** `main.py` — refactor `ChunkedPositionController` to separate `compute_next_chunk()` and `record_moved()`. Keep `update()` as a thin wrapper calling both for backwards compatibility.

**Validation:** Deploy, capture. The wrapper ensures identical behavior. Camera must show no regression.

### Step 4: Convert runtime loop to asyncio

**Change:** `main.py` — replace `_thread` + `while True` with `asyncio.run()` + `asyncio.gather()`. Remove `SharedDMXState` lock. Wire up `dmx_task` and `motion_task`.

**Validation:** This is the critical step. Deploy, capture 20s, analyze. Compare stdev and max_dev against baseline. Expect improvement (fewer spikes) or at worst parity.

### Step 5: Clean up dead code

**Change:** Remove `SharedDMXState`, `RUNTIME_CONTROL_SLEEP_MS`, the `update()` wrapper, `_thread` import, any other code that only existed for the threaded architecture.

**Validation:** Final capture. Confirm clean architecture with no regression.

## Agentic validation loop

Each migration step runs through an automated validation cycle:

```
┌─────────────────────────────────────────────────────┐
│                  MIGRATION STEP N                   │
│                                                     │
│  1. Make code change (one subsystem only)            │
│  2. Deploy to Pico via mpremote                      │
│  3. Wait for homing (~35s)                           │
│  4. Run 3x 20s captures via capture_and_visualize.sh │
│  5. Compute median stdev and max_dev across 3 runs   │
│  6. Compare against baseline median (3-run baseline) │
│                                                     │
│  ┌─ IF regression (median stdev > baseline + 2px) ──┐│
│  │  • Diagnose: read capture PNGs, check patterns   ││
│  │  • Identify root cause in the step's changes     ││
│  │  • Fix and re-run from step 2                    ││
│  │  • Max 3 fix attempts per step                   ││
│  │  • If still regressed: revert step, report       ││
│  └──────────────────────────────────────────────────┘│
│                                                     │
│  ┌─ IF pass ────────────────────────────────────────┐│
│  │  • Record new baseline = this step's median      ││
│  │  • Proceed to step N+1                           ││
│  └──────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────┘
```

## Mandatory validation protocol

**Every code change MUST be validated with a 20s capture before proceeding.**

```bash
./capture_and_visualize.sh 20
```

Deploy idle firmware (`idle_main.py` — a trivial `while True: time.sleep(1)` loop) immediately after each capture so the motor is safe while analyzing results. Never leave experimental firmware running unattended.

Baseline (threaded firmware, commit 6ea1609): **avg stdev ~20px, avg max_dev ~43px**.
Any change that produces stdev > baseline + 5px is a regression — revert before continuing.

## Key lessons learned

1. **The PIO FIFO is the bottleneck.** The 8-entry, 352 us buffer makes it impossible to safely yield during DMX byte reading. Any approach that yields during the ~23 ms frame read risks byte-level corruption.

2. **Cooperative scheduling is too coarse for this workload.** The two tasks need microsecond-level interleaving that only the GIL (or hardware DMA) can provide. Asyncio yields at millisecond granularity.

3. **Large motion chunks don't help.** Increasing chunk size to cover the 23 ms DMX block causes the acceleration controller to overshoot (300,000 steps/s^2 * 25 ms = 7,500 Hz speed change per cycle, vs 300 Hz in the original ~1 ms cycles).

4. **SM resync after early frame exit is fragile.** After reading only the first N bytes, the PIO SM stalls with a full FIFO. Resync at the next break requires deactivate + drain + reactivate, but the ~50-80 us this takes exceeds the 8-12 us mark-after-break window, missing the start code.

## Possible future approaches (not yet attempted)

- **Read only needed channels**: Instead of reading all 512 DMX channels (~23ms), read only the last 16 channels (~1.5ms). Add `DMX_START_CHANNEL` config (e.g., 497 for last 16). Requires PIO SM to capture only specific channel range.

- **PIO-level DMA**: Use RP2040 DMA to read DMX bytes directly into a buffer without CPU involvement. This eliminates the 23 ms CPU block entirely. MicroPython's DMA support is limited but may be accessible via `mem32` register writes.

- **Dual-PIO DMX reader**: A second PIO SM that extracts only the needed channels from the DMX stream, reducing FIFO pressure.

- **Interrupt-driven DMX**: Use PIO IRQ to signal Python only when channel data is ready, rather than polling.

- **Hybrid: asyncio for motion + thread for DMX**: Keep DMX on core 1 via `_thread` (where the GIL is acceptable for I/O-bound work) and use asyncio only for the motion loop. This might give the best of both worlds.

## What stays the same

- All PIO programs (`step_count_pio`, `step_freerun_pio`, `step_counter_pio`, `dmx_rx`)
- All TMC2209 UART communication
- Homing algorithm (synchronous, runs before asyncio loop)
- `config.py` parameter meanings and values
- HIL test infrastructure (capture.py, analyze_x_data.py, etc.)
- OpenCV streamer
- Hardware wiring
