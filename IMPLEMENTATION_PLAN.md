# DMX Stepper Implementation Plan

## Current Status
- The repo layout is established: `firmware/` for Pico code, `hil/` for host-side validation, and `puzzle_pieces/` for historical reference code.
- The host-side tools are usable:
  - `hil/vision_observer.py` can verify coarse angular motion.
  - `hil/dmx_stimulus.py` can transmit and log DMX frames.
  - `hil/coordinator.py` exists, but it is not yet the authoritative pass/fail runner.
- RP2040 DMX reception on `GP29` is already proven.
- TMC2209 UART on `UART0 TX=GP0 / RX=GP1` is already proven.
- PIO step generation is implemented in the active firmware.
- The old external `DIAG` assumption is not proven. A scan of `GP8..GP13` did not produce one trustworthy hardware end-stop signal.
- Recent small-travel "homing successes" were false positives. The OpenCV pipeline showed that those runs did not traverse the true mechanism range.

## MVP Goal
Ship a reliable first firmware that can:
- boot cleanly
- receive DMX at the required `44 fps`
- drive both motors with PIO-generated steps
- home reliably at startup
- recover or fail safely on DMX loss
- be validated by the OpenCV pipeline on large moves

For MVP, homing should be based on `UART StallGuard` rather than requiring external `DIAG`.

## Design Direction
- Keep `DMX receive` on PIO.
- Keep `step generation` on PIO for both axes.
- Keep `TMC2209 configuration` on UART:
  - microsteps
  - run current
  - hold current
  - StallGuard setup
  - driver enable/disable, since `EN` is hardwired to `GND`
- Treat external `DIAG` as an optional future optimization, not a current dependency.

## Why UART-Only For MVP
- The RP2040 has enough headroom for this architecture:
  - DMX is handled by PIO.
  - Step generation is handled by PIO.
  - The CPU only needs to parse frames, update target states, and poll UART during homing or fault handling.
- `44 fps` DMX means a new frame roughly every `22.7 ms`, which is a light control workload.
- External `DIAG` is currently less trustworthy than UART because its physical pin mapping and electrical behavior are still unresolved.
- A false hardware stall input is worse than no hardware stall input.

## Phase 1: Single-Axis MVP Homing
- Finish one-axis homing using:
  - PIO step generation
  - UART-based StallGuard detection
  - UART-only driver enable/disable
- Require optical validation for success:
  - total triangle travel must exceed `270 deg`
  - the final centered position must settle cleanly
- Remove any firmware path that can report success after small-travel false triggers.
- Keep the current external `DIAG` instrumentation for debugging, but do not block MVP on it.

## Phase 2: Single-Axis DMX Motion
- Reintroduce DMX control on top of the verified homing foundation.
- Start with one axis and a small DMX mapping:
  - target position
  - max speed
  - acceleration
  - run current
  - hold current
- Verify:
  - stable response to repeated `44 fps` updates
  - no missed DMX frames under motion
  - no motion instability while receiving continuous DMX

## Phase 3: Dual-Axis Runtime
- Bring up the second axis using the same architecture:
  - PIO DMX receiver
  - two PIO step generators
  - shared UART config logic
- Validate:
  - both axes can run while DMX is streaming
  - no starvation or timing regressions
  - camera verification remains usable for large coordinated moves

## Phase 4: Soak And Failure Handling
- Add long-running tests with continuous DMX updates.
- Define runtime behavior for:
  - DMX loss
  - startup homing failure
  - runtime stall / jam
- For MVP, a runtime jam can still be handled with UART polling if needed.

## Phase 5: Optional DIAG Hardware Support
- Only after MVP is stable, return to external `DIAG`.
- Goal:
  - find the real RP2040 input pin
  - verify polarity and pull requirements electrically
  - prove that the external GPIO reflects real end-stop events
- If that succeeds, add `DIAG` as an optional fast-path for homing and jam detection.

## Validation Strategy
- Prefer optical verification over console output.
- Treat OpenCV as the authority for large travel and final settling.
- Use DMX logs plus firmware result JSONs for correlation.
- Do not count a homing run as valid unless the observed mechanical travel matches the expected physical range.

## Recommended Order From Here
1. Make single-axis homing reliable with `UART-only` stall detection.
2. Verify that the axis always centers after homing and that the OpenCV trace shows full travel.
3. Reintroduce one-axis DMX motion and test sustained `44 fps` updates.
4. Add the second axis and repeat the same validation under load.
5. Return to external `DIAG` only after the MVP firmware is already stable.
