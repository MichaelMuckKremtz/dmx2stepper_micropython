# dmx2stepper

RP2040-based DMX-to-stepper controller firmware with PIO DMX input, PIO step generation, TMC2209 UART control, UART StallGuard homing, and OpenCV-based hardware-in-the-loop verification.

## DMX Channel Map

The active one-axis runtime currently uses:

| Channel | Meaning | Notes |
| --- | --- | --- |
| 1 | position MSB | 16-bit position target |
| 2 | position LSB | 16-bit position target |
| 3 | run current | `0..9` = firmware default, `10..255` = active range |
| 4 | hold current | `0..9` = firmware default, `10..255` = active range |
| 5 | max speed | `0..9` = firmware default, `10..255` = active range |
| 6 | acceleration | `0..9` = firmware default, `10..255` = active range |
| 7 | enable | `0..9` = default enabled, `10..127` = disable, `128..255` = enable |

Current firmware defaults at `DMX=0` for channels `3..7` are:

- microsteps: `1/128`
- run current: `24`
- hold current: `12`
- max speed: `30000 steps/s`
- acceleration: `200000 steps/s^2`
- enabled: `true`

## Current Status

- DMX reception on the RP2040 is implemented with PIO.
- Step generation is implemented with PIO.
- TMC2209 configuration is handled over UART for microsteps, current, and driver control.
- The active firmware is currently running at `1/128` microstepping.
- Single-axis startup homing is working with UART StallGuard and has been optically verified.
- After homing, the active firmware moves to center and enters one-axis DMX runtime.
- One-axis runtime at 44 FPS DMX input has been validated functionally.
- One-axis smooth-ramp runtime motion has now passed optical verification with the current HIL workflow.
- Recent `1/128` bring-up measured startup homing at about `14.2 s` and end-to-end span at about `24.5k` microsteps.
- External `DIAG` is not part of the current MVP path.

## MVP Direction

The current MVP path is:

1. Reliable startup homing with PIO steps and UART StallGuard.
2. Optically verified one-axis DMX runtime.
3. Second-axis bring-up with the same architecture.
4. Dual-axis validation under sustained 44 FPS DMX input.

The axis homes on startup, moves to center, and then responds to the DMX channels above.

## Repository Layout

- [firmware/](firmware): active RP2040 firmware
- [hil/](hil): host-side DMX and vision verification tools
- [puzzle_pieces/](puzzle_pieces): historical experiments and reference implementations
- [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md): higher-level project plan
- [next_steps.md](next_steps.md): short-term milestones
- [diary.md](diary.md): recent implementation notes and findings

## Running The Firmware

Upload and run the current firmware:

```bash
./run_firmware.sh --upload
```

Run without re-uploading:

```bash
./run_firmware.sh
```

Run with RP2040 debug prints enabled:

```bash
./run_firmware.sh --debug
```

Direct deploy only:

```bash
bash firmware/deploy.sh /dev/ttyACM0
```

## Smooth Ramp Verification

Run a long eased DMX position-ramp and score the OpenCV trace for monotonicity, backtracking, and frame-to-frame jumps:

```bash
./run_smooth_ramp_check.sh --upload
```

The default workflow uses [hil/scenarios/smooth_position_ramp.csv](hil/scenarios/smooth_position_ramp.csv) and writes:

- a DMX stimulus log
- a vision CSV
- a copied Pico homing result
- a copied Pico runtime status
- a summary JSON with smoothness metrics

## Next Steps

- Lock the default one-axis full-span move to the intended `~2 s` runtime target at `1/128` microstepping.
- Bring up the second axis with the same PIO + UART architecture.
- Validate dual-axis runtime under sustained DMX load.
- Add soak and fault-handling validation for MVP.
