# Moondrop DAWN PRO 2 — firmware RE & dev SDK

**Language:** English · [Русский](README.ru.md)

Reverse engineering of the **Moondrop DAWN PRO 2** USB DAC firmware (SPACETOUCH SPV4040, Andes RISC-V core) and tools to modify it:
an emulator, a browser-based flasher, LED colour patches and an **SDK for writing your own C programs that run inside the DAC**.

The interesting part: on this DAC **the audio samples go through the CPU** (USB → software EQ → DAC). So you can hook the audio path
and run your own DSP. The `bubble` example does exactly that: a "warm" sound effect plus a soft bubbling sound when you press the volume buttons.

> # ⚠️ DISCLAIMER
>
> ## **THIS PROJECT IS VIBE-CODED: ALL CODE, REVERSE ENGINEERING AND DOCUMENTATION WERE PRODUCED BY AN AI (Claude) DURING A CONVERSATION.**
>
> ## **USE ENTIRELY AT YOUR OWN RISK.**
>
> - **No human has reviewed the code. There are no tests beyond runs in a home-made emulator and a few manual checks on a single device.**
> - **Findings about the firmware (addresses, behaviour, "what a function does") come from automated analysis and may be wrong.**
> - **Flashing can damage your device (up to bricking it). Neither the author nor the AI accepts any liability for any damage.**
> - This project is not affiliated with or endorsed by Moondrop. Vendor firmware images are **not published** here: you work with your own copy and your own device.
> - Read [Risks](#risks) first.

## What works (verified on hardware)

- 🔌 **WebHID flasher** in the browser (`web/spv_flasher.html`): enter the bootloader, write, verify by read-back, dump flash.
- 🎨 **Custom LED colours** in every mode (`tools/spv_led_colors.py`): normal mode, EQ mode, blinking, volume limit.
  Fixes the PWM polarity and the "off" phase of blinking (the LED is common-anode).
- 🧑‍💻 **C SDK** (`sdk/`): `app_init()` / `app_loop()` running inside the stock firmware, timers, LED control, a debug log.
  Example: a rainbow on the LED (`sdk/examples/rainbow`) runs on a real device.
- 🔊 **Custom sound** (`sdk/examples/bubble`): the program hooks into the audio path (USB → EQ → DAC). It adds a "warm" effect
  (bass shelf, headphone crossfeed, soft limiter) and a **soft bubbling sound on the "+"/"−" volume buttons**.
  Double-click "−" to toggle the effect. Verified on hardware.
- 🧪 **Emulator** for RV32IMAC + F + Andes extensions (AndeStar V5, DSP): the firmware boots to its main loop, so you can test an image before flashing it.

## Quick start

You need: Linux, Python 3, `gcc-riscv64-unknown-elf` (for the SDK; Debian/Ubuntu: `sudo apt install gcc-riscv64-unknown-elf`), Chrome/Edge for the flasher.

```sh
git clone https://github.com/muxfunhome-droid/moondrop-dawn-pro-2-dev-sdk && cd moondrop-dawn-pro-2-dev-sdk

# 1. put YOUR image into firmware/ (build "Nov 17 2025 13:50:18")
cp ~/path/to/firmware.bin firmware/firmware.bin
python3 tools/spv_fw_tool.py info firmware/firmware.bin       # parse and verify checksums

# 2. browser access to the device (once), then re-plug the DAC
sudo cp udev/70-moondrop-dac.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger

# 3. colour image: normal mode green, EQ mode and blinking purple
scripts/build_custom.sh firmware/firmware.bin build/fw_custom2.bin

# 4. (optional) your own C program on top of it
scripts/build_app.sh rainbow build/fw_custom2.bin build/fw_rainbow.bin
scripts/build_app.sh bubble  build/fw_custom2.bin build/fw_bubble.bin    # sound effect + bubbles on the buttons

# 5. open web/spv_flasher.html in Chrome, pick the image, "enter bootloader" → "connect bootloader" → "flash"
#    (if the device is already in the bootloader, 0666:0888, skip "enter bootloader")
```

Before the first flash, take a **flash dump** with the button in the flasher and keep it. Flash the unmodified original first
(to test the whole chain), then the patched images.

> The flasher UI and the code comments are in Russian. The button labels are explained in [`docs/README_flasher.md`](docs/README_flasher.md).

## Layout

| Path | What |
|---|---|
| `tools/spv_fw_tool.py` | parser for the "SPV APP Tag" container: verify and patch with checksum recalculation |
| `tools/spv_led_colors.py` | LED colours, PWM inversion, blink phase, "preset 8" mode; `--check` runs the result in the emulator |
| `tools/spv_emu.py` | emulator and disassembler |
| `tools/led_run.py`, `tools/led_probe.py` | emulator harness (boot-ROM stubs, chip key) and LED scenarios |
| `web/spv_flasher.html` | WebHID flasher (Chrome/Edge) |
| `sdk/` | C SDK: `include/spv.h`, runtime `rt/`, builder `spv_build.py`, `examples/` |
| `udev/` | access rule for the DAC and its bootloader (`0666:0888`) |
| `scripts/` | build examples |
| `docs/` | detailed documentation (below) |
| `firmware/` | your images go here (ignored by git) |

## Documentation

- [`docs/WORKLOG.md`](docs/WORKLOG.md) — what was done, how it works, a list of every firmware change and what was verified on hardware.
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — firmware map: memory, peripherals, functions, audio path, buttons, emulator, patch recipe.
- [`docs/LED_findings.md`](docs/LED_findings.md) — how the LED works: pins, PWM, polarity, events.
- [`docs/README_flasher.md`](docs/README_flasher.md) — image format and bootloader protocol.
- [`sdk/README.md`](sdk/README.md) — how to write C programs.

Every document has a Russian original next to it (`*.ru.md`). The documents were written while the work was in progress, so
`ARCHITECTURE.md` and others name the Python files without the `tools/` prefix.

## What is known about the device

| | |
|---|---|
| USB | normal mode `35d8:011d`, bootloader `0666:0888` ("USB20"); enter the bootloader with HID report `[75, 1, 255]` |
| Core | Andes RISC-V (RV32IMAC + F + Andes extensions), 360 MHz |
| Memory | code runs from SPI flash (XIP at `0x48000000`) plus an ITCM copy of part of the code at address 0; RAM `0x100000…0x140000` |
| Audio | PCM from USB is processed by the CPU: int32 samples, 10-band biquad EQ in software, then to the DAC |
| Buttons | "+" = pin 8, "−" = pin 34 (active low), handled by a MultiButton-style library |
| LED | RGB on PWM, pins R3 / G33 / B5, **common anode** (duty 0 = lit) |
| "Normal mode" | EQ preset 8 (factory); "EQ mode" = preset ≠ 8 |
| Chip binding | die ID XOR `"Ttg?Tech"` is compared with a key in flash at `0x3e000` (not part of the image) |

## Risks

- Only the firmware build **"Nov 17 2025 13:50:18"** is supported. The scripts check the build date and the original bytes and refuse other images.
- Never block in your program's `app_loop()`: the HID command that enters the bootloader is handled in the main loop. Keep a dump and a known-good image at hand.
- A flash dump contains a key unique to your chip and your USB configuration: do not publish it.
- The bootloader is recoverable: a failed flash leaves the device in `0666:0888` mode, from which you can flash a working image. No guarantees, though.

## Ideas

Headset buttons via the ADC, LED control from the PC over HID, a volume or sample-rate indicator, more DSP effects (a persistent
on/off setting, a proper parametric EQ from the PC), decoding the vendor HID commands and the USB configuration block (`0x38000`),
a USB host model in the emulator. See `docs/ARCHITECTURE.md` §9.

## License

The project is licensed under the **GNU General Public License v3.0 or later** (GPL-3.0-or-later), see [`LICENSE`](LICENSE).

The device firmware, the `hub.moondroplab.tech` web hub and the trademarks belong to their owners and are not covered by this license.
The bootloader protocol was reconstructed from the vendor's publicly available web application for interoperability with one's own device.
