# Next Steps

## Where We Are Now
- Startup homing with `PIO` steps and `UART StallGuard` is working.
- The firmware moves to center after homing.
- One-axis DMX runtime is working and can keep up with `44 fps`.
- The active runtime is now silent by default on the RP2040.
- The active runtime is currently configured for `1/128` microstepping.
- DMX channels `3..7` are live again, with `0..9` preserving firmware defaults and `10..255` activating runtime control.
- One-axis smooth-ramp runtime motion has now passed optical verification.
- A smooth-ramp verification workflow exists:
  - [hil/scenarios/smooth_position_ramp.csv](hil/scenarios/smooth_position_ramp.csv)
  - [hil/verify_smooth_dmx_ramp.py](hil/verify_smooth_dmx_ramp.py)
  - [run_smooth_ramp_check.sh](run_smooth_ramp_check.sh)
- The smooth-ramp baseline is now calibrated for the current Pi camera rig.
- Recent `1/128` measurements show:
  - startup homing at about `14.2 s`
  - end-to-end travel at about `24.5k` microsteps

## Immediate Task List

### Task 1: Lock The Default One-Axis Speed
- Measure and tune the default `1/128` runtime so one end-to-end move lands near `2 s`.
- Keep this separate from the DMX-controlled speed ceiling work.

### Task 2: Keep The One-Axis Ramp As The Baseline
- Use:
  - `./run_smooth_ramp_check.sh --upload`
- Expect:
  - a valid vision CSV
  - a valid stimulus CSV
  - a valid runtime status JSON
  - a passing smoothness summary
- Treat this as the regression check before changing runtime behavior again.

### Task 3: Bring Up The Second Axis
- Add the second axis on the same `PIO` + `UART` architecture.
- Keep the one-axis path working while adding the second step generator.
- Re-run functional runtime checks under continuous `44 fps` DMX load.

### Task 4: Extend Optical Validation To Two Axes
- Add a dual-axis DMX scenario.
- Extend the vision scoring to validate both traces in the same run.
- Confirm that adding the second axis does not introduce timing instability or obvious motion artifacts.

## Next Milestone
- Achieve one **optically verified smooth dual-axis DMX ramp run** with:
  - both axes active
  - valid OpenCV capture
  - no major backtracking on either trace
  - no large CV jumps on either trace
  - stable `44 fps` DMX reception throughout the run

## Milestones After That

### Milestone 4: MVP Soak
- Run longer startup/runtime cycles.
- Verify DMX loss handling and jam behavior.

## What We Should Not Do Right Now
- Do not return to external `DIAG` work yet.
- Do not treat functional DMX logs alone as proof of smooth motion.
- Do not spend more time on firmware-side print debugging unless a specific bring-up issue requires it.

## Summary
- The Pico-side architecture is in the right place.
- The one-axis optical runtime proof point now exists.
- The next short-term proof point is a clean `~2 s` default full-span move at `1/128`.
- After that, the next real proof point is dual-axis motion under the same validation discipline.
