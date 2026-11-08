#!/bin/sh
# Пример: собрать C-программу из sdk/examples/<имя> поверх базового образа.
# Использование: scripts/build_app.sh rainbow build/fw_custom2.bin build/fw_rainbow.bin
set -e
cd "$(dirname "$0")/.."
python3 -I sdk/spv_build.py "sdk/examples/${1:?имя примера}" --base "${2:?базовый образ}" -o "${3:?выходной образ}" --emu
