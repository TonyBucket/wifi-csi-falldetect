#!/usr/bin/env bash
set -euo pipefail

PORT="${1:-/dev/ttyACM1}"
BAUD="${2:-460800}"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/firmware/rx/build"

echo "Flashing RX to ${PORT}..."
esptool --chip esp32s3 -p "${PORT}" -b "${BAUD}" \
  --before default_reset --after hard_reset write_flash \
  --flash_mode dio --flash_size 16MB --flash_freq 80m \
  0x0 "${DIR}/bootloader/bootloader.bin" \
  0x8000 "${DIR}/partition_table/partition-table.bin" \
  0x10000 "${DIR}/csi_rx.bin"

echo "RX Flash Complete on ${PORT}."
