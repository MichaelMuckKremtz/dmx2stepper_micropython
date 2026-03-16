"""Minimal TMC2209 UART-velocity smoke test for hardware bring-up."""

import time

import config
from tmc2209 import TMC2209


REG_VACTUAL = 0x22


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
    for baudrate in baudrate_candidates():
        driver = build_driver(baudrate)
        print("[info] trying TMC2209 init at {} baud".format(baudrate))
        try:
            if driver.initialize(
                run_current=min(31, config.DEFAULT_RUN_CURRENT + 4),
                hold_current=min(31, config.DEFAULT_HOLD_CURRENT + 2),
                microsteps=config.MICROSTEP_MODE,
                hold_delay=config.CURRENT_HOLD_DELAY,
            ):
                print("[info] TMC2209 init OK at {} baud".format(baudrate))
                return driver, baudrate
            print("[warn] TMC2209 init failed at {} baud".format(baudrate))
        except Exception as exc:
            print("[warn] TMC2209 init raised at {} baud: {}".format(baudrate, exc))
        driver.close()
    return None, None


def set_velocity(driver, velocity):
    return driver.write_register(REG_VACTUAL, velocity & 0xFFFFFFFF)


def hold_velocity(driver, velocity, duration_ms):
    print("[vel] set {} for {} ms".format(velocity, duration_ms))
    if not set_velocity(driver, velocity):
        print("[warn] VACTUAL write failed for velocity {}".format(velocity))
        return
    time.sleep_ms(duration_ms)
    set_velocity(driver, 0)
    print("[vel] stop")
    time.sleep_ms(700)


def main():
    enable_text = "UART-only" if config.EN_PIN is None else "GP{}".format(config.EN_PIN)
    print("=" * 60)
    print("UART VELOCITY SMOKE TEST")
    print(
        "EN={} DIAG=GP{} UART{} TX=GP{} RX=GP{}".format(
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

    try:
        driver.set_enabled(True)
        print("[info] driver enabled, diag={}".format(int(driver.diag_triggered())))
        print("[info] using baudrate {}".format(active_baudrate))

        hold_velocity(driver, 450, 3000)
        hold_velocity(driver, -450, 3000)
        hold_velocity(driver, 750, 2500)
        hold_velocity(driver, -750, 2500)

        print("[info] UART velocity smoke test complete")
        return 0
    finally:
        set_velocity(driver, 0)
        driver.set_enabled(False)
        driver.close()
        print("[info] driver disabled")


if __name__ == "__main__":
    main()
