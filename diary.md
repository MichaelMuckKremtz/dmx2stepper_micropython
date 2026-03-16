# Diary

## 2026-03-16

### Goal
- Finish milestone 1:
  - `UART-only` startup homing
  - `PIO` step generation
  - move to center after homing
  - optical verification
- Finish milestone 2:
  - one-axis DMX runtime on top of the homing result
  - prove stable DMX reception at `44 fps`

### What I Changed
- Reworked the Pico firmware in `firmware/main.py` so it now:
  - homes with `PIO` step generation
  - uses `UART StallGuard` only for end-stop detection
  - centers after measuring the end-to-end span
  - stays alive for one-axis DMX runtime
- Kept `TMC2209` configuration on UART:
  - microsteps
  - run current
  - hold current
  - enable/disable because `EN` is tied to `GND`
- Moved the DMX receiver state machine to `SM4` so it no longer conflicts with the stepper PIO state machines.
- Added explicit verification-mode overrides:
  - `RUN_RUNTIME_AFTER_HOMING`
  - `RUNTIME_EXIT_AFTER_MS`
  These let the host run homing-only or time-boxed runtime checks cleanly.
- Added a one-axis runtime scenario:
  - `hil/scenarios/one_axis_runtime.csv`
- Added a dedicated runtime verifier:
  - `hil/verify_one_axis_dmx_runtime.py`
- Updated `hil/verify_pio_homing.py` so it verifies the new `UART-only` homing contract instead of insisting on external `DIAG`.
- Increased the DMX frame gap timeout in `firmware/dmx_receiver.py` so the Pico keeps reading the full 8-channel block under load.

### Key Tuning Decisions
- The first working homing setup was:
  - `STEP=GP2`
  - `DIR=GP3`
  - `home_direction=-1`
  - `speed=800 Hz`
- The important homing change was reducing the UART stall confirmation count from `4` to `2`.
  - Before that, the firmware kept banging into the end-stop too long.
  - After that, it stopped early enough to be mechanically reasonable.
- I also removed the speed sweep for homing bring-up.
  - The wider sweep created too much unnecessary stalling when the first good trial was already known.

### Milestone 1 Result
- Milestone 1 passed with optical verification.
- Passing artifacts:
  - `hil/captures/vision_homing_20260316_170603.csv`
  - `hil/captures/homing_result_20260316_170603.json`
- Passing summary:
  - firmware `success=true`
  - firmware `centered=true`
  - selected trial `0`
  - optical travel span `338.7 deg`
  - final end span `0.0 deg`
  - center error `0.95 deg`

### Milestone 2 Result
- Milestone 2 passed functionally in a headless check.
- Passing artifacts:
  - `hil/captures/runtime_homing_result_20260316_171443.json`
  - `hil/captures/runtime_status_20260316_171443.json`
  - `hil/captures/dmx_runtime_20260316_171443.csv`
- Passing summary:
  - startup homing succeeded first
  - Pico reported `644` received DMX frames during the timed runtime
  - runtime stayed active
  - the position moved from `1530` to `1922` steps under DMX control

### Important Findings
- External `DIAG` is still not needed for the MVP path.
  - `UART StallGuard` is now the more trustworthy path on this hardware.
- The DMX receiver was truncating frames under runtime load.
  - Symptom: only channels `1` and `2` arrived reliably, while channels `3..7` looked like zeros.
  - Fix: widen the inter-byte timeout in `firmware/dmx_receiver.py`.
- The host-side camera stack became unstable during milestone 2 verification.
  - `vision_observer.py` started failing with `Device or resource busy`.
  - `pipewire` and `wireplumber` were part of the problem, but even after killing them the camera path stayed flaky.
  - Because of that, milestone 2 was verified headlessly instead of optically.

### What Was Learned
- The RP2040 architecture is still the right one:
  - PIO for DMX input
  - PIO for step generation
  - UART for TMC2209 configuration and StallGuard polling
- The current single-axis path is strong enough to keep building on:
  - startup homing works
  - centering works
  - one-axis DMX runtime works
  - the Pico can keep up with `44 fps` DMX traffic

### What I Would Check Next
- Restore reliable host camera access so runtime motion can be optically verified again.
- Re-run milestone 2 with the camera once the host-side video stack is stable.
- Then add the second axis using the same architecture instead of returning to the external `DIAG` problem first.

### Milestone 2 Optical Follow-Up
- The camera path recovered later the same day and `vision_observer.py` completed successfully again.
- I added a dedicated smooth-ramp workflow:
  - `hil/scenarios/smooth_position_ramp.csv`
  - `hil/verify_smooth_dmx_ramp.py`
  - `run_smooth_ramp_check.sh`
- The Pico runtime was kept quiet by default and made easier to debug explicitly with:
  - `firmware/config.py: DEBUG_LOGGING`
  - `./run_firmware.sh --debug`
- The motion loop needed one practical runtime tuning change:
  - reduce the chunk size to `10` steps
  - reduce the runtime control sleep to `2 ms`
- The host-side vision path also needed calibration for this Pi camera rig:
  - use `640x480` capture
  - lower contour `MIN_AREA` to `150`
  - keep the smooth-ramp verifier thresholds realistic for the observed camera cadence

### Smooth Ramp Result
- The one-axis smooth-ramp run now passes optically.
- Passing artifacts:
  - `hil/captures/smooth_ramp_homing_result_20260316_214659.json`
  - `hil/captures/smooth_ramp_runtime_status_20260316_214659.json`
  - `hil/captures/vision_smooth_ramp_20260316_214659.csv`
  - `hil/captures/dmx_smooth_ramp_20260316_214659.csv`
  - `hil/captures/smooth_ramp_summary_20260316_214659.json`
- Passing summary:
  - runtime active `true`
  - received DMX frames `917`
  - visible travel span `242.0 deg`
  - segment 1 monotonic ratio `1.0`, max step `6.4 deg`
  - segment 2 monotonic ratio `1.0`, max step `28.7 deg`
  - segment 3 monotonic ratio `0.7553`, max step `16.5 deg`

### Updated Conclusion
- Milestone 2 is now complete both functionally and optically.
- The smooth-ramp workflow is the correct regression check before further runtime changes.
- The next engineering step is second-axis bring-up, not more one-axis firmware refactoring.

### DMX Channel Follow-Up
- The one-axis runtime no longer stays in `position-only` mode.
- The active DMX contract is now:
  - channel `1` = position MSB
  - channel `2` = position LSB
  - channel `3` = run current
  - channel `4` = hold current
  - channel `5` = max speed
  - channel `6` = acceleration
  - channel `7` = enable
- For channels `3..7`, values `0..9` preserve firmware defaults and values `10..255` activate the configured runtime range.

### 1/128 Microstep Bring-Up
- I changed the active microstep mode from `1/16` to `1/128`.
- That required scaling the homing path for the finer step size:
  - homing search distances
  - retract/release distances
  - homing speeds
- Measured results from the `1/128` bring-up:
  - startup homing takes about `14.2 s`
  - end-to-end travel is about `24.5k` microsteps
- I also raised the default runtime speed, acceleration, and chunking limits substantially so the default full-span move can be pushed toward the intended `~2 s` target.

### New Scenarios
- Added:
  - `hil/scenarios/end_to_end_default.csv`
  - `hil/scenarios/end_to_end_speed_max.csv`
- These are for direct end-to-end runtime timing checks at the current microstep setting.

### Current Status Of The 2-Second Goal
- The `1/128` runtime is much faster now than the initial bring-up.
- The exact default full-span runtime is not locked to `2.0 s` yet.
- The next focused task is to pin that default traverse time cleanly before returning to second-axis work.

### Fixed-Span Runtime Follow-Up
- Live visual checks showed that using the measured StallGuard span directly caused repeated end-stop contact during DMX moves.
- I changed the startup flow so it now:
  - seeks one end only with `UART StallGuard`
  - backs off that end
  - moves to center inside a fixed logical travel window
- I then tuned the logical travel window downward in stages while watching the live motion:
  - `23000`
  - `20000`
  - `10000`
- The current logical travel window is `10000` microsteps.

### Soft-End Margin Follow-Up
- Defining the runtime range directly as `0..travel_steps` still let DMX full-low/full-high target the hard-stop coordinates.
- I added a runtime soft-end margin so the active DMX target range is inside the fixed travel window instead of on the hard stops.
- Current setting:
  - fixed travel window `10000`
  - soft-end margin `1000`
  - full-scale DMX currently maps into `1000..9000`

### What The Latest Visual Tests Show
- Startup homing remains mechanically smooth and repeatable enough to keep building on.
- Under live DMX control, the motion is still visibly more jittery than startup homing.
- The jitter is present even when the console is not intentionally changing values, which strongly suggests that the current runtime planner is too eager to react to tiny target updates.
- One headless margin-enabled run also ended with the controller still in motion rather than cleanly parked at the final high target, which is another sign that the current runtime motion path needs refinement.

### Research Direction For Commercial-Grade Smoothness
- I checked current fixture and motor-control references to understand what commercial moving heads do differently.
- The recurring themes were:
  - `16-bit` pan/tilt positioning
  - tracking vs vector-style motion control
  - digital filtering of small tracking updates
  - selectable motion curves such as `S-curve` vs `Linear`
  - encoder calibration / encoder feedback
  - firmware-side optimization specifically to prevent pan/tilt misstepping
- The practical takeaway for this repo is that the next milestone should be motion quality on one axis, not second-axis bring-up.

### Updated Conclusion
- The architecture is no longer the main blocker.
- The active blocker is runtime motion quality under live DMX.
- The next milestone is to make one-axis DMX motion feel closer to a commercial moving head:
  - add deadband and/or filtering
  - consider a vector-style internal move profile
  - re-prove one-axis smooth motion optically on the current runtime
- Second-axis work should resume only after that milestone is met.
