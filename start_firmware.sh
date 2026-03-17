#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FIRMWARE_DIR="$ROOT_DIR/firmware"
DEVICE="${DEVICE:-/dev/ttyACM0}"

echo "[INFO] Root: $ROOT_DIR"
echo "[INFO] Device: $DEVICE"
echo "[INFO] Uploading firmware from $FIRMWARE_DIR"

cd "$FIRMWARE_DIR"
mpremote connect "$DEVICE" fs cp *.py :

echo "[INFO] Resetting Pico to start firmware"
mpremote connect "$DEVICE" reset

echo "[INFO] Firmware started"
echo "[INFO] The Pico will boot, run homing, and then enter DMX runtime mode"
