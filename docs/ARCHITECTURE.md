# SPV4040 (SpacETouch) USB DAC — firmware architecture and RE tools

**Language:** English · [Русский](ARCHITECTURE.ru.md)

A document for future agents and humans. Project goal: be able to modify the firmware and write custom code for this DAC
(the sources are lost; there is only `firmware.bin`). Image build: **"Nov 17 2025 13:50:18"**. All addresses below refer to this build.

Knowledge status: ✅ verified by emulation or by code · 🟡 inferred from code, not verified · ❓ unknown.

---------------------------------------------------------------------------------------------
## 1. Files

(File names below are given without the `tools/` prefix used in the repository.)

| File | Purpose |
|---|---|
| `firmware.bin`, `firmware_prevc.bin` | images in the "SPV APP Tag" format (magic `SPV@`), 225 351 bytes. `prevc` is a different build |
| `spv_fw_tool.py` | container parser, verification, byte patching with checksum recalculation (`info / verify / patch / fixcrc`) |
| `spv_flasher.html` | WebHID flasher (Chrome/Edge), Moondrop hub protocol. Has a **flash dump** button. ✅ works on hardware |
| `spv_emu.py` | **emulator** RV32IMAC+F+Andes (see §6) + `dis()` disassembler |
| `led_run.py` | emulator harness: ROM stubs, factory key, GPIO log. `make(path)` → a ready `Emu` |
| `led_probe.py` | LED scenarios in the emulator |
| `spv_led_colors.py` | LED colours in all modes (stubs in the patch area, `--check` verification) |
| `sdk/` | **SDK for C programs**: `spv.h`, runtime with hooks into main(), `link.ld`, builder `spv_build.py` (+ `--emu`), examples. See `sdk/README.md` |
| `andes_disasm.py` | old disassembler (needs capstone, addresses = file offsets). **Obsolete**, use `spv_emu.dis` (not in the repository) |
| `LED_findings.md` | LED report (v2) |
| `README_flasher.md` | container format and bootloader protocol |

---------------------------------------------------------------------------------------------
## 2. Container and SPI flash layout

Header (`spv_fw_tool.py`, docstring): `off 0` reset vector `j 0xa4` · `+4` "SPV@" · `+8` entry `0x48001000` ·
`+0xC` userFlag "AMAO" · `+0x14/0x18` pointers to the date/time strings · `+0x1C` number of sections ·
`+0x20` directory `{name[4], addr, size, checksum}` × N.
Section checksum = 32-bit sum of the section's data bytes; its own checksum field counts as zero. ✅

| Flash offset | What | In the `.bin`? |
|---|---|---|
| `0x00000–0x20ad4` | **CPU** section (all code and data) | yes |
| `0x20ad4–0x37000` | empty (zeros in the file). **Patch area**: `spv_led_colors.py` uses `0x20ae0–0x20bc0` and extends CPU; SDK programs start at `0x20c00` | yes |
| `0x37000` | **EXT** section (0x47 bytes) ❓ purpose | yes |
| `0x38000` | device configuration block, 0xbc0 bytes, read at startup (`0x4e18`). 🟡 USB VID/PID/strings live here: there is no static descriptor in the image | **no** |
| `0x39bc0` | **UI settings** (40 bytes, version = 2), `flash_save_ui_param` | **no** |
| `0x3e000` | **chip binding key** (8 bytes), unique per unit | **no** |
| `0x4c000000` | at startup "SPV@" is checked at `0x4c000004` (second image / external flash?) ❓ | — |

> ⚠️ `0x38000`, `0x39bc0` and `0x3e000` exist only on the device. The flasher writes only the sections from the `.bin`,
> so these areas survive reflashing (unless the bootloader erases the whole chip ❓; so far they always survived).
> **Before any experiment take a dump of 0x48000000…0x48040000** (button in `spv_flasher.html`).

---------------------------------------------------------------------------------------------
## 3. CPU and memory

Andes RV32 core (AndeStar V5): **RV32IMAC + F + Zicsr**, Andes extensions: Performance (`bbc/bbs`,
`beqc/bnec`, `bfos/bfoz`, `lea.*`, GP-relative `lbgp/lhgp/lwgp/sbgp/shgp/swgp/addigp`) and **DSP**
(opcode `0x7f`: `minw/maxw/sclip32/add64/sub64/wexti/smar64/maddr32/msubr32/mulsr64/…`).
ABI with hardware float (handlers save the FPU registers) → 🟡 `ilp32f`. `gp = 0x106c18`, stack top `0x140000`.

| Address | Region |
|---|---|
| `0x00000000` | **code in RAM** (ITCM?): copy of flash `0x48010a60`, length `0x9baa`. **File offset = `0x10a60 + address`** ✅ |
| `0x00080000` | **boot ROM** (functions, see §4) |
| `0x00100000` | `.data` (copied from `0x4801a620`, `0x64b8` bytes), then `.bss` from `0x1064b8` to `~0x122000` ✅ |
| `0x0013ffff` | top of stack |
| `0x10000000` | Andes **PLIC**: priorities `+0`, enables `+0x2000`, threshold/claim `0x10200000/0x10200004` ✅ |
| `0x20000000…` | peripherals (§5) |
| `0x48000000` | SPI flash, XIP (CPU section) |

Vectors: `mtvec = 0x48001500`, `mmisc_ctl.VEC_PLIC = 1` → it is a table of addresses, `[0]` = exceptions,
`[N]` = handler for IRQ N. Each handler is a wrapper (save registers → body in RAM → `claim/complete` → `mret`). ✅

| IRQ | Body | Block |
|---|---|---|
| 3 | `0x5a84` | ADC `0x21010000` |
| 8, 10, 11 | `0x2c8c`, `0x2f48`, `0x2d30` | `0x20010000` (timer; IRQ 8 drives the button ticks, see §10; 🟡 capture/frequency measurement) |
| 9 | `0x2d64` | `0x20020000/0x20170000` (🟡 audio clock adjustment) |
| 21 | `0x46a4` | `0x20120000` (🟡 audio/timer) |
| 23 | `0x65c0` | `0x20170000` |
| 24 | `0x1498` | `0x20021000` |
| 32 | `0x9b58` | `0x20140000` |
| 34 | `0x8b10` | USB endpoints: audio ISO OUT → `0x7018` (see §10) ✅ |
| 36 | `0x274c` | `0x20110000` (USB) |
| 22, 37 | empty | — |

---------------------------------------------------------------------------------------------
## 4. Boot ROM `0x80000` (as called by the firmware)

| Address | Function | Arguments |
|---|---|---|
| `0x80318` | memcpy | `(dst, src, n)` ✅ |
| `0x80410` | memset | `(dst, c, n)` ✅ |
| `0x8225e` | SPI flash read | `(flash_off, buf, n)` ✅ |
| `0x82200` | SPI flash write/program | `(flash_off, buf, n)` 🟡 |
| `0x8213c` | SPI flash erase | `(flash_off, n, …)` 🟡 |
| `0x82356` / `0x8236e` | begin / end of a flash operation (`a0 = 0`) 🟡 |
| `*(0x80080)` | pointer to printf (strings: "device init complete!", "suspend", …) 🟡 |

Before flash operations the firmware sets bit 24 in `0x20090000` (SPI controller) and waits for bit 5 in `+8`.

---------------------------------------------------------------------------------------------
## 5. Peripherals (from the code)

| Address | Block | What is known |
|---|---|---|
| `0x20000000` | chip variant ID | expected `0x49004080` (mask `0xfffeffff`) or `0x490144xx` / `0x49214480`… ✅ |
| `0x20002000`, `0x20003000` | clocks / clock gating / reset | `0x20002000 + 4*(16+n)` ← 117 when PWM is enabled ✅ |
| `0x20003024` bit 1 + `0x2100801c` bit 31 | **reboot into the USB bootloader** (`0x588`) ✅ |
| `0x20005000` | GPIO: `+0` data, `+0x10/+0x14` direction (64 pins) | barely used in normal operation ✅ |
| `0x20090000` | SPI flash controller | ✅ |
| `0x20100000` | **PWM**: channel `n` = `+4n`, value `(duty<<16) \| 0xff00`, bit 0 = enable | 8-bit ✅ |
| `0x20110000` | **USB controller** + **pad** registers `+4*pin` (9 = PWM function, `0x10/0x210` = GPIO/off); `+0x110/+0x114` = input levels of pins 0–31 / 32–63 | ✅ pads, pin levels; 🟡 USB |
| `0x20120000` | audio/timer: `+0xa4 = 0x1387_0001` (period 4999) | 🟡 |
| `0x20160000` | `+0x28/+0x2c` = **unique die ID** (64-bit) | ✅ |
| `0x21000000…0x21005000` | AON/pads/power, used for sleep | 🟡 |
| `0x21010000` | **ADC**: start per channel, status `+0x4c`, results `+0x50/0x54/0x6c/0x70` | ✅ |

---------------------------------------------------------------------------------------------
## 6. The emulator `spv_emu.py`

**Core.** Class `Emu(image)`. An interpreter with a decoded-instruction cache for flash, about 110k instructions/s.
Registers `x[32]`, `f[32]` (raw 32-bit), CSRs in a dict, `trap()/mret` for exceptions
(external interrupts are **not generated**, the PLIC is not modelled).
- Memory: flash = the image at `0x48000000`; MMIO `0x20000000–0x22000000` → `mmio_read/mmio_write`; everything else is RAM,
  lazy 4 KB pages (`0x4c…` and `0x10000000` also end up here as RAM).
- MMIO: value = the last written one (`mmio_regs`). Hooks: `mmio_read_hooks[addr]`, `mmio_write_hooks[]`.
  Log `mmio_log` (disable with `log_mmio = False`).
- **Polling detector.** If `(pc, addr)` is read more than `poll_limit = 2000` times in a row with no MMIO writes in between,
  the answer is inverted for good (`poll_flips`). This gets through "wait for ready" loops. Prints `[poll] …` to stderr.
- **Time.** Every read of `mcycle/time` adds `cyc_per_read = 20000` cycles, so delays pass quickly.
- `hooks[pc] = fn(emu)` — native replacement of a function (after the call `pc = ra`). `pre_hooks[pc]` — called before the instruction.
  A call into the ROM range without a hook → `unknown_rom` (if set: log and `a0 = 0`) or `Stop`.
- `hist` — the last 64 PCs, `calls` — counter of `jal/jalr ra` targets. Exceptions: `Stop`, `Unimpl`.
- `dis(emu, start, end)` — disassembler with GP addresses resolved.
- **Andes semantics.** Verified by a successful boot: `bbc/bbs/beqc/bnec/bfoz/bfos(msb≥lsb)/lea.*/lwgp/swgp/lbugp/lhugp/addigp/sbgp`,
  `minw/maxw/add64/wexti`. Implemented per the P-extension spec but not verified: `bfos/bfoz` with `msb < lsb`, `sclip32/uclip32`,
  `smar64/smsr64/umar64/umsr64/maddr32/msubr32/mulsr64/mulr64`, `ffb/ffzmism/ffmism/flmism`, `shgp`.
  An unrecognised DSP opcode → `Unimpl` with `f7/f3`. In flash (0x1000–0xe000) there were also `f7` 0x00/0x10/0x1f/0x20/0x3f/0x4e/0x4f/0x50/0x6a/0x7f,
  but most such places are data, not code.
- Float: `fcvt.w.s` always rounds to nearest (the rounding mode is ignored). Harmless for the checks done so far.

**The `led_run.make(path)` harness.** ROM stubs, `0x20000000 = 0x49004080`, a fake die ID,
`pre_hooks[0x13a4]` substitutes the correct key (bypasses the check at `0x1350–0x13cc`), `gpio_log` for `0x20005000`/`0x21010000`.
```python
from led_run import make; from spv_emu import dis
e = make('firmware.bin'); e.run(2_500_000)     # ≈20 s, main loop 0x360 reached
print(dis(e, 0x360, 0x3a0))
e.store(0x106538, 4, 3)                         # "USB configured" → the audio logic 0x1138 starts working
```
State snapshot: `copy.deepcopy(e)` after boot (as done in `spv_led_colors.emu_check`).
To call a firmware function from Python: set `a0…`, `ra` = a sentinel address, `pc` = the function and `run(…, until={sentinel})`
(see `sdk/examples/bubble/test_emu.py`, `call()`).

**What is missing.** A USB host model (enumeration, HID reports), PLIC/timer interrupts, audio DMA, a real ADC.
To check code that depends on them, set the state variables in RAM by hand (see §7) or call the handlers directly (as `test_emu.py` does with the button tick and the EQ).

---------------------------------------------------------------------------------------------
## 7. Program map (RAM code `0x0…`, flash code `0x48…`)

**Startup:** `0x48000000 → 0xa4 → 0x480000b0` (sp) → `0x48001000` (gp, mtvec, CSRs) → `0x480012e4`
(check of the second image `0x4c000000`) → `0x480068b0` (crt0: copies RAM code/.data, clears bss) →
`0x48006b6c` init → initialisation in RAM code (clocks, flash config, key, ADC, audio, USB, buttons) → main loop.

| Address | Function |
|---|---|
| `0x360` | **main loop** (super-loop): `0x1138` → LED (`0x5fc`, if the LED is enabled) |
| `0x1138` | audio control task, only when `[0x106538] ≥ 2` (USB configured). Sample processing itself happens in IRQ 34, §10 |
| `0x12d4–0x13d4` | **chip binding check**: die ID (add64) XOR "Ttg?Tech" == flash `0x3e000`; otherwise `0x588` |
| `0x588` | **reboot into the USB bootloader** (flag in `0x2100801c`) |
| `0x5b9c` / `0x5b64` | delays (by `mcycle`), µs / ms |
| `0x4e18` | reads the device config (flash `0x38000`, 0xbc0 bytes) |
| `0x5230` / `0x51a0` / `0x50ec` | load / save UI settings (`0x39bc0`) / pack them from the RAM structure |
| `0x21f8` | applies an EQ preset (`[0x10648c]`): computes biquad coefficients for all sample rates |
| `0x1668` | **EQ on a sample buffer** (§10) |
| `0x2414` | LONG_PRESS_HOLD button callback: volume auto-repeat, preset toggle |
| `0x5988(ch, &out)` | reads an ADC channel (headset buttons) |
| `0x75f4` | standard USB requests (bRequest 0..11); `0x7888` = SET_CONFIGURATION → `[0x106538]` |
| `0x3a20…0x3e94` | USB Audio class requests (switch on the unit ID `wIndex>>8`) |
| `0x7018` | **USB audio ISO OUT handler** (IRQ 34) |
| `0x74f8`, `0x8d84` | **HID vendor**: report `[75, 1, 255]` → `0x588`; `[75, 1, n]` → subcommands `0x8e90` (n = 1..4 …) ❓; `[75, 128, …]` ❓ |
| `0x9890` / `0x9818` | entering sleep (`wfi`) / preparing pins for sleep (pins 3, 34, 36 → output 0) |
| `0x48004184` | `pwm_set(pin, en, duty)` |
| `0x48004288` | `led_set_rgb(r, g, b, on, brightness)` — R = pin 3 / ch3, G = pin 33 / ch1, B = pin 5 / ch5 |
| `0x48002fc0` | LED mode `(mode, blink, period, counter)`: 0 white, 1 green, 2 red, 3 blue, 5 purple |
| `0x5fc` | LED task driven by the event `[0x1064b8]` (see `LED_findings.md`) |
| `0x48003410` / `0x48003170` | PRESS_DOWN / PRESS_UP button callbacks (dispatch on the button id) |
| `0x48008b38` / `0x48008b5c` | GPIO: write mask / pin direction |

**RAM variables:** `0x106538` — USB state · `0x10648c` — EQ preset (8 by default) ·
`0x1064b8` — LED event `{u8 ev; u8 blink; u16 counter@+2; u16 period@+4}` · `0x11cb10` — **UI structure**
(`+15` = LED on, `+12` = preset; see `0x50ec` for the mapping to file fields) · `0x11cb34` — sample rate in Hz (and the start of the audio/volume structure) ·
`0x11cb38` — bit depth · `0x11d22c` — **DAC volume** (int16, 0 = max).

Default UI settings (40 bytes, written when the flash is empty):
`02000000 ffffffff ff000000 ff010005 08000201 3c000100 ffffffff 01000001 00010100 0000ffff`.

HID descriptor (`0x48020a00`): Report IDs **75** and **84**, 63 bytes in/out each, Usage Page `0xFF01`; plus consumer volume keys.

---------------------------------------------------------------------------------------------
## 8. How to write custom code (recipe)

> **There is a ready path now: `sdk/` (C, gcc, hooks into init/main loop, emulation).** Below is the manual way for small patches.
> Firmware `main()`: `0x48002d88 jalr ra,0xf4` (init) and the loop `0x48002d8c jalr ra,0x360`. `0x47f4` = core clock (360 MHz),
> `0x5b64(ms)` / `0x5b9c(us)` = delays. RAM `0x122000…0x13f000` is not used by the firmware (measured in the emulator).

1. **Where to put code.** There is no free space inside CPU. Use the flash patch area `0x48020bc0+`
   (after the LED stubs), extending the CPU section `size` in the directory (`off 0x28`) and recalculating the checksum
   (`Firmware.recompute_all()`). Keep bytes `0x20ad4–0x20ae0` at `0xFF`: on the device that is erased flash,
   and crt0 copies them into the last word of `.data`. Do not go past `0x37000` (EXT).
2. **How to hook in.** Replace a `jal ra, F` (4 bytes, ±1 MB) with `jal ra, stub`, or in RAM code an `auipc ra / jalr ra` pair with your target.
   Leave the stub with a tail `j F` (ra preserved) or `ret`. Example: `spv_led_colors.py`
   (`enc_jal/enc_auipc/enc_jalr/hi_lo`). From C, use `spv_ram_redirect()` (SDK) to redirect a whole RAM-code function at run time.
3. **RAM code** (`0x0…0x9baa`) is edited in the file at offset `0x10a60 + address`. It is copied at every boot.
   It can also be patched at run time (it is writable; `fence.i` afterwards).
4. **Build.** For small patches encode instructions by hand (`enc_*`), for larger ones use the SDK
   (`riscv64-unknown-elf-gcc -march=rv32imafc_zicsr -mabi=ilp32f -mno-relax`, see `sdk/README.md`).
   Andes instructions are not needed in your own code.
5. **Check.** Before flashing, always run the patched image in the emulator (`make(path).run(…)`, then a scenario).
6. **Do not touch** the key check (`0x12d4…`) without a reason: on a mismatch the device goes into the bootloader (this is reversible).

---------------------------------------------------------------------------------------------
## 9. Open questions / next steps

- HID vendor commands (`0x8d84`, subcommands `0x8e90`): decode the table, find an "LED on/off" command and
  the commands that set LED events 1, 3–9 (only event 2 was found in the code).
- Contents of the config block at `0x38000` (VID/PID/strings): take it from a device dump.
- The EXT section (`0x48037000`, 0x47 bytes).
- Whether the second image at `0x4c000000` is needed; what the ROM bootloader does with the `encryptAddress` field.
- A USB host model in the emulator (control transfers via the `0x20110000` registers) to exercise HID commands.
- Compare `firmware.bin` and `firmware_prevc.bin` (48 188 differing bytes): what changed between the builds.
- CPU load of custom DSP at high sample rates (192 kHz and above), on hardware.
- Persisting custom settings (e.g. an effect on/off flag) in the UI settings block.

---------------------------------------------------------------------------------------------
## 10. Audio path and buttons (decoded 2026-11-08, example `sdk/examples/bubble`)

**Audio goes through the CPU.** USB ISO OUT is handled in IRQ 34 (`0x8b20` → `0x7018`). The packet is put into the ring buffer
at `0x1066dc`, `0x6918` converts it to int32 L/R interleaved, then (if `[0x11c0bc] == 0`, i.e. not altsetting 4):
`>>2` (headroom) → `0x9954` → **`0x1668(buf, frames, 10, fs)` = EQ** → `sclip32` to 30 bits → `<<2` → `0x696c` on to the DAC. ✅
- `0x1668` picks the coefficient table by `fs` (44.1k `0x11c7dc`, 48k `0x11c714`, 88.2k `0x11c64c`, 96k `0x11c584`,
  176.4k `0x11c4bc`, 192k `0x11c3f4`, 352.8k `0x11c32c`, 384k `0x11c264`, 705.6k `0x11c19c`, 768k `0x11c0d4`; 20 bytes per band)
  and calls `0x9a2c(frames, buf, coef, state)` for each band; state at `0x11c8a4 + 40·i`. ✅ (a C replacement is bit-exact in the emulator)
- `0x9a2c` is an in-place stereo biquad (smar64). `0x21f8` computes the coefficients (`0x1870`) for all sample rates.
- `[0x11cb34]` = fs in Hz, `[0x11cb38]` = bit depth (16/24/32).
- The IRQ handler saves the caller-saved FPU registers and fcsr and **re-enables interrupts (nesting)** (`csrrsi mstatus,8`).

**Buttons: a MultiButton library** (`0x2780` init, `0x27e8` attach, `0x27f8` state machine, tick `0x26d0` from IRQ 8).
Structure of 44 bytes: `+4` id, `+8` level-read function, `+12+4·event` callbacks (0 PRESS_DOWN, 1 PRESS_UP, 2 REPEAT, 3 SINGLE,
4 DOUBLE, 5 LONG_START, 6 LONG_HOLD), `+40` next. ✅
| id | structure | what | callbacks |
|---|---|---|---|
| 8 | `0x11ca60` | **"+"**, pin 8 (`0x20110110` bit 8), active low, pull-up | DOWN `0x48003410`, UP `0x48003170`, HOLD `0x2414` |
| 34 | `0x11ca34` | **"−"**, pin 34 (`0x20110114` bit 2) | the same |
| 40–42 | `0x11ca8c/0x11cae4/0x11cab8` | headset buttons via the ADC (`0x5a00`) → HID consumer keys (`0x8430`) 🟡 | DOWN/UP |

The volume step happens on PRESS_UP (and every 100 ticks of HOLD); volume is `int16 [0x11d22c]`, 0 = maximum, step ≈153.
At the limit → LED event 2. A long "+" (1500 HOLD ticks) toggles the EQ preset 8 ↔ the saved one.
