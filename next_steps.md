# Next Steps

## Where We Are Now
- PIO-based step generation is implemented and is still the correct architecture.
- TMC2209 UART control is working and remains required for:
  - microstep selection
  - motor current configuration
  - driver enable/disable, because `EN` is soldered to `GND`
- The current homing/debug firmware can exercise `STEP=GP2` and `DIR=GP3` and also try the swapped pair.
- The OpenCV pipeline is already good enough to reject false homing passes.
- The key new finding is negative:
  - the recent `DIAG`-based "successes" were not real end-stop detections
  - the external `DIAG` path has not been validated on `GP8..GP13`
  - we should not block progress on external `DIAG`

## New Immediate Milestone
- Achieve one **optically verified UART-only homing and centering pass** on the RP2040 using:
  - PIO-generated steps
  - UART StallGuard detection
  - `STEP=GP2` / `DIR=GP3` first, with the swapped fallback if needed

## Success Criteria For This Milestone
- The Pico boots and configures the TMC2209 over UART.
- The motor performs a real homing search with PIO-generated steps.
- The OpenCV trace shows total mechanism travel greater than `270 deg`.
- The firmware moves the motor to the mechanical midpoint after homing.
- The final centered position settles cleanly and repeatably.
- No small-travel false trigger is accepted as success.

## What This Means In Practice
- External `DIAG` is now a debug aid, not the primary stop source.
- The MVP path is to trust UART StallGuard first and prove it with optical data.
- Any firmware path that claims success without full observed travel is wrong by definition.

## Milestones After That

### Milestone 2: One-Axis DMX Runtime
- Reintroduce DMX control for one motor on top of the verified homing path.
- Validate stable operation under continuous `44 fps` incoming DMX.
- Start with coarse motion and large setpoint changes that the OpenCV pipeline can verify.

### Milestone 3: Two-Axis DMX Runtime
- Bring up both motors together with the same PIO/UART architecture.
- Confirm that DMX reception, target updates, and both PIO step generators remain stable under load.

### Milestone 4: MVP Soak
- Run longer tests with repeated DMX changes, startup cycles, and DMX loss handling.
- Confirm there are no hidden timing regressions or resource issues on the RP2040.

### Milestone 5: Optional External DIAG
- Return to the hardware `DIAG` question only after the MVP path is stable.
- If needed later, verify the real pin, polarity, and pull configuration with a dedicated probe.

## Recommended Next Engineering Step
1. Remove the remaining dependency on external `DIAG` for declaring homing success.
2. Tune UART StallGuard thresholds and homing speed until one full-travel centered homing run passes optically.
3. Once that is stable, merge the homing path back into the one-axis DMX control firmware.

## Things We Should Stop Doing
- Stop treating internal `IOIN.DIAG` toggles as proof of a valid external `DIAG` signal.
- Stop accepting short travel spans as homing success.
- Stop delaying DMX integration while waiting for the external `DIAG` wiring problem to be solved.

## Summary
- The project should now aim for a `UART-only` homing MVP.
- PIO step generation stays mandatory.
- External `DIAG` is optional and can be solved later.
- The next real proof point is not another DIAG scan.
- The next real proof point is one repeatable, full-travel, optically verified homing-and-centering pass.
