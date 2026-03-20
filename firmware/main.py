"""UART-only homing with PIO steps followed by one-axis DMX runtime."""

import _thread
import json
import machine
import os
import time

import config
from dmx_receiver import DMXReceiver
from pio_stepper import PIOStepper
from tmc2209 import TMC2209


def clamp(value, low, high):
    return max(low, min(high, int(value)))


def board_name():
    return os.uname().machine


def write_json(path, payload):
    with open(path, "w") as handle:
        handle.write(json.dumps(payload))


def map_u16_to_steps(value, span_steps):
    value = clamp(value, 0, 65535)
    span_steps = max(1, int(span_steps))
    return (value * span_steps + 32767) // 65535


def map_u16_to_steps_with_margin(value, span_steps, margin_steps):
    span_steps = max(1, int(span_steps))
    margin_steps = max(0, int(margin_steps))
    usable_low = min(margin_steps, span_steps // 2)
    usable_high = max(usable_low, span_steps - usable_low)
    usable_span = max(0, usable_high - usable_low)
    return usable_low + map_u16_to_steps(value, usable_span)


def resolve_runtime_position_limits(span_steps):
    span_steps = max(1, int(span_steps))
    margin_steps = max(0, int(config.RUNTIME_SOFT_END_MARGIN_STEPS))
    usable_low = min(margin_steps, span_steps // 2)
    usable_high = max(usable_low, span_steps - usable_low)
    return int(usable_low), int(usable_high)


def resolve_runtime_travel_steps(measured_travel_steps):
    measured_travel_steps = max(1, int(measured_travel_steps))
    configured_span = max(0, int(getattr(config, "RUNTIME_TRAVEL_STEPS", 0)))
    if configured_span <= 0:
        return measured_travel_steps, 0
    return min(measured_travel_steps, configured_span), configured_span


def resolve_fixed_home_span_steps():
    configured_runtime_span = max(0, int(getattr(config, "RUNTIME_TRAVEL_STEPS", 0)))
    if configured_runtime_span > 0:
        return configured_runtime_span
    return max(1, int(config.HOME_FIXED_TRAVEL_STEPS))


def stallguard_adjustment(microsteps):
    return max(1.0, float(microsteps) / 8.0)


def microstep_distance_adjustment(microsteps):
    return max(1.0, float(microsteps) / 16.0)


def home_speed_trials():
    adjustment = stallguard_adjustment(config.MICROSTEP_MODE)
    min_freq = int(round(float(config.HOME_MIN_FREQ_1_8_HZ) * adjustment))
    max_freq = int(round(float(config.HOME_MAX_FREQ_1_8_HZ) * adjustment))
    speeds = []
    for requested in config.HOME_SPEED_TRIALS_1_8_HZ:
        scaled = int(float(requested) * adjustment)
        clamped = clamp(scaled, min_freq, max_freq)
        if clamped not in speeds:
            speeds.append(clamped)
    return tuple(speeds)


def scaled_home_steps(base_steps):
    return max(1, int(round(float(base_steps) * microstep_distance_adjustment(config.MICROSTEP_MODE))))


def scaled_home_speed(base_speed_hz):
    return max(1, int(round(float(base_speed_hz) * microstep_distance_adjustment(config.MICROSTEP_MODE))))


def median_int(values):
    ordered = sorted(int(value) for value in values)
    count = len(ordered)
    middle = count // 2
    if count % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) // 2


def derive_uart_threshold(startup_sg_values):
    baseline = median_int(startup_sg_values)
    threshold = int(float(baseline) * float(config.HOME_UART_THRESHOLD_RATIO))
    return clamp(threshold, config.HOME_UART_THRESHOLD_MIN, config.HOME_UART_THRESHOLD_MAX)


def debug_log(message):
    if bool(config.DEBUG_LOGGING):
        print(message)


def append_recent_event(events, event, max_events=32):
    events.append(event)
    if len(events) > int(max_events):
        del events[0]


class SharedDMXState:
    """State shared between the DMX reader thread and the motion loop."""

    def __init__(self):
        self._lock = _thread.allocate_lock()
        self.target_u16 = int(config.DEFAULT_TARGET_U16)
        self.frame_count = 0

    def update_from_channels(self, channels, frame_count):
        self._lock.acquire()
        self.target_u16 = (int(channels[0]) << 8) | int(channels[1])
        self.frame_count = int(frame_count)
        self._lock.release()

    def snapshot(self):
        self._lock.acquire()
        val = self.target_u16
        count = self.frame_count
        self._lock.release()
        return val, count


class ChunkedPositionController:
    """Acceleration-limited position tracking with linear fade detection."""

    def __init__(self, axis, span_steps):
        self.axis = axis
        self.span_steps = max(1, int(span_steps))
        self.current_position_steps = self.span_steps // 2
        self.target_position_steps = self.current_position_steps
        self.current_speed_hz = 0.0
        self.max_speed_hz = float(config.MOTOR_MAX_SPEED_HZ)
        self.acceleration_steps_s2 = float(config.MOTOR_ACCELERATION_S2)
        self.enabled = True
        self._last_update_ms = time.ticks_ms()
        self._step_accumulator = 0.0
        self._last_target_u16 = config.DEFAULT_TARGET_U16
        self._last_applied_target_u16 = None

        self._dmx_history = [(0, 0)] * config.LINEAR_FADE_WINDOW
        self._dmx_history_idx = 0
        self._dmx_history_full = False
        self._fade_velocity_hz = 0.0
        self._fade_direction = 0
        self._is_linear_fade = False
        self._fade_blend_ms = 0.0
        self._prev_was_linear = False

    def hold_position(self):
        self.target_position_steps = int(self.current_position_steps)
        self.current_speed_hz = 0.0
        self._step_accumulator = 0.0
        self._last_update_ms = time.ticks_ms()
        self._reset_dmx_history()

    def _reset_dmx_history(self):
        self._dmx_history = [(0, 0)] * config.LINEAR_FADE_WINDOW
        self._dmx_history_idx = 0
        self._dmx_history_full = False
        self._is_linear_fade = False
        self._fade_velocity_hz = 0.0

    def apply_snapshot(self, snapshot_target_u16):
        if self._last_applied_target_u16 == snapshot_target_u16:
            return
        self._last_applied_target_u16 = snapshot_target_u16

        new_target = int(
            map_u16_to_steps_with_margin(
                snapshot_target_u16,
                self.span_steps,
                config.RUNTIME_SOFT_END_MARGIN_STEPS,
            )
        )
        at_target = (int(self.current_position_steps) == int(self.target_position_steps)
                     and abs(self.current_speed_hz) < 1.0)
        if at_target and abs(new_target - self.current_position_steps) <= int(config.RUNTIME_POSITION_DEADBAND_STEPS):
            pass
        else:
            self.target_position_steps = new_target

    def _detect_linear_fade(self, target_u16, now_ms):
        self._dmx_history[self._dmx_history_idx] = (now_ms, target_u16)
        self._dmx_history_idx = (self._dmx_history_idx + 1) % config.LINEAR_FADE_WINDOW
        if self._dmx_history_idx == 0:
            self._dmx_history_full = True

        min_samples = 4
        if not self._dmx_history_full and self._dmx_history_idx < min_samples:
            self._is_linear_fade = False
            return

        history = []
        for i in range(min_samples):
            idx = (self._dmx_history_idx - min_samples + i) % config.LINEAR_FADE_WINDOW
            history.append(self._dmx_history[idx])

        if history[0][0] == 0:
            self._is_linear_fade = False
            return

        deltas = []
        for i in range(1, len(history)):
            dt = history[i][0] - history[i-1][0]
            du = history[i][1] - history[i-1][1]
            if dt > 0:
                deltas.append(du / dt)

        if len(deltas) < min_samples - 1:
            self._is_linear_fade = False
            return

        avg_delta = sum(deltas) / len(deltas)

        if abs(avg_delta) < 0.5:
            self._is_linear_fade = False
            self._fade_velocity_hz = 0.0
            return

        variance = sum((d - avg_delta) ** 2 for d in deltas) / len(deltas)
        same_sign = all((d > 0) == (avg_delta > 0) for d in deltas if abs(d) > 0.5)

        if variance < config.LINEAR_FADE_VARIANCE_THRESH and same_sign:
            self._is_linear_fade = True
            u16_per_ms = avg_delta
            steps_per_u16 = self.span_steps / 65535.0
            self._fade_velocity_hz = abs(u16_per_ms) * 1000.0 * steps_per_u16
            self._fade_direction = 1 if avg_delta > 0 else -1

            if self._fade_velocity_hz < config.LINEAR_FADE_MIN_STEPS_SEC:
                self._is_linear_fade = False
                self._fade_velocity_hz = 0.0
        else:
            self._is_linear_fade = False
            self._fade_velocity_hz = 0.0

    def _approach(self, current, target, delta):
        if current < target:
            return min(target, current + max(0, delta))
        if current > target:
            return max(target, current - max(0, delta))
        return target

    def update(self, target_u16=0):
        now_ms = time.ticks_ms()
        elapsed_ms = time.ticks_diff(now_ms, self._last_update_ms)
        if elapsed_ms <= 0:
            return 0
        self._last_update_ms = now_ms

        if not self.enabled:
            self.current_speed_hz = 0.0
            self._step_accumulator = 0.0
            return 0

        self._detect_linear_fade(target_u16, now_ms)

        distance = int(self.target_position_steps) - int(self.current_position_steps)

        if distance == 0 and abs(self.current_speed_hz) < 1.0:
            self.current_speed_hz = 0.0
            self._step_accumulator = 0.0
            return 0

        tracking_deadband = int(config.POSITION_TRACKING_DEADBAND)
        if abs(distance) <= tracking_deadband and abs(self.current_speed_hz) < float(config.VELOCITY_DEADBAND_HZ):
            self.current_speed_hz = 0.0
            self._step_accumulator = 0.0
            return 0

        elapsed_s = elapsed_ms / 1000.0
        direction = 0
        if distance > 0:
            direction = 1
        elif distance < 0:
            direction = -1

        if self._is_linear_fade and direction != 0:
            max_chunk = min(config.LINEAR_FADE_CHUNK_STEPS, int(config.RUNTIME_MAX_CHUNK_STEPS))
        else:
            max_chunk = int(config.RUNTIME_MAX_CHUNK_STEPS)

        desired_speed = 0.0
        if direction != 0:
            moving_direction = 0
            if self.current_speed_hz > 0:
                moving_direction = 1
            elif self.current_speed_hz < 0:
                moving_direction = -1

            if moving_direction != 0 and moving_direction != direction:
                desired_speed = 0.0
            else:
                stop_distance = 0.0
                if self.acceleration_steps_s2 > 0:
                    stop_distance = (self.current_speed_hz * self.current_speed_hz) / (2.0 * self.acceleration_steps_s2)

                if abs(distance) <= stop_distance:
                    desired_speed = direction * min(
                        self.max_speed_hz,
                        max(1.0, (2.0 * self.acceleration_steps_s2 * abs(distance)) ** 0.5),
                    )
                else:
                    desired_speed = direction * self.max_speed_hz

        max_delta = self.acceleration_steps_s2 * elapsed_s
        self.current_speed_hz = self._approach(self.current_speed_hz, desired_speed, max_delta)

        self._step_accumulator += abs(self.current_speed_hz) * elapsed_s
        steps_due = int(self._step_accumulator)
        if steps_due <= 0:
            return 0

        remaining = abs(int(self.target_position_steps) - int(self.current_position_steps))
        if remaining <= 0:
            self._step_accumulator = 0.0
            return 0

        steps_to_take = min(steps_due, remaining, max_chunk)
        if steps_to_take <= 0:
            return 0

        effective_speed = int(max(config.RUNTIME_MIN_CHUNK_SPEED_HZ, min(abs(self.current_speed_hz), self.max_speed_hz)))
        moved = int(
            self.axis.move_fixed_steps_blocking(
                steps_to_take,
                direction,
                effective_speed,
                poll_ms=1,
            )
        )
        self.current_position_steps += moved if direction > 0 else -moved
        self._step_accumulator -= moved
        if moved < steps_to_take:
            self.current_speed_hz = 0.0
            self._step_accumulator = 0.0
        return moved


def build_driver():
    return TMC2209(
        uart_id=config.UART_ID,
        baudrate=config.UART_BAUDRATE,
        rx_pin=config.UART_RX_PIN,
        tx_pin=config.UART_TX_PIN,
        driver_address=config.TMC_ADDRESS,
        en_pin=config.EN_PIN,
        diag_pin=config.DIAG_PIN,
    )


def configure_driver(driver):
    if not driver.initialize(
        run_current=config.DEFAULT_RUN_CURRENT,
        hold_current=config.DEFAULT_HOLD_CURRENT,
        microsteps=config.MICROSTEP_MODE,
        hold_delay=config.CURRENT_HOLD_DELAY,
    ):
        return False
    return driver.set_driver_enabled_via_uart(True, fallback_toff=config.DRIVER_ENABLE_TOFF)


def build_axis(step_pin, dir_pin, axis_slot):
    axis_slot = max(0, int(axis_slot))
    return PIOStepper(
        step_pin=step_pin,
        dir_pin=dir_pin,
        step_sm_id=config.PIO_STEP_SM_ID + (axis_slot * 2),
        counter_sm_id=config.PIO_COUNTER_SM_ID + (axis_slot * 2),
        step_frequency=config.PIO_STEP_FREQUENCY,
        counter_frequency=config.PIO_COUNTER_FREQUENCY,
    )


def seek_endstop_uart(driver, axis, direction, speed_hz, label):
    max_home_steps = scaled_home_steps(config.HOME_MAX_STEPS)
    min_stall_steps = scaled_home_steps(config.HOME_MIN_STALL_STEPS)
    timeout_ms = int((max_home_steps * 1000) / max(1.0, float(speed_hz))) + config.HOME_TIMEOUT_MARGIN_MS
    status = {
        "label": label,
        "direction": int(direction),
        "speed_hz": int(speed_hz),
        "max_home_steps": int(max_home_steps),
        "search_steps": 0,
        "search_elapsed_ms": 0,
        "last_sg": None,
        "sg_history": [],
        "startup_sg_values": [],
        "startup_sg_samples": 0,
        "uart_threshold": None,
        "low_sg_peak": 0,
        "stop_reason": "not_started",
        "success": False,
    }
    last_status_ms = -config.PRINT_INTERVAL_MS
    low_sg_count = 0

    if not driver.set_coolstep_threshold(config.HOME_COOLSTEP_THRESHOLD):
        status["stop_reason"] = "coolstep_config_failed"
        return status
    driver.set_stallguard_threshold(0)

    def stop_fn(steps, elapsed_ms):
        nonlocal last_status_ms, low_sg_count

        sg = driver.read_stallguard_result()
        if sg is not None:
            sg = int(sg)
            status["last_sg"] = sg
            history = status["sg_history"]
            history.append(sg)
            if len(history) > 12:
                del history[0]

            if status["startup_sg_samples"] < config.HOME_STARTUP_SG_SAMPLES:
                status["startup_sg_samples"] += 1
                status["startup_sg_values"].append(sg)
                if status["startup_sg_samples"] == config.HOME_STARTUP_SG_SAMPLES:
                    status["uart_threshold"] = int(derive_uart_threshold(status["startup_sg_values"]))
                    debug_log(
                        "[seek:{}] armed uart threshold {} from startup median {}".format(
                            label,
                            status["uart_threshold"],
                            median_int(status["startup_sg_values"]),
                        )
                    )
            elif steps >= min_stall_steps:
                threshold = int(status["uart_threshold"])
                if sg <= threshold:
                    low_sg_count += 1
                    if low_sg_count > status["low_sg_peak"]:
                        status["low_sg_peak"] = int(low_sg_count)
                else:
                    low_sg_count = 0

                if low_sg_count >= config.HOME_UART_CONFIRM_POLLS:
                    return "uart_stall"

        if elapsed_ms - last_status_ms >= config.PRINT_INTERVAL_MS:
            debug_log(
                "[seek:{}] elapsed={}ms steps={} sg={} low_hits={} thr={}".format(
                    label,
                    elapsed_ms,
                    steps,
                    status["last_sg"],
                    low_sg_count,
                    status["uart_threshold"],
                )
            )
            last_status_ms = elapsed_ms

        return None

    search = axis.run_until(
        direction=direction,
        speed_hz=speed_hz,
        max_steps=max_home_steps,
        stop_fn=stop_fn,
        poll_ms=config.HOME_POLL_MS,
        timeout_ms=timeout_ms,
    )

    status["search_steps"] = int(search["steps"])
    status["search_elapsed_ms"] = int(search["elapsed_ms"])
    status["stop_reason"] = search["stop_reason"]
    status["success"] = search["stop_reason"] == "uart_stall"

    debug_log(
        "[seek:{}] result success={} stop_reason={} steps={} elapsed={}ms".format(
            label,
            int(status["success"]),
            status["stop_reason"],
            status["search_steps"],
            status["search_elapsed_ms"],
        )
    )
    time.sleep_ms(config.HOME_SETTLE_MS)
    return status


def run_centering_trial(driver, step_pin, dir_pin, axis_slot, home_direction, speed_hz, trial_index):
    axis = build_axis(step_pin, dir_pin, axis_slot)
    home_retract_steps = scaled_home_steps(config.HOME_RETRACT_STEPS)
    home_release_steps = scaled_home_steps(config.HOME_RELEASE_STEPS)
    fixed_travel_steps = resolve_fixed_home_span_steps()
    home_retract_speed_hz = scaled_home_speed(config.HOME_RETRACT_SPEED_HZ)
    home_release_speed_hz = scaled_home_speed(config.HOME_RELEASE_SPEED_HZ)
    status = {
        "trial_index": int(trial_index),
        "axis_slot": int(axis_slot),
        "step_pin": int(step_pin),
        "dir_pin": int(dir_pin),
        "home_direction": int(home_direction),
        "speed_hz": int(speed_hz),
        "retract_steps": 0,
        "release_steps": 0,
        "second_release_steps": 0,
        "first_end": None,
        "second_end": None,
        "travel_steps": 0,
        "measured_travel_steps": 0,
        "configured_runtime_travel_steps": max(0, int(getattr(config, "RUNTIME_TRAVEL_STEPS", 0))),
        "runtime_travel_steps": 0,
        "center_steps_requested": 0,
        "center_steps_moved": 0,
        "center_direction": int(home_direction),
        "centered": False,
        "stop_reason": "not_started",
        "success": False,
    }

    debug_log(
        "[trial] index={} slot={} step=GP{} dir=GP{} home_dir={} speed={}Hz".format(
            trial_index,
            axis_slot,
            step_pin,
            dir_pin,
            home_direction,
            speed_hz,
        )
    )

    try:
        if home_retract_steps > 0:
            status["retract_steps"] = int(
                axis.move_fixed_steps_blocking(
                    home_retract_steps,
                    -home_direction,
                    home_retract_speed_hz,
                    poll_ms=config.HOME_POLL_MS,
                )
            )
            debug_log("[trial] retract completed steps={}".format(status["retract_steps"]))
            time.sleep_ms(config.HOME_SETTLE_MS)

        first_end = seek_endstop_uart(driver, axis, home_direction, speed_hz, "first_end")
        status["first_end"] = first_end
        if not first_end["success"]:
            status["stop_reason"] = "first_end_" + first_end["stop_reason"]
            return status

        release_target_steps = int(home_release_steps)
        if release_target_steps > 0:
            status["release_steps"] = int(
                axis.move_fixed_steps_blocking(
                    release_target_steps,
                    -home_direction,
                    home_release_speed_hz,
                    poll_ms=config.HOME_POLL_MS,
                )
            )
            debug_log("[trial] release completed steps={}".format(status["release_steps"]))
            time.sleep_ms(config.HOME_SETTLE_MS)

        measured_travel_enabled = bool(getattr(config, "HOME_MEASURE_TRAVEL_STEPS", True))
        if measured_travel_enabled:
            second_end = seek_endstop_uart(driver, axis, -home_direction, speed_hz, "second_end")
            status["second_end"] = second_end
            if not second_end["success"]:
                status["stop_reason"] = "second_end_" + second_end["stop_reason"]
                return status

            measured_travel_steps = int(second_end["search_steps"]) + int(status["release_steps"])
        else:
            measured_travel_steps = int(fixed_travel_steps)

        if measured_travel_steps < int(config.HOME_MIN_TRAVEL_STEPS):
            status["stop_reason"] = "travel_too_small"
            status["measured_travel_steps"] = int(measured_travel_steps)
            return status

        runtime_travel_steps, configured_runtime_travel_steps = resolve_runtime_travel_steps(measured_travel_steps)
        status["measured_travel_steps"] = int(measured_travel_steps)
        status["configured_runtime_travel_steps"] = int(configured_runtime_travel_steps)
        status["runtime_travel_steps"] = int(runtime_travel_steps)
        status["travel_steps"] = int(runtime_travel_steps)
        runtime_min_position_steps, runtime_max_position_steps = resolve_runtime_position_limits(runtime_travel_steps)
        status["runtime_min_position_steps"] = int(runtime_min_position_steps)
        status["runtime_max_position_steps"] = int(runtime_max_position_steps)

        status["initial_position_steps"] = int(status["release_steps"])
        status["centered"] = True
        status["success"] = True
        status["stop_reason"] = "backed_up"

        debug_log(
            "[trial] measured_travel={} runtime_travel={} center_requested={} center_moved={} success={}".format(
                status["measured_travel_steps"],
                status["travel_steps"],
                status["center_steps_requested"],
                status["center_steps_moved"],
                int(status["success"]),
            )
        )
        return status
    finally:
        axis.deinit()


def run_homing(driver, result):
    trial_index = 0
    speed_trials = home_speed_trials()

    for axis_slot, (step_pin, dir_pin) in enumerate(config.STEP_DIR_TRIALS):
        for home_direction in config.HOME_DIRECTION_TRIALS:
            for speed_hz in speed_trials:
                trial = run_centering_trial(
                    driver,
                    step_pin,
                    dir_pin,
                    axis_slot,
                    home_direction,
                    speed_hz,
                    trial_index,
                )
                result["trials"].append(trial)
                write_json(config.RESULT_FILE, result)
                if trial["success"]:
                    result["success"] = True
                    result["centered"] = True
                    result["selected_trial"] = int(trial_index)
                    result["selected_axis_slot"] = int(axis_slot)
                    result["stop_reason"] = trial["stop_reason"]
                    return trial
                trial_index += 1

    return None


def dmx_worker(shared):
    dmx = DMXReceiver(pin_num=config.DMX_PIN, sm_id=config.DMX_SM_ID)
    dmx.start()
    while True:
        frame_received = dmx.read_frame()
        if not frame_received:
            continue
        if dmx.last_start_code != 0x00:
            continue
        channels = dmx.get_channels(config.DMX_START_CHANNEL, 8)
        if int(channels[7]) == 255:
            machine.reset()
        shared.update_from_channels(channels, dmx.get_frame_count())


def build_runtime_status(
    result,
    homing_trial,
    controller,
    target_u16,
    stable_target_since_ms,
    idle_since_ms,
    total_steps_emitted,
    last_step_ms,
):
    return {
        "runtime_active": True,
        "board": result["board"],
        "selected_trial": result["selected_trial"],
        "selected_axis_slot": result["selected_axis_slot"],
        "step_pin": homing_trial["step_pin"],
        "dir_pin": homing_trial["dir_pin"],
        "home_direction": homing_trial["home_direction"],
        "travel_steps": homing_trial["travel_steps"],
        "measured_travel_steps": int(homing_trial.get("measured_travel_steps", homing_trial["travel_steps"])),
        "configured_runtime_travel_steps": int(homing_trial.get("configured_runtime_travel_steps", 0)),
        "runtime_travel_steps": int(homing_trial.get("runtime_travel_steps", homing_trial["travel_steps"])),
        "runtime_min_position_steps": int(homing_trial.get("runtime_min_position_steps", 0)),
        "runtime_max_position_steps": int(homing_trial.get("runtime_max_position_steps", homing_trial["travel_steps"])),
        "current_position_steps": int(controller.current_position_steps),
        "target_position_steps": int(controller.target_position_steps),
        "current_speed_hz": round(float(controller.current_speed_hz), 3),
        "target_u16": int(target_u16),
        "stable_target_since_ms": stable_target_since_ms,
        "idle_since_ms": idle_since_ms,
        "total_steps_emitted": int(total_steps_emitted),
        "last_step_ms": last_step_ms,
    }


def main():
    result = {
        "status": "running",
        "success": False,
        "centered": False,
        "runtime_ready": False,
        "board": board_name(),
        "microsteps": int(config.MICROSTEP_MODE),
        "run_current": int(config.DEFAULT_RUN_CURRENT),
        "hold_current": int(config.DEFAULT_HOLD_CURRENT),
        "step_dir_trials": [list(pair) for pair in config.STEP_DIR_TRIALS],
        "home_direction_trials": [int(value) for value in config.HOME_DIRECTION_TRIALS],
        "home_speed_trials_hz": list(home_speed_trials()),
        "trials": [],
        "selected_trial": None,
        "selected_axis_slot": None,
        "result_file": config.RESULT_FILE,
        "status_file": config.STATUS_FILE,
        "home_measure_travel_steps": bool(getattr(config, "HOME_MEASURE_TRAVEL_STEPS", True)),
        "configured_runtime_travel_steps": max(0, int(getattr(config, "RUNTIME_TRAVEL_STEPS", 0))),
        "runtime_min_position_steps": int(resolve_runtime_position_limits(max(1, int(getattr(config, "RUNTIME_TRAVEL_STEPS", config.HOME_FIXED_TRAVEL_STEPS))))[0]),
        "runtime_max_position_steps": int(resolve_runtime_position_limits(max(1, int(getattr(config, "RUNTIME_TRAVEL_STEPS", config.HOME_FIXED_TRAVEL_STEPS))))[1]),
    }
    write_json(config.RESULT_FILE, result)

    driver = build_driver()
    runtime_axis = None
    controller = None

    debug_log("=" * 72)
    debug_log("UART HOMING + ONE-AXIS DMX RUNTIME")
    debug_log("board={}".format(result["board"]))
    debug_log(
        "uart0 tx=GP{} rx=GP{} dmx=GP{} sm={} microsteps={} run={} hold={} en=UART-only".format(
            config.UART_TX_PIN,
            config.UART_RX_PIN,
            config.DMX_PIN,
            config.DMX_SM_ID,
            config.MICROSTEP_MODE,
            config.DEFAULT_RUN_CURRENT,
            config.DEFAULT_HOLD_CURRENT,
        )
    )
    debug_log("=" * 72)

    try:
        if not configure_driver(driver):
            result["status"] = "done"
            result["stop_reason"] = "driver_init_failed"
            write_json(config.RESULT_FILE, result)
            debug_log("[error] TMC2209 initialization or UART enable failed")
            driver.set_driver_enabled_via_uart(False, fallback_toff=config.DRIVER_ENABLE_TOFF)
            return

        homing_trial = run_homing(driver, result)
        result["status"] = "done"
        if homing_trial is None:
            result["stop_reason"] = "all_trials_failed"
            write_json(config.RESULT_FILE, result)
            driver.set_driver_enabled_via_uart(False, fallback_toff=config.DRIVER_ENABLE_TOFF)
            debug_log("[result] homing failed on all trials")
            return

        result["runtime_ready"] = True
        write_json(config.RESULT_FILE, result)

        if not config.RUN_RUNTIME_AFTER_HOMING:
            debug_log("[result] homing complete; runtime disabled by configuration")
            return

        runtime_axis = build_axis(
            homing_trial["step_pin"],
            homing_trial["dir_pin"],
            homing_trial["axis_slot"],
        )
        controller = ChunkedPositionController(runtime_axis, homing_trial["travel_steps"])
        initial_pos = int(homing_trial.get("initial_position_steps", controller.span_steps // 2))
        controller.current_position_steps = initial_pos
        controller.target_position_steps = initial_pos

        shared = SharedDMXState()
        _thread.start_new_thread(dmx_worker, (shared,))

        last_status_ms = -config.STATUS_INTERVAL_MS
        runtime_start_ms = time.ticks_ms()
        stable_target_since_ms = None
        idle_since_ms = None
        total_steps_emitted = 0
        last_step_ms = None

        debug_log(
            "[runtime] selected_trial={} step=GP{} dir=GP{} home_dir={} travel_steps={}".format(
                result["selected_trial"],
                homing_trial["step_pin"],
                homing_trial["dir_pin"],
                homing_trial["home_direction"],
                homing_trial["travel_steps"],
            )
        )

        while True:
            now_ms = time.ticks_ms()
            target_u16, frame_count = shared.snapshot()
            controller.apply_snapshot(target_u16)

            if target_u16 != controller._last_target_u16:
                stable_target_since_ms = int(now_ms)
                controller._last_target_u16 = target_u16

            moved = controller.update(target_u16)
            if moved > 0:
                total_steps_emitted += int(moved)
                last_step_ms = int(now_ms)

            at_target_after = (
                int(controller.current_position_steps) == int(controller.target_position_steps)
                and abs(float(controller.current_speed_hz)) < 1.0
            )
            if at_target_after:
                if idle_since_ms is None:
                    idle_since_ms = int(now_ms)
            else:
                idle_since_ms = None

            if (
                bool(config.RUNTIME_STATUS_STREAM_ENABLED)
                and int(config.STATUS_INTERVAL_MS) > 0
                and time.ticks_diff(now_ms, last_status_ms) >= config.STATUS_INTERVAL_MS
            ):
                write_json(
                    config.STATUS_FILE,
                    build_runtime_status(
                        result,
                        homing_trial,
                        controller,
                        target_u16,
                        stable_target_since_ms,
                        idle_since_ms,
                        total_steps_emitted,
                        last_step_ms,
                    ),
                )
                last_status_ms = now_ms

            if (
                int(config.RUNTIME_EXIT_AFTER_MS) > 0
                and time.ticks_diff(now_ms, runtime_start_ms) >= int(config.RUNTIME_EXIT_AFTER_MS)
            ):
                debug_log("[runtime] exiting after configured runtime window")
                return

            time.sleep_ms(config.RUNTIME_CONTROL_SLEEP_MS)
    finally:
        if runtime_axis is not None:
            runtime_axis.deinit()


if __name__ == "__main__":
    main()
