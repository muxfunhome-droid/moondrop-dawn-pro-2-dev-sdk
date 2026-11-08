# SPV4040 SDK — your own C programs for the DAC

**Language:** English · [Русский](README.ru.md)

> **⚠️ THIS PROJECT IS VIBE-CODED: EVERYTHING WAS MADE BY AN AI. USE AT YOUR OWN RISK.** Details in the main `README.md`.

A C program is embedded into the stock firmware as an add-on: audio, USB, the equaliser and the buttons
work as before, and your code gets `app_init()` at startup and `app_loop()` on every pass of the main loop.
It can also hook firmware functions, including the audio path (see the `bubble` example).

## Quick start
```sh
scripts/build_app.sh rainbow build/fw_custom2.bin build/fw_rainbow.bin
# same as: python3 -I sdk/spv_build.py sdk/examples/rainbow --base build/fw_custom2.bin -o build/fw_rainbow.bin --emu
```
The script compiles the program, inserts it into `fw_custom2.bin`, recalculates the checksums and runs the result
in the emulator. It prints the `spv_log()` output and the colours the program set on the LED.
Flash the resulting `fw_rainbow.bin` with `web/spv_flasher.html` as usual.

A new program: create a folder with `*.c` files, `#include "spv.h"` and define `app_init` / `app_loop`.

You need `riscv64-unknown-elf-gcc` (Debian: `sudo apt install gcc-riscv64-unknown-elf`).

## First run on hardware
1. Flash **`fw_sdk_empty.bin`** (the empty program `examples/empty` on top of `fw_custom2.bin`).
   The DAC must behave exactly like with `fw_custom2.bin`. This checks the hook mechanism, RAM at `0x130000` and `mcycleh`.
2. Only then flash `fw_rainbow.bin` and your own programs.
3. Keep a flash dump or `fw_custom2.bin` at hand to roll back.

## How it works
| What | Where |
|---|---|
| Program code and constants | flash `0x48020c00…0x48037000` (~89 KB), after the firmware code and the LED stubs |
| `.data` / `.bss` | RAM `0x130000…0x13a000` (40 KB). The firmware uses `0x100000…0x122000`, the stack grows down from `0x140000` |
| Stack | shared with the firmware |
| Entry at startup | `0x48002d88`: `jalr ra,0xf4` (firmware init) → `jal __spv_init_hook` |
| Main loop | `0x48002d8c`: `jalr ra,0x360` → `jal __spv_loop_hook` (the body of `0x360` is rewritten in C in `rt/spv_rt.c`) |

`__spv_init_hook`: firmware init → copy `.data` / clear `.bss` → `app_init()`.
`__spv_loop_hook`: the firmware audio task `0x1138` → the stock LED task (unless the program has claimed the LED) → `app_loop()`.

The program image starts with an `SPVA` header that stores the original size of the CPU section. So `spv_build.py`
can use an already built image as the base: the old program is removed (the base is restored byte for byte) and the new one is installed.

Compiler flags: `-march=rv32imafc_zicsr -mabi=ilp32f -mno-relax -Os`. The ABI matches the firmware, so its
functions can be called directly. `gp` belongs to the firmware (`0x106c18`); `-mno-relax` and the absence of
`__global_pointer$` guarantee that your code does not touch it. There is no standard library: the runtime provides
`memcpy/memset/memmove/memcmp/strlen`, plus `libgcc` (64-bit division etc.).

## API (`include/spv.h`)
- **Time:** `spv_millis()`, `spv_micros()`, `spv_cycles()`, `spv_cpu_hz()` (360 MHz according to the emulator).
  The blocking `spv_delay_ms/us()` are firmware functions: only short ones in `app_loop`.
- **LED:** `spv_led_claim(true)` disables the stock LED logic. Then `spv_led_rgb(r,g,b)` sets the real colour
  (the common anode is handled, independent of the `--invert` patches), `spv_led_off()` turns it off. `spv_hsv_to_rgb()`.
  `spv_led_claim(false)` gives the LED back to the firmware.
- **Firmware state:** `SPV_EQ_PRESET` (8 = normal mode), `SPV_LED_EVENT`, `SPV_LED_ENABLED`, `SPV_USB_STATE`,
  `SPV_VOLUME` (int16, 0 = max).
- **Hooks:** `spv_ram_redirect(addr, orig0, orig1, fn)` replaces the first 8 bytes of a firmware function in RAM code
  (`0x0…0x9baa`) with a jump to `fn` (`auipc t0` + `jalr zero,t0`). The arguments and the return address are preserved, so `fn`
  gets the same arguments and returns straight to the caller. The original words are checked first (protection against another build).
  Call it from `app_init()`.
- **Misc:** `spv_reboot_to_bootloader()`, `spv_fw_adc_read()` (knobs/buttons, semantics not decoded), registers `SPV_PAD()`, `SPV_REG32()`.
- **Log:** `spv_log("x=%d %08x %s\n", …)` writes to a ring buffer in RAM. The emulator shows it (`--emu`).

## Rules
- `app_loop()` must not block: the same loop handles audio and the firmware's HID commands,
  including the "enter bootloader" command used for reflashing. **A hung `app_loop` = no normal way to
  reflash.** There is no known way yet to enter the bootloader without a working firmware.
- Write state machines on `spv_millis()` instead of `while(…)` loops and long delays.
- Always run `--emu` before flashing. The emulator does not model interrupts, the USB host or audio, so
  set the states you need in RAM yourself (see `emulate()` in `spv_build.py`), or call firmware functions directly
  (see `examples/bubble/test_emu.py`).
- Code called from interrupt handlers (for example a hooked audio function) may use float: the firmware's handlers save
  the caller-saved FPU registers and fcsr. It must not block, and the firmware's interrupts nest
  (the timer/button IRQ can preempt the audio IRQ), so share data between them through simple single-writer variables.

## Verified on hardware
`fw_rainbow.bin` (a rainbow on the LED on top of `fw_custom2.bin`) was flashed and works perfectly. This confirms: execution of code
from `0x48020c00`, writing and reading RAM at `0x130000`, `.data`/`.bss`, `mcycle/mcycleh`, `spv_millis()`, `spv_led_rgb()`,
`spv_led_claim()`, the replaced calls in the firmware's `main()`.
`examples/bubble` (audio and button hooks, `spv_ram_redirect`) also works on hardware.
Not verified: `spv_log` (the RAM buffer is visible only to the emulator), `spv_fw_adc_read`, `spv_reboot_to_bootloader` from your own program.

## Example `examples/bubble` — custom sound and bubbles on the buttons ✅ on hardware
```sh
scripts/build_app.sh bubble build/fw_custom2.bin build/fw_bubble.bin
python3 -I sdk/examples/bubble/test_emu.py       # bit-exact vs the stock EQ, buttons, renders build/bubble_demo.wav
```
- The firmware EQ function `0x1668` (called by the USB interrupt for every audio packet) is redirected to `bubble_eq()` with
  `spv_ram_redirect()`: the stock EQ (bit-exact) + a "warm" effect (bass +4 dB, headphone crossfeed, soft limiter).
- The "+"/"−" button callbacks are swapped right in the MultiButton RAM structures: PRESS_DOWN → a bubble (higher for "+", lower for "−"),
  LONG_HOLD → a bubble for every volume step, DOUBLE_CLICK "−" → effect on/off, EQ mode change → a double bubble.
  Details: `docs/ARCHITECTURE.md` §10.
- The bubble is synthesised inside the audio stream, so you hear it only while the host is sending audio.
- The sound parameters (`FX_*`, `BUB_*`) are at the top of `main.c`.
- `test_emu.py` takes about 8 minutes: it boots two images and pushes 1 s of audio through the EQ in the emulator.
  It writes `bubble_demo.wav` (with the effect and three button presses) and `bubble_demo_factory.wav` (the stock path) for comparison.
