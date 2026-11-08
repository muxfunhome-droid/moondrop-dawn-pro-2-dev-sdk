# The LED in the SPV DAC firmware — report (v2, based on emulation)

**Language:** English · [Русский](LED_findings.ru.md)

Goal: find how the LED colour and brightness are set and how to change them by reflashing.
Result: **the logic is fully found and verified in the emulator**. The previous version of the report
("a digital LED on GPIO 0x20005000, fixed brightness, 0x21010000 = IOMUX") was wrong.

## Tools
| File | What it does |
|------|-----------|
| `spv_emu.py` | emulator RV32IMAC + Zicsr + F + AndeStar V5 (bbc/bbs/beqc/bnec/bfos/bfoz/lea/*gp) + Andes DSP (minw/maxw/add64/wexti/smar64/maddr32/…), MMIO log, polling-loop detector, accelerated mcycle, built-in disassembler `dis()` |
| `led_run.py` | harness: boot-ROM stubs (memcpy/memset/SPI flash), factory key, GPIO log |
| `led_probe.py` | LED scenarios: sets EQ preset / event / LED flag and prints the writes to PWM and pads |

```
python3 led_run.py   firmware.bin 3000000   # boot to the main loop + peripheral log
python3 led_probe.py firmware.bin           # LED scenarios
```

## What had to be emulated for the firmware to boot
- **Boot ROM `0x80000`**: `0x80318` memcpy, `0x80410` memset, `0x8225e` SPI flash read,
  `0x8213c` erase, `0x82200` write, `0x82356/0x8236e` begin/end of an operation. printf is called through the pointer at `0x80080`.
- **Code in RAM**: the loader copies `0x9baa` bytes from flash `0x48010a60` to address `0x0`.
  RAM address `X` ↔ file offset `0x10a60 + X`. *This is why the earlier static analysis did not find any driver calls.*
- **Chip binding**: the die ID (`0x20160028/2c`) XOR `"Ttg?Tech"` is compared with 8 bytes
  at flash `0x3e000`, written at the factory. They are not in the image; the emulator substitutes the key.
- Interrupt controller: Andes PLIC at `0x10000000` (claim/complete at `0x10200004`), vectored mode, table at `0x48001500`.
- Main loop: super-loop `0x360`, no RTOS. The audio task runs only after USB `SET_CONFIGURATION` (`[0x106538] ≥ 2`).

## LED hardware (RGB, PWM)
| Colour (per the firmware API) | Pin | Pad register | PWM channel |
|------|-----|-----------|-----------|
| R | 3  | `0x2011000c` | ch3 → `0x2010000c` |
| G | 33 | `0x20110084` | ch1 → `0x20100004` |
| B | 5  | `0x20110014` | ch5 → `0x20100014` |

- `pwm_set(pin, en, duty)` = `0x48004184`: pad ← `9` (PWM function), channel ← `(duty<<16) | 0xff00` (+ bit 0 = enable). **8-bit PWM, 0..255.**
- `led_set_rgb(r, g, b, on, brightness)` = `0x48004288`: `duty = colour × brightness / 255.0`. With `on = 0` all channels go to 0.
- Pad `0x210` / `0x10` = the pin is not in the PWM function (see below for which one is lit).
- `0x21010000` is the **ADC** (knobs/buttons), not IOMUX. `0x20005000` (GPIO) is touched only when preparing for sleep.

## Colour logic
Main loop (`0x360`): if the LED is enabled in the settings (`st[15]` = byte `0x11cb1f`, saved to flash), it calls
`led_task` (`0x5fc`) with the event `[0x1064b8]` = `{u8 event; u8 blink; u16 half; u16 period}`. If the LED is disabled, all pads ← `0x210`.

| Event | What is lit | Code |
|---|---|---|
| 0 (normal state), EQ preset ≠ 8 | **(R0, G220, B255)**, brightness 255 → cyan | `0x72c` |
| 0, EQ preset = 8 (factory value in the settings) | PWM is not enabled: R ← `0x10`, G/B ← `0x210` | `0x75c` |
| 1, 2 | white (255,255,255), blinking 500/500. Event 2 = **volume reached its limit** | `0x624` |
| 3, 4, 5 | purple (102,0,255) | `0x648` |
| 6 | red (255,0,0) | `0x66c` |
| 7 → 8 | red, then green (0,255,0) | `0x68c`/`0x6c4` |
| 9 | blue (0,0,255) | `0x6fc` |

There is no yellow (R+G) in any `led_set_rgb` call of this firmware (9 call sites in total).
The "red/yellow/white" description from the product README does not apply to this image.

## How to change the colour and brightness of the normal state (verified in the emulator)
Instructions at `0x734..0x742` (RAM), file offsets:

| Offset | Now | Meaning | Format |
|---|---|---|---|
| `0x11194` | `1307f00f` | `addi a4,zero,255` — **brightness** | 4 bytes, 0..255 (up to 2047) |
| `0x1119a` | `1306f00f` | `addi a2,zero,255` — **B** | 4 bytes |
| `0x1119e` | `9305c00d` | `addi a1,zero,220` — **G** | 4 bytes |
| `0x111a2` | `0145` | `c.li a0,0` — **R** | 2 bytes, only 0..31 |

Encoding: `addi rd,zero,imm` = `(imm<<20) | (rd<<7) | 0x13`, little-endian; a1 = 11, a2 = 12, a4 = 14.
Example (green, verified with `led_probe`: ch1 = 255, ch3 = ch5 = 0):
```
python3 spv_fw_tool.py patch firmware.bin -o p1.bin       --offset 0x1119a --bytes 13060000   # B=0
python3 spv_fw_tool.py patch p1.bin       -o fw_green.bin --offset 0x1119e --bytes 9305f00f   # G=255
```
R > 31 needs a 4-byte `addi`, and there are only 2 bytes. That fragment has to be reworked, e.g. by swapping
registers or jumping to the patch area. (`spv_led_colors.py` does it properly via stubs.)

## What remained unverified at that point (needed the device)
1. **Which physical colour is on which pin.** The R/G/B names come from the calls themselves (the vendor passes
   (255,0,0) as "red", (102,0,255) as "purple"), but the board wiring cannot be learned from the firmware.
2. **Polarity.** If the LED is common-anode, `duty = 255` may mean "off".
3. **Which EQ preset is saved on the device.** With preset 8 the normal state does not use the PWM colour at all.
4. No USB host was emulated: the "configured" state and the events were set in memory directly.

## Result on hardware (2026-11-08): PWM polarity is INVERTED
`fw_green.bin` (normal = 0,255,0) lit **purple** on the device. That is exactly "R and B on, G off",
i.e. a common-anode LED: duty 0 = full brightness, duty 255 = off. Fix: `spv_led_colors.py --invert on`
(3 × `bfoz a2,a2,15,0` in `led_set_rgb` @0x48004288 → `xori a2,a2,255`); colours are then given "as is".
There are no `led_set_rgb` calls with `on=0` in the firmware (all 9 sites pass `a3=1`), so the "all channels to 0" branch is dead.
Image: `fw_custom.bin` = `--invert on --set all=160,0,255 --set normal=0,255,0` (normal green, the rest purple).

## Clarification (2026-11-08, second iteration on hardware)
- The device's "normal mode" = **EQ preset 8** (factory). The PWM colour is not used there at all: pad R ← 0x10 (red is lit),
  G/B ← 0x210 (off). That is why the `normal` colour (preset ≠ 8, "custom tuning/EQ") is not visible in normal mode.
  A new site `normal8` (RAM 0x75c) calls `led_set_rgb` instead.
- Pad **0x10 = LED LIT**, 0x210 = off. The "off" phase of blinking (5 blocks of 16 bytes: 0x48003014/3050/30a0/30f4/3144)
  set all three pads to 0x10 → a white flash between colours. `--blink-off dark` replaces it with 0x210 (a shared stub after the colour stubs).
- PWM: duty 0 = channel lit (common anode); `--invert on` compensates for it.
- Image: `fw_custom2.bin` = `--invert on --blink-off dark --set all=160,0,255 --set normal8=0,255,0`
  (normal mode green; preset ≠ 8 and all events/blinking purple). ✅ works on hardware.
