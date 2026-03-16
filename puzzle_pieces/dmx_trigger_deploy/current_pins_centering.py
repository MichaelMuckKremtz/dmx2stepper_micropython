"""One-shot PIO sensorless centering run using the historical stepper logic."""

from machine import Pin
import os

from stepper import Stepper


ENABLE_PIN = 2
STEP_PIN = 5
DIR_PIN = 6
MS1_PIN = 3
MS2_PIN = 4
UART_RX_PIN = 1
UART_TX_PIN = 0
MOTOR_ID = 0
UART_BAUDRATE = 230400


def board_type():
    machine_name = os.uname().machine
    if "2040" in machine_name:
        if "W" in machine_name:
            return "RP2040 W", 125_000_000
        return "RP2040", 125_000_000
    if "2350" in machine_name.lower():
        if "W" in machine_name:
            return "RP2350 W", 150_000_000
        return "RP2350", 150_000_000
    return machine_name, 125_000_000


def main():
    board_name, max_pio_frequency = board_type()
    enable_pin = Pin(ENABLE_PIN, Pin.OUT, value=1)
    stepper = None

    print("=" * 72)
    print("PIO SENSORLESS CENTERING TEST")
    print(
        "board={} step=GP{} dir=GP{} en=GP{} diag=GP11 uart0 tx=GP{} rx=GP{} motor_id={}".format(
            board_name,
            STEP_PIN,
            DIR_PIN,
            ENABLE_PIN,
            UART_TX_PIN,
            UART_RX_PIN,
            MOTOR_ID,
        )
    )
    print("=" * 72)

    try:
        enable_pin.value(0)
        print("[info] driver enabled")

        stepper = Stepper(
            max_frequency=max_pio_frequency,
            frequency=5_000_000,
            debug=True,
            step_pin=STEP_PIN,
            dir_pin=DIR_PIN,
            ms1_pin=MS1_PIN,
            ms2_pin=MS2_PIN,
            uart_rx_pin=UART_RX_PIN,
            uart_tx_pin=UART_TX_PIN,
            microstep_index=0,
            motor_id_override=MOTOR_ID,
            uart_baudrate=UART_BAUDRATE,
        )

        if not stepper.tmc_test():
            print("[error] TMC2209 UART test failed")
            return 1

        print("[info] TMC2209 UART test OK")

        for requested_frequency in (400, 2000):
            print("[info] starting centering with requested frequency {} Hz".format(requested_frequency))
            if stepper.centering(requested_frequency):
                print("[result] centering succeeded at requested {} Hz".format(requested_frequency))
                return 0
            print("[warn] centering failed at requested {} Hz".format(requested_frequency))

        print("[result] centering failed on all attempted frequencies")
        return 1
    finally:
        if stepper is not None:
            stepper.stop_stepper()
            stepper.deactivate_pio()
        enable_pin.value(1)
        print("[info] driver disabled")


if __name__ == "__main__":
    main()
