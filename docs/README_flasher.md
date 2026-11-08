# SPV DAC — patcher and flasher

**Language:** English · [Русский](README_flasher.ru.md)

The firmware format and the bootloader protocol were reconstructed from the web application
`hub.moondroplab.tech` (bundle `assets/index-*.js`). The device is a DAC with a RISC-V core and an
"SPV APP Tag" container (magic `SPV@`). The `firmware.bin` and `firmware_prevc.bin` images used in this project
are valid images of this format (verification passes).

## Files
- `web/spv_flasher.html` — WebHID flasher (Chrome/Edge). Replicates the hub's protocol byte for byte.
- `tools/spv_fw_tool.py` — parse / verify / patch the container with checksum recalculation.

## Protocol (short)
- Transport: WebHID, output report `reportId=0`, the whole message in one report, zero-padded.
- Frame: `magic(4)=0x58585542 | cmd(2)=OP|CMD<<8 | seq(1) | flag(1) | len(2) | crc32(4, over the first 10 bytes)` + `[content | crc32(content)]`. CRC32 is standard IEEE.
- Flashing sequence: `handshake(0x0110)` → for each section `setWriteArea(0x0120, addr,len,slice=256)` → 256-byte chunks `writeData(0x0121)` → `endReset(0x0124)`.
- Entering the bootloader from normal mode: an ordinary command `[OP=1, CMD=255]` on the device's `reportId` (75 for Moondrop). The device then re-enumerates as **VID 0x0666 / PID 0x0888** ("USB20") — that is the bootloader.

## How to flash
The page is in Russian. The steps and their buttons:

| Step | Button (Russian label) | Meaning |
|---|---|---|
| 1 | «Образ прошивки (.bin)» file picker | pick the `.bin`: the page parses the sections and verifies the checksums |
| 2 | «Перевести в загрузчик» | **enter the bootloader** (only if the device is in normal mode; pick the reportId, 75) |
| 3 | «Подключить загрузчик (0x0666:0x0888)» | **connect to the bootloader** |
| 4 | «Прошить» | **flash**. The «проверять записанное чтением флеша» checkbox = verify by read-back |
| — | «Снять дамп flash» | **dump flash** (after connecting to the bootloader) |
| — | «Прочитать Flash ID» | read the flash chip ID |

1. Open `web/spv_flasher.html` in Chrome/Edge.
2. Pick the `.bin`.
3. If the device is in normal mode, enter the bootloader. If it is already in the bootloader (`0666:0888`), skip this step:
   the bootloader does not understand that command (you would get `Failed to write the report`).
4. Connect to the bootloader → flash.

> Take a dump of the original from the device first and keep it. The bootloader is recoverable:
> a failed flash leaves the device in `0x0666:0x0888` mode, from which a working image can be flashed.

## Patching (for LED experiments)
Section checksum = sum of its data bytes (its own checksum field counts as 0 if it lies within the range).
`spv_fw_tool.py` recalculates it automatically, so a patched image stays valid.

```
python3 tools/spv_fw_tool.py info  firmware.bin
python3 tools/spv_fw_tool.py patch firmware.bin -o fw_new.bin --offset 0x89d6 --bytes 2f0a03b4
python3 tools/spv_fw_tool.py fixcrc fw_edited.bin -o fw_fixed.bin   # after a manual edit in a hex editor
```

## About the LED
**Outdated here; see `LED_findings.md` and `ARCHITECTURE.md`.** In short: RGB on PWM at `0x20100000`
(pins 3/33/5), not `0x21010000` (that is the ADC). The colours of all modes are changed with `spv_led_colors.py`.

## Dump and entering the bootloader (updated)
- The "dump flash" button (after connecting to the bootloader) reads `0x48000000…+0x40000` and saves a `.bin`.
  The dump contains areas that are not in the image: USB config `0x38000`, settings `0x39bc0`, chip key `0x3e000`.
- "Enter bootloader" lists all HID devices: the DAC's VID in normal mode lives in the flash config
  and is not visible in the image. The firmware expects the report `[75, 1, 255]` (reportId 75), which is what the button sends.
