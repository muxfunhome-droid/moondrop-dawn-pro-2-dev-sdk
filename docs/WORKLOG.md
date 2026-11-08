# What was done and how it works

**Language:** English · [Русский](WORKLOG.ru.md)

> **⚠️ THIS PROJECT IS VIBE-CODED: EVERYTHING WAS MADE BY AN AI. USE AT YOUR OWN RISK.** Details in the main `README.md`.

A detailed project report: timeline, principles, a list of every firmware change and what of it was verified on hardware.
Firmware reference: `ARCHITECTURE.md`; LED: `LED_findings.md`; format and protocol: `README_flasher.md`;
C SDK: `../sdk/README.md`.

Legend: ✅ verified on the device · 🧪 verified only in the emulator · 🟡 inferred from code, not verified.

---------------------------------------------------------------------------------------------
## 1. The starting point

A Moondrop DAWN PRO2 DAC (marked SPACETOUCH SPV4040, Andes RISC-V core). There are no firmware sources, only a binary image
(`firmware.bin`, build "Nov 17 2025 13:50:18") and the vendor's web hub. Goals: understand how the firmware works, learn to change
it safely (the LED colours first) and make it possible to write your own programs in C.

---------------------------------------------------------------------------------------------
## 2. Timeline

| # | Stage | Result |
|---|---|---|
| 1 | Decoding the "SPV APP Tag" container and the bootloader protocol (from the web hub's JS) | `spv_fw_tool.py` (info/verify/patch/fixcrc), `spv_flasher.html` (WebHID, ✅ works) |
| 2 | Emulator RV32IMAC + F + Andes (AndeStar V5, DSP) | `spv_emu.py`: the firmware boots to the main loop, MMIO log, disassembler |
| 3 | Finding the LED logic | RGB on PWM, pins 3/33/5, 9 call sites of `led_set_rgb` (the first version of the report was wrong and was corrected) |
| 4 | `spv_led_colors.py`: colours in every mode | stubs in the patch area, emulator check (`--check`) |
| 5 | First flash on hardware (`normal = 0,255,0`) | ✅ it lit **purple**, not green → PWM polarity |
| 6 | PWM inversion (`--invert on`) | on hardware normal mode turned red, and white appeared while blinking → see §4 |
| 7 | "Preset 8" mode and the "off" phase | `normal8` + `--blink-off dark` → ✅ **colours work as intended** |
| 8 | C SDK | `sdk/`: gcc-riscv64, a runtime with hooks in `main()`, the `spv_build.py` builder, ✅ the LED rainbow works |
| 9 | WebHID access rights | a udev rule for normal mode (`35d8`) and the bootloader (`0666:0888`) |
| 10 | Decoding the audio path and the buttons | the samples go through the CPU: USB interrupt → `0x7018` → EQ `0x1668` (10 biquads `0x9a2c`) → DAC; buttons are a MultiButton library, "+" = pin 8, "−" = pin 34 (ARCHITECTURE §10) |
| 11 | `sdk/examples/bubble`: custom sound + bubbles on the buttons | `spv_ram_redirect()` in the SDK; EQ bit-exact with the stock one (emulator), ✅ **works on hardware**: the "warm" effect and bubbles on "+"/"−" |
| 12 | Vendor HID channel PC ↔ chip | receive buffer `0x106554`, handler `0x8d84`, send `0x8418`; `sdk/examples/calc` — ✅ a calculator on the DAC works on hardware |
| 13 | `sdk/examples/life` | Game of Life 10×10 on the chip, computed in portions in `app_loop()`, cycle detection; 🧪 emulator |

---------------------------------------------------------------------------------------------
## 3. How things work

### 3.1 Container and loading
The image is an "SPV APP Tag": a header, a section directory `{name, addr, size, checksum}`, the **CPU** section (all code and data) and **EXT**.
The section checksum is a byte sum. Code runs from SPI flash (XIP, `0x48000000`), and part of it is copied into RAM-ITCM at address 0:
`crt0` takes `0x9baa` bytes from flash `0x48010a60` and puts them at `0x0`. Hence **RAM address X ↔ file offset `0x10a60 + X`**. Because of this,
naive static analysis did not see any driver calls: most of the logic lives in that RAM block.

### 3.2 Firmware and chip binding
The main loop is a super-loop without an RTOS (`0x360`): the audio control task (`0x1138`, active after USB `SET_CONFIGURATION`) and the LED task (`0x5fc`).
At startup the firmware checks a "binding key": the die ID XOR `"Ttg?Tech"` must match 8 bytes at flash `0x3e000`. Those bytes
were written at the factory and are **not in the image**, so they survive reflashing. On a mismatch the device goes into the USB bootloader (reversible).

### 3.3 The LED
- Three PWM channels (`0x20100000`, 8-bit): R — pin 3, G — pin 33, B — pin 5. `pwm_set(pin, en, duty)` puts the pad into the PWM function (9) and writes the channel.
- `led_set_rgb(r,g,b,on,brightness)`: `duty = colour × brightness / 255`. Called from 9 places (blink modes, steady colours, the normal state).
- **The polarity on the board is inverted** (common anode): `duty 0` = lit, `255` = off. The firmware does not account for it, which is why the "stock" colours look different.
- Pads outside PWM: `0x10` — the LED is **lit**, `0x210` — off.
- The DAC's "normal mode" is **EQ preset 8** (factory). In that state the PWM colour is not used at all; just the red pad R is lit.
  The "custom tuning / EQ" mode is a preset ≠ 8, and that is where the normal-state colour applies.
- The "off" phase of blinking set all three pads to `0x10`, i.e. it **lit white** between flashes.

### 3.4 How the patches work
There is no free space inside CPU. Patches go **after the CPU section** (flash `0x48020ae0…`): the section is extended and the checksum recalculated.
A call site (`jal ra, F` in flash or an `auipc ra`/`jalr ra` pair in RAM code) is redirected to a stub, and the stub returns control
to the original function with a tail `j F`. Every patch must be run in the emulator (`--check`).

### 3.5 The SDK
`app_init()` and `app_loop()` are called from two replaced instructions in the firmware's `main()`. The main loop body is rewritten in C so that the LED
can be "taken over". The program lives in flash at `0x48020c00…`, its data in free RAM at `0x130000…`. The program image starts with an
`SPVA` header that remembers the original CPU size: the builder can remove an old program and install a new one (the base is restored byte for byte).

### 3.6 The emulator
An interpreter with a decode cache (~110k instructions/s). Peripheral models are minimal: MMIO = "last written value" with hooks;
a polling detector inverts status registers to get through wait loops; `mcycle` is accelerated. Missing: USB host, interrupts, audio DMA, ADC.
To check logic, the needed states are set in RAM by hand (`led_probe.py`, `emu_check` in `spv_led_colors.py`, `emulate` in `spv_build.py`),
or firmware functions are called directly (`sdk/examples/bubble/test_emu.py` drives the button tick and the EQ that way).

### 3.7 The audio path and the bubble example
The USB audio OUT interrupt (IRQ 34, `0x7018`) converts each packet to int32 stereo and runs it through the EQ function `0x1668`
(10 software biquads `0x9a2c`). The `bubble` program redirects `0x1668` at run time with `spv_ram_redirect()` to its own C function:
it replays the stock EQ exactly (same coefficient tables and filter state), then applies its effect and mixes in the synthesised bubbles.
The buttons are handled by a MultiButton-style library whose callbacks live in RAM structures, so the program simply wraps
the PRESS_DOWN and LONG_HOLD callbacks (the stock ones still run) and adds a DOUBLE_CLICK callback for "−".
The button callbacks run in the timer interrupt and the audio in the USB interrupt; they talk through counters with a single writer each.

---------------------------------------------------------------------------------------------
## 4. What had to be fixed and why (mistakes along the way)

1. **The first LED model was wrong.** At first it looked like a digital LED on GPIO. Emulation showed PWM at `0x20100000`; `0x21010000` is the ADC.
2. **Green lit purple.** So `duty 0` lights and `255` turns off (common anode). Fix: invert `duty` inside `led_set_rgb`.
3. **"Default red" in normal mode.** The colours were patched in the "preset ≠ 8" branch, but the user's device ran with preset 8, where PWM is not
   enabled at all. A separate `normal8` site was added.
4. **A white flash while blinking.** The "off" phase used pads `0x10`, which means "lit". Replaced with `0x210`.
5. **Permissions.** WebHID could not open the device (`Failed to open the device`) because `/dev/hidraw*` belonged to root. udev rules are needed, and separately for
   the bootloader: it has a different VID/PID (`0666:0888`). The first version of the rule did not cover it.
6. An intermediate diagnostic image (cycling through all 8 channel combinations) was built but not needed: the user described the behaviour of the stock
   firmware, and that was enough to find the cause (items 3–4).
7. **"Failed to write the report" in the flasher.** The user pressed "enter bootloader" while the device was already in the bootloader, which
   does not understand that command. The flasher now detects `0666:0888` and says to go straight to "connect bootloader".
8. `fence.i` is not accepted by the assembler with `-march=rv32imafc_zicsr` (it needs `zifencei`); the runtime emits it as a raw word.

---------------------------------------------------------------------------------------------
## 5. Every change to the firmware

Offsets are file/flash offsets (`0x48000000 + offset`); RAM code is `0x10a60 + address`.

| What | Where | Essence |
|---|---|---|
| Colour stubs | `0x20ae0…0x20ba8` (10 × 20 bytes) | `addi a0/a1/a2/a4` + `j led_set_rgb`; the call sites are redirected to them (9 in `led_task`/modes + `normal8`) |
| PWM inversion | `0x42b4`, `0x42cc`, `0x42e4` | three `bfoz a2,a2,15,0` → `xori a2,a2,255` (4 bytes for 4 bytes) |
| `normal8` | RAM `0x75c`, 20 bytes | the "preset 8" branch: instead of pad R, a call to `led_set_rgb` via a stub, then a return to `0x74c` |
| "Off" phase | `0x48003014/3050/30a0/30f4/3144` (16 bytes each) + a shared stub `0x20ba8…0x20bc0` | a `jal` to the stub, which writes `0x210` to all three pads and returns to `0x48003008` |
| SDK hooks | `0x48002d88`, `0x48002d8c` | `jalr ra,0xf4(zero)` and `jalr ra,0x360(zero)` → `jal` into the SDK runtime |
| SDK program | `0x48020c00…` up to `0x48037000` | the `SPVA` header, code, `.rodata`, the `.data` image |
| `bubble`: EQ hook | RAM `0x1668`, 8 bytes, **at run time** (`app_init`) | `bge`/`addi sp`/`sw ra` → `auipc t0` + `jalr zero,t0` into `bubble_eq()`; the image file is not changed there |
| `bubble`: buttons | RAM `0x11ca60`/`0x11ca34` (+12+4·event), at run time | the PRESS_DOWN/LONG_HOLD callbacks are wrapped, DOUBLE_CLICK for "−" is added; the stock ones are called as before |

The CPU section grows from `0x20ad4` to `0x20bc0` (LED patches) and further to the end of the program. Bytes `0x20ad4–0x20ae0` stay `0xFF` (erased flash on the device).

---------------------------------------------------------------------------------------------
## 6. What was verified

| What | Status |
|---|---|
| WebHID flasher: entering the bootloader, writing, reflashing several times | ✅ |
| Colours: normal mode green, EQ mode and blinking purple, the off phase dark | ✅ |
| SDK: code at `0x48020c00`, RAM `0x130000`, `.data/.bss`, `mcycle/mcycleh`, `spv_millis`, `spv_led_rgb`, `spv_led_claim`, rainbow | ✅ |
| The firmware going through every LED mode (events 1–9, steady and blinking) on the device | 🧪 (in the emulator); on hardware only the states actually seen were checked |
| `spv_log` (ring buffer) | 🧪 (readable only by the emulator) |
| Audio: EQ hook `0x1668`, the "warm" effect, bubbles on "+"/"−" (`examples/bubble`) | ✅ on hardware; EQ bit-exact with the stock one and the buttons — 🧪 (`test_emu.py`) |
| CPU load of the effect at 192 kHz and above | 🟡 not measured |
| Calculator over HID (`examples/calc`): expressions, replies, reflashing still works | ✅ on hardware (16 510 cycles for `(2+3)*-2^3/7`); 🧪 19 expressions in `test_emu.py` |
| Game of Life over HID (`examples/life`) | 🧪 11 cases vs a Python reference; not yet run on hardware |
| Headset buttons via the ADC, HID commands | 🟡 |
| Flash dump from the device in the flasher | 🟡 (the button exists; writing a dump back was not tested) |

---------------------------------------------------------------------------------------------
## 7. Known limitations and risks

- **Only** the build "Nov 17 2025 13:50:18" is supported: the addresses are hard-coded, `spv_led_colors.py` and `spv_build.py` check the build date and the original bytes.
- `app_loop()` must not block: the HID command that enters the bootloader is handled in the main loop. A hung program = no normal way to reflash. There is no known way
  to enter the bootloader without a working firmware, so keep a dump and a known-good image at hand.
- The areas outside the image (`0x38000` USB config, `0x39bc0` settings, `0x3e000` chip key) are unique to each device. Do not overwrite them and do not publish dumps.
- The bubble is synthesised while processing USB audio packets: without an open audio stream from the host you will not hear it.
- The effect on/off state (double-click "−") is not saved: after a power cycle the effect is on again.
- It has not been verified that the bootloader does not erase the whole chip when writing. So far the settings and the key have survived every flash.
