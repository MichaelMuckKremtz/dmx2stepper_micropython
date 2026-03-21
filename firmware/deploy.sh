#!/usr/bin/env bash
set -euo pipefail

DEVICE="${1:-/dev/ttyACM0}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FIRMWARE_DIR="${SCRIPT_DIR}"

cd "${FIRMWARE_DIR}"

echo ""
echo "WARNING: This will WIPE all files on ${DEVICE} and replace them"
echo "         with the current contents of this folder."
echo ""

read -r -p "Continue? [y/N] " response
if [[ ! "$response" =~ ^[Yy]$ ]]; then
    echo "Aborted."
    exit 1
fi

echo ""
echo "Deleting existing files on ${DEVICE}..."
mpremote connect "${DEVICE}" exec "import os; [os.remove(f) for f in os.listdir() if f not in ('boot.py',)]"
echo "Done."

echo ""
echo "Uploading firmware..."
mpremote connect "${DEVICE}" fs cp config.py dmx_receiver.py pio_stepper.py tmc2209_uart.py tmc2209.py main.py :
echo "Done."

echo ""
echo "Restarting ${DEVICE}..."
mpremote connect "${DEVICE}" reset

echo ""
echo -e "  \e[36m╔════════════════════════════════╗\e[0m"
echo -e "  \e[36m║  \e[32m████\e[0m                        \e[36m║\e[0m"
echo -e "  \e[36m║  \e[32m████\e[37m▓▓▓▓▓▓\e[33m▒▒▒▒▒▒\e[32m██\e[0m        \e[36m║\e[0m"
echo -e "  \e[36m║  \e[32m████\e[37m▓▓▓▓▓▓\e[33m▒▒▒▒▒▒\e[32m██\e[37m▓▓▓▓\e[33m▒▒\e[32m██\e[0m  \e[36m║\e[0m"
echo -e "  \e[36m║  \e[32m████\e[37m▓▓▓▓▓▓\e[33m▒▒▒▒▒▒\e[32m██\e[37m▓▓▓▓\e[33m▒▒\e[32m██\e[0m  \e[36m║\e[0m"
echo -e "  \e[36m║  \e[32m████\e[37m▓▓▓▓▓▓\e[33m▒▒▒▒▒▒\e[32m██\e[37m▓▓▓▓\e[33m▒▒\e[32m██\e[0m  \e[36m║\e[0m"
echo -e "  \e[36m║  \e[32m████\e[37m▓▓▓▓▓▓\e[33m▒▒▒▒▒▒\e[32m██\e[37m▓▓▓▓\e[33m▒▒\e[32m██\e[0m  \e[36m║\e[0m"
echo -e "  \e[36m║  \e[32m████\e[37m▓▓▓▓▓▓\e[33m▒▒▒▒▒▒\e[32m████\e[0m        \e[36m║\e[0m"
echo -e "  \e[36m║  \e[32m████████████████\e[37m██\e[33m██\e[32m████████\e[0m  \e[36m║\e[0m"
echo -e "  \e[36m║  \e[32m████████████████\e[37m██\e[33m██\e[32m████████\e[0m  \e[36m║\e[0m"
echo -e "  \e[36m╚════════════════════════════════╝\e[0m"
echo -e "         \e[35m◉\e[0m   \e[1;37mFirmware deployed.\e[0m"
echo ""
