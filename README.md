# DMX-to-Stepper

MicroPython firmware for the Raspberry Pi Pico (RP2040) that controls a stepper motor via a TMC2209 driver, driven by DMX-512 input. The Pico auto-homes on power-up using UART-only Stallguard detection (no physical limit switches), then maps two DMX channels (16-bit, 0–65535) to the full travel range.

An optional Raspberry Pi running OpenCV can be connected to provide vision-based position feedback via TCP on port 9999.

---

## Hardware

| Component | Details |
|---|---|
| MCU | Raspberry Pi Pico (RP2040) |
| Stepper driver | TMC2209 (UART control) |
| Motor | NEMA 17, 128 microsteps, ~20000 steps travel |
| DMX interface | RS-485 transceiver on GP29 |
| Vision (optional) | Raspberry Pi + CSI camera (imx477 HQ tested) |

### Pinout (Pico)

| Function | GPIO | Notes |
|---|---|---|
| DMX input | GP29 | RS-485 DI, pulled high |
| UART TX | GP0 | TMC2209 RX |
| UART RX | GP1 | TMC2209 TX |
| STEP | GP2 | TMC2209 STEP |
| DIR | GP3 | TMC2209 DIR |
| DIAG | GP8 | Stallguard DIAG input |

---

## File Layout

```
.
├─ firmware/           ← active firmware (deploy this)
│  ├─ main.py          runtime loop, homing, DMX handling
│  ├─ config.py        all hardware/motion constants
│  ├─ dmx_receiver.py  PIO DMX512 receiver
│  ├─ tmc2209.py       driver wrapper (currents, stallguard, enable)
│  ├─ tmc2209_uart.py  low-level UART transport
│  ├─ pio_stepper.py   PIO step generation + pulse counter
│  └─ deploy.sh        deploy to Pico via mpremote
├─ hil/                hardware-in-loop (Raspberry Pi side)
│  └─ opencv_streamer/ camera vision, streams MJPEG + TCP X coordinate
├─ capture.sh          wrapper to run capture.py
└─ run_camera_stream.sh  wrapper to run opencv_streamer
```

---

## Configuration

All hardware parameters live in `firmware/config.py`. Key values:

### DMX

| Parameter | Default | Description |
|---|---|---|
| `DMX_PIN` | 29 | GPIO connected to RS-485 DI |
| `DMX_SM_ID` | 4 | PIO state machine for DMX RX |
| `DMX_START_CHANNEL` | 1 | First DMX channel used |

### Motor / Driver

| Parameter | Default | Description |
|---|---|---|
| `MICROSTEP_MODE` | 128 | TMC2209 microstep resolution |
| `DEFAULT_RUN_CURRENT` | 24 | mA during motion |
| `DEFAULT_HOLD_CURRENT` | 12 | mA when idle |
| `UART_BAUDRATE` | 230400 | TMC2209 UART speed |

### Motion

| Parameter | Default | Description |
|---|---|---|
| `HOME_FIXED_TRAVEL_STEPS` | 20000 | Logical travel span when `HOME_MEASURE_TRAVEL_STEPS` is False |
| `RUNTIME_TRAVEL_STEPS` | 20000 | Usable range during DMX runtime |
| `MOTOR_MAX_SPEED_HZ` | 18684 | Maximum step rate |
| `MOTOR_ACCELERATION_S2` | 300000 | Acceleration in steps/s² |

### DMX Mapping

| Channel | Function |
|---|---|
| CH1 (MSB) + CH2 (LSB) | 16-bit target position (0–65535) |
| CH8 = 255 | Trigger Pico hard reset |

DMX 0 = one end of travel, DMX 65535 = opposite end. Center is DMX 32768.

---

## Getting Started

### 1. Flash MicroPython

Download the latest `.uf2` from [micropython.org/download/rp2-pico/](https://micropython.org/download/rp2-pico/) and flash by holding BOOTSEL and dragging the file onto the Pico.

### 2. Deploy firmware

```bash
cd firmware
./deploy.sh
```

This uploads all `.py` files to the Pico's filesystem on `/dev/ttyACM0`.

### 3. Hard reset

**Important**: after deploying, hard-reset the Pico — old code may persist otherwise:

```bash
mpremote reset
```

### 4. Send DMX

- CH8 < 255: runtime mode active
- CH8 = 255: Pico resets
- CH1+CH2: set target position

The Pico homes on boot (retract → seek endstop via Stallguard → back off → ready).

### Optional: Vision feedback

```bash
# On the Raspberry Pi
./run_camera_stream.sh
# Open http://<pi-ip>:8080/ to view annotated MJPEG stream
# X coordinate pushed to TCP port 9999
```

Connect `nc localhost 9999` to see live X values.

---

## Quirks

- **Jittery Movements**: While static positions work well, movements have random jitters that are not yet understood nor fixed.
- **Hard reset after deploy**: always run `mpremote reset` after uploading firmware
- **DIAG pin optional**: homing uses UART Stallguard, not the physical DIAG pin
- **CH8 = 255 resets**: the Pico watches CH8 and resets on any value of 255

---

## Next Steps

- [ ] Check positional accuracy over time / over reboots.
- [ ] Understand cause for jittery moves and make them smooth
- [ ] Add a second Stepper to this RP2040
- [ ] Web-based or file-based config editing (currently requires code changes)
