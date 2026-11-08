#!/bin/sh
# Пример: цветной образ для DAWN PRO2 — обычный режим зелёный, режим эквалайзера и мигание фиолетовые.
# Использование: scripts/build_custom.sh firmware/firmware.bin build/fw_custom2.bin
set -e
cd "$(dirname "$0")/.."
IN=${1:-firmware/firmware.bin}; OUT=${2:-build/fw_custom2.bin}
mkdir -p "$(dirname "$OUT")"
python3 -I tools/spv_led_colors.py "$IN" -o "$OUT" \
    --invert on --blink-off dark \
    --set all=160,0,255 --set normal8=0,255,0 --check
