"""Minimal on-hardware motor-control smoke test for the Pico firmware stack."""

import time

import config
from motion_axis import MotionAxis
from tmc2209 import TMC2209


def baudrate_candidates():
    candidates = [int(config.UART_BAUDRATE)]
    for baudrate in (115200, 230400):
        if baudrate not in candidates:
            candidates.append(baudrate)
    return candidates


def build_driver(baudrate):
    return TMC2209(
        uart_id=config.UART_ID,
        baudrate=baudrate,
        rx_pin=config.UART_RX_PIN,
        tx_pin=config.UART_TX_PIN,
        driver_address=config.TMC_ADDRESS,
        en_pin=config.EN_PIN,
        diag_pin=config.DIAG_PIN,
    )


def initialize_driver():
    last_error = None
    for baudrate in baudrate_candidates():
        driver = build_driver(baudrate)
        print("[info] trying TMC2209 init at {} baud".format(baudrate))
        try:
            ok = driver.initialize(
                run_current=config.DEFAULT_RUN_CURRENT,
                hold_current=config.DEFAULT_HOLD_CURRENT,
                microsteps=config.MICROSTEP_MODE,
                hold_delay=config.CURRENT_HOLD_DELAY,
            )
            if ok:
                print("[info] TMC2209 init OK at {} baud".format(baudrate))
                return driver, baudrate
            print("[warn] TMC2209 init failed at {} baud".format(baudrate))
        except Exception as exc:
            last_error = exc
            print("[warn] TMC2209 init raised at {} baud: {}".format(baudrate, exc))
        driver.close()

    if last_error is not None:
        print("[error] last init exception:", last_error)
    return None, None


def run_blocking_move(axis, direction, steps, speed_hz, pause_ms=400):
    start_pos = axis.current_position_steps
    sign = "+" if direction > 0 else "-"
    print(
        "[move] dir={} steps={} speed_hz={} start_pos={}".format(
            sign,
            steps,
            speed_hz,
            start_pos,
        )
    )
    moved = axis.move_fixed_steps_blocking(steps, direction, speed_hz)
    print(
        "[move] done moved={} end_pos={} delta={}".format(
            moved,
            axis.current_position_steps,
            axis.current_position_steps - start_pos,
        )
    )
    time.sleep_ms(pause_ms)


def main():
    enable_text = "UART-only" if config.EN_PIN is None else "GP{}".format(config.EN_PIN)
    print("=" * 60)
    print("MOTOR SMOKE TEST")
    print(
        "STEP=GP{} DIR=GP{} EN={} DIAG=GP{} UART{} TX=GP{} RX=GP{}".format(
            config.STEP_PIN,
            config.DIR_PIN,
            enable_text,
            config.DIAG_PIN,
            config.UART_ID,
            config.UART_TX_PIN,
            config.UART_RX_PIN,
        )
    )
    print("=" * 60)

    driver, active_baudrate = initialize_driver()
    if driver is None:
        print("[error] unable to initialize TMC2209 over UART")
        return 1

    axis = MotionAxis(config.STEP_PIN, config.DIR_PIN, step_pulse_us=config.STEP_PULSE_US)

    try:
        driver.set_enabled(True)
        axis.set_enabled(True)
        print("[info] driver enabled, diag={}".format(int(driver.diag_triggered())))
        print("[info] using baudrate {}".format(active_baudrate))

        # First pass: short visible movement in each direction.
        run_blocking_move(axis, 1, 160, 250)
        run_blocking_move(axis, -1, 160, 250)

        # Second pass: a slightly longer burst after a current update.
        run_current = min(31, config.DEFAULT_RUN_CURRENT + 4)
        hold_current = min(31, config.DEFAULT_HOLD_CURRENT + 2)
        if driver.set_run_hold_current(run_current, hold_current, config.CURRENT_HOLD_DELAY):
            print("[info] updated current run={} hold={}".format(run_current, hold_current))
        else:
            print("[warn] current update failed")

        run_blocking_move(axis, 1, 400, 500)
        run_blocking_move(axis, -1, 400, 500)

        # Final pass: leave the axis offset long enough for the camera observer to catch it.
        run_blocking_move(axis, 1, 1600, 350, pause_ms=250)
        print("[hold] holding offset position for 2500 ms")
        time.sleep_ms(2500)
        run_blocking_move(axis, -1, 1600, 350, pause_ms=250)

        print("[info] motor smoke test complete")
        return 0
    finally:
        driver.set_enabled(False)
        axis.set_enabled(False)
        driver.close()
        print("[info] driver disabled")


if __name__ == "__main__":
    main()
