# Next Steps

## Where We Are Now
- Startup homing with `PIO` steps and `UART StallGuard` is working.
- The firmware moves to center after homing.
- One-axis DMX runtime is working and can keep up with `44 fps`.
- The active runtime is now silent by default on the RP2040.
- The active runtime is currently configured for `1/128` microstepping.
- DMX channels `3..7` are live again, with `0..9` preserving firmware defaults and `10..255` activating runtime control.
- The active startup path now homes to one end only, then uses a fixed logical travel window instead of a measured end-to-end span.
- The current fixed logical travel window is `10000` microsteps with a `1000` step soft-end margin.
- One-axis smooth-ramp runtime motion has historical optical proof, but not yet for the latest fixed-span runtime tuning.
- A smooth-ramp verification workflow exists:
  - [hil/scenarios/smooth_position_ramp.csv](hil/scenarios/smooth_position_ramp.csv)
  - [hil/verify_smooth_dmx_ramp.py](hil/verify_smooth_dmx_ramp.py)
  - [run_smooth_ramp_check.sh](run_smooth_ramp_check.sh)
- The smooth-ramp baseline is now calibrated for the current Pi camera rig.
- Recent live-DMX trials show:
  - startup homing remains mechanically smooth
  - the runtime still exhibits visible jitter under DMX control
  - runtime soft-end margins were needed to stop targeting the hard-stop coordinates directly

## Immediate Task List

### Task 1: Make One-Axis DMX Motion Smooth
- Add enough target filtering, deadband, and/or internal vector-style motion planning that the fixture stops visibly chattering under steady DMX.
- Keep manual visual tests in the loop while tuning this.
- Treat the current fixed-span and soft-margin setup as the geometry baseline while solving motion quality.

### Task 2: Refresh The Optical Baseline
- Use:
  - `./run_smooth_ramp_check.sh --upload`
- Expect:
  - a valid vision CSV
  - a valid stimulus CSV
  - a valid runtime status JSON
  - a passing smoothness summary
- Treat this as the regression check again only after the latest runtime tuning is optically clean.

### Task 3: Capture Lessons From Commercial Fixtures
- Use the current research direction:
  - `16-bit` positioning
  - tracking vs vector-style motion control
  - digital filtering of small update steps
  - selectable motion curves such as `S-curve`
  - encoder-based calibration / correction as future work
- Translate those ideas into changes that are practical on the current RP2040 + TMC2209 hardware.

### Task 4: Bring Up The Second Axis
- Add the second axis on the same `PIO` + `UART` architecture.
- Keep the one-axis path working while adding the second step generator.
- Re-run functional runtime checks under continuous `44 fps` DMX load only after the one-axis motion quality milestone is complete.

### Task 5: Extend Optical Validation To Two Axes
- Add a dual-axis DMX scenario.
- Extend the vision scoring to validate both traces in the same run.
- Confirm that adding the second axis does not introduce timing instability or obvious motion artifacts.

## Next Milestone
- Achieve one **commercially credible smooth one-axis DMX run** with:
  - no visible idle jitter under steady DMX
  - no obvious chatter when making small position changes
  - no end-stop contact during full-scale moves inside the current soft margins
  - one refreshed optical smooth-ramp pass on the current runtime settings

## Milestones After That

### Milestone 4: Dual-Axis Motion
- Bring up the second axis without regressing one-axis smoothness.
- Achieve one optically verified smooth dual-axis DMX ramp run.

### Milestone 5: MVP Soak
- Run longer startup/runtime cycles.
- Verify DMX loss handling and jam behavior.

## What We Should Not Do Right Now
- Do not return to external `DIAG` work yet.
- Do not treat functional DMX logs alone as proof of smooth motion.
- Do not spend more time on firmware-side print debugging unless a specific bring-up issue requires it.

## Summary
- The Pico-side architecture is in the right place.
- The current open problem is no longer basic bring-up, but motion quality under live DMX.
- The next short-term proof point is one-axis motion that looks commercially smooth.
- After that, the next real proof point is dual-axis motion under the same validation discipline.
