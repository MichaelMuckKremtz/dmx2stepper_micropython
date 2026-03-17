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

Current one-axis runtime geometry defaults are:

- startup homing: single-end `UART StallGuard`
- fixed logical travel window: `20000` microsteps
- runtime soft-end margin: `1000` microsteps
- full-scale DMX position range with the current margin: `1000..19000`

## Current Status

- DMX reception on the RP2040 is implemented with PIO.
- Step generation is implemented with PIO.
- TMC2209 configuration is handled over UART for microsteps, current, and driver control.
- The active firmware is currently running at `1/128` microstepping.
- Single-axis startup homing now seeks one end only with `UART StallGuard`, backs off, and moves to center inside a fixed logical span.
- After homing, the active firmware moves to center and enters one-axis DMX runtime.
- One-axis runtime at 44 FPS DMX input has been validated functionally.
- The current fixed-span runtime is being manually tuned against end-stop contact and visible jitter under live DMX control.
- Recent runtime tuning switched from measured end-to-end travel to a fixed logical travel window plus soft-end margins because measured StallGuard spans were larger than the real safe motion range.
- The earlier one-axis smooth-ramp optical pass is now historical evidence for a previous tuning state, not the active regression baseline for the latest runtime settings.
- External `DIAG` is not part of the current MVP path.

## MVP Direction

The current MVP path is:

1. Reliable startup homing with PIO steps and UART StallGuard.
2. One-axis DMX runtime that remains smooth under live DMX updates, not just under scripted bring-up checks.
3. Re-establish optically verified one-axis smooth motion with the current runtime mapping and limits.
4. Second-axis bring-up with the same architecture only after the one-axis motion quality is acceptable.

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

## Live Ethernet Camera Stream

Expose the Pi camera over HTTP/MJPEG while the same process continues doing the OpenCV angle tracking:

```bash
./run_camera_stream.sh
```

By default the stream binds to `0.0.0.0:8080`. Open `http://<pi-ip>:8080/` from another machine on the same Ethernet network.

Direct usage is also available if you want to change the bind settings or logging behaviour:

```bash
python3 hil/vision_observer.py --stream --stream-host 0.0.0.0 --stream-port 8080
```

The stream is served from the same `vision_observer.py` capture loop, so OpenCV tracking and remote viewing share one camera client instead of fighting over the device.

Existing HIL scripts can enable the same stream without extra code changes by exporting the inherited environment variables before launch:

```bash
VISION_STREAM=1 VISION_STREAM_PORT=8080 ./run_smooth_ramp_check.sh --upload
```

Optional environment knobs:

- `VISION_STREAM_HOST`
- `VISION_STREAM_PORT`
- `VISION_STREAM_FPS`
- `VISION_STREAM_JPEG_QUALITY`

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

## Idle No-Step Verification

Run a headless regression that fails if the runtime emits any steps while disabled or after reaching a fixed target:

```bash
python3 hil/verify_idle_no_steps.py --mode disabled-idle --upload
python3 hil/verify_idle_no_steps.py --mode fixed-target
```

Optional raw vision capture can be added for a secondary spot check:

```bash
python3 hil/verify_idle_no_steps.py --mode disabled-idle --with-vision --vision-filter-window 1 --vision-deadband-deg 0
```

## Jump-Hold Runtime Diagnostics

Run no-ramp position jumps with hold windows while polling Pico telemetry:

```bash
python3 hil/verify_jump_hold_runtime.py --upload --with-vision --vision-filter-window 1 --vision-deadband-deg 0
```

This writes:

- a DMX stimulus log
- a polled runtime telemetry CSV
- copied Pico homing/result JSON files
- an optional raw-vision CSV
- a summary JSON with hold-window details

## Full Diagnostics Suite

Run the current homing benchmark, no-step idle checks, and jump-hold runtime checks in one command:

```bash
python3 hil/run_motion_diagnostics.py --upload --with-vision
```

## Next Steps

- Build a mixed DMX validation scenario with:
  - linear ramps at multiple speeds
  - discrete jumps between positions
  - a slow sine wave with growing amplitude
- Score that scenario against the output of the continuously running video observer instead of relying on DMX logs alone.
- Make live DMX motion look commercially smooth on one axis.
- Re-establish a passing optical smooth-ramp check for the current runtime mapping.
- Only then resume second-axis bring-up and dual-axis validation.
- Add soak and fault-handling validation after the motion-quality milestone.
