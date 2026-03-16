# DMX Stepper Implementation Plan

## Current Status
- The repo layout is stable:
  - `firmware/` for active Pico code
  - `hil/` for host-side DMX and vision validation
  - `puzzle_pieces/` for historical references
- RP2040 DMX reception on `GP29` is proven with PIO.
- PIO step generation is implemented in the active firmware.
- TMC2209 UART on `UART0 TX=GP0 / RX=GP1` is proven and remains the required control path for:
  - microstep selection
  - run current
  - hold current
  - driver enable/disable, because `EN` is hardwired to `GND`
- The current active runtime is at `1/128` microstepping.
- Single-axis startup homing with `UART StallGuard` is working.
- Single-axis homing plus centering has already passed optical validation.
- One-axis DMX runtime at `44 fps` has been validated functionally.
- The runtime firmware is now quiet by default:
  - RP2040 `print()` output is disabled unless explicitly enabled for debugging
- DMX channels `3..7` are now live again with:
  - channels `1..2` = 16-bit position target
  - channels `3..6` = run current, hold current, max speed, acceleration
  - channel `7` = enable
  - values `0..9` preserving firmware defaults for channels `3..7`
  - values `10..255` activating the configured runtime ranges
- A smooth-ramp verification workflow now exists:
  - DMX scenario generation
  - OpenCV capture
  - trace smoothness scoring
- One-axis smooth-ramp runtime motion has now passed optical verification.
- The active runtime no longer measures its usable span on startup.
- The active one-axis startup flow is now:
  - seek one end with `UART StallGuard`
  - back off that end
  - move to center inside a fixed logical travel window
- The current fixed logical travel window is `10000` microsteps with a `1000` step soft-end margin, so full-scale DMX currently maps into `1000..9000`.
- Recent live-DMX tuning confirmed a new limitation:
  - startup homing remains smooth
  - live DMX motion is still visibly jittery and can hit soft limits or mis-track under aggressive changes
- The earlier optical smooth-ramp pass is therefore a proof point for an older tuning state, not the current production baseline.

## MVP Goal
Ship a reliable first firmware that can:
- boot cleanly
- home reliably at startup
- move to center after homing
- receive DMX at `44 fps`
- drive both motors with PIO-generated steps
- behave predictably on DMX loss
- be validated optically on real motion

For MVP, homing and runtime fault handling should be based on `UART StallGuard`, not external `DIAG`.

## Architecture Direction
- Keep `DMX receive` on PIO.
- Keep `step generation` on PIO for both axes.
- Keep `TMC2209 configuration` on UART.
- Keep the active firmware quiet by default to avoid adding serial overhead during runtime tests.
- Treat external `DIAG` as optional future work, not an MVP dependency.

## Why UART-Only Still Makes Sense
- DMX receive is already offloaded to PIO.
- Step generation is already offloaded to PIO.
- `44 fps` DMX is a light control workload for the RP2040 CPU.
- UART StallGuard is already working on real hardware.
- External `DIAG` remains electrically unresolved and has produced misleading results before.

## Completed Milestones

### Milestone 1: Single-Axis UART Homing
- Implemented startup homing with:
  - PIO step generation
  - UART StallGuard detection
  - UART-only enable/disable
- Implemented automatic move-to-center after homing.
- Verified optically with full travel.

### Milestone 2: One-Axis DMX Runtime
- Reintroduced one-axis DMX runtime on top of the verified homing result.
- Validated stable DMX reception at `44 fps`.
- Simplified the current runtime test mode so manual testing only needs:
  - channel 1 = position MSB
  - channel 2 = position LSB

## Current Validation State
- Homing-and-centering has optical proof.
- One-axis runtime has functional proof.
- One-axis smooth runtime motion has one historical optical proof point, but the current fixed-span runtime still needs a fresh optical proof after the latest motion changes.
- The highest-priority open issue is visible jitter under live DMX updates even when homing remains mechanically smooth.
- The exact `~2 s` full-span target is now secondary to getting commercially acceptable motion quality on one axis first.

## New Immediate Milestone: Commercial-Grade One-Axis Motion
- Before bringing up the second axis, make one-axis live DMX motion look and feel closer to a commercial moving head.
- Working assumptions from fixture and motor-control references:
  - keep `16-bit` pan/tilt style targeting
  - add fixture-side filtering / deadband so small DMX changes do not create visible chatter
  - support a vector-style internal move profile rather than simply tracking every tiny DMX update
  - consider selectable motion curves such as `linear` vs `S-curve`
  - treat encoder-assisted correction or other closed-loop feedback as future work if open-loop smoothness stalls out

## Research Notes
- Martin fixture manuals describe two motion strategies:
  - tracking mode, where the controller sends small updates and the fixture tracks them
  - vector mode, where the fixture uses an internal speed channel and can produce smoother motion, especially when incoming updates are slow or irregular
- Martin manuals also explicitly mention digital filtering of tracking updates for smooth movement and `16-bit` pan/tilt positioning.
- ETC / High End documentation shows that commercial fixtures also invest in:
  - encoder calibration and multiple encoder technologies
  - pan/tilt curve selection such as `S-curve` vs `Linear`
  - software optimization to prevent misstepping
  - lower parked motor current and other motion-quality tuning
- Trinamic documents reinforce that driver-level features such as interpolation and chopper-mode tuning matter, but those alone are not enough if the higher-level motion planner chatters.

## Phase 3: Second Axis Bring-Up
- Add the second axis on top of the same architecture only after the new one-axis motion-quality milestone is met:
  - shared DMX receiver
  - one PIO step generator per axis
  - shared UART configuration logic
- Keep the refreshed one-axis smooth-motion regression passing while the second axis is added.

## Phase 4: Dual-Axis Optical Validation
- Extend the smooth-ramp workflow to score both visible traces.
- Verify that both axes remain stable under continuous `44 fps` DMX updates.

## Phase 5: Soak And Failure Handling
- Add longer-duration runtime tests.
- Define and verify behavior for:
  - DMX loss
  - startup homing failure
  - runtime jam / stall
- Keep UART StallGuard as the default MVP fault path.

## Phase 6: Optional External DIAG
- Return to external `DIAG` only after the MVP path is stable.
- Goals:
  - identify the real RP2040 input pin
  - verify polarity and pull requirements
  - prove that the external signal corresponds to real mechanical events

## Validation Strategy
- Prefer optical validation over console output.
- Use firmware JSON outputs for structured status.
- Keep the Pico runtime silent by default.
- Treat a runtime test as incomplete unless the OpenCV trace is available when optical proof is expected.

## Recommended Order From Here
1. Remove visible one-axis DMX jitter with filtering, deadband, and/or a better internal motion profile.
2. Re-prove one-axis smooth motion optically with the current fixed-span runtime.
3. Revisit the exact `~2 s` traverse target only after the motion quality is acceptable.
4. Add the second axis and repeat functional runtime validation under load.
5. Extend the optical ramp workflow to score both axes together.
6. Add soak and fault-handling checks before revisiting external `DIAG`.
