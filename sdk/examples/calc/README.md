# calc — a calculator that runs inside the USB DAC

**Language:** English · [Русский](README.ru.md)

Just for fun: the PC sends an expression, the **RISC-V core inside the Moondrop DAWN PRO 2** parses and evaluates it,
and the result comes back over USB. Audio is untouched, the DAC keeps playing. The LED flashes cyan for 150 ms after each calculation.

Real output from the device:
```
$ sdk/examples/calc/host/calc "(2+3)*-2^3 / 7"
= -5.71428571428571
  посчитано на RISC-V в ЦАПе: 16510 тактов (45.9 мкс), вычисление №6, USB туда-обратно 15.9 мс
```
(16 510 CPU cycles = 45.9 µs on the chip; the ~16 ms is the USB round trip. The host program prints in Russian.)

## Build
```sh
scripts/build_app.sh calc build/fw_custom2.bin build/fw_calc.bin   # firmware, checked in the emulator
python3 -I sdk/examples/calc/test_emu.py                           # 19 expressions vs Python, error cases, stock commands
make -C sdk/examples/calc/host                                     # PC program (Linux, no dependencies)
```
Flash `build/fw_calc.bin` with `web/spv_flasher.html`.

## Usage (Linux)
```sh
sdk/examples/calc/host/calc "2+2*2"          # one expression
sdk/examples/calc/host/calc 2 + 2 '*' 2      # arguments are joined (quote *, or the shell expands it)
sdk/examples/calc/host/calc                  # interactive mode
```
Operators: `+ - * / % ^ ( )`, `sqrt(x)`, `pi`, `e`, numbers like `1.5e3`. `^` takes integer exponents only and is right-associative
(`2^3^2 = 512`); unary minus binds weaker than `^` (`-3^2 = -9`). Computed in `double` (soft-float from libgcc: the core has single precision only).
The device is found automatically (`/sys/class/hidraw`, VID:PID `35d8:011d` + report ID 75 in the descriptor); manually: `-d /dev/hidrawN`.
Access to `/dev/hidraw*` is given by the udev rule from `udev/`.

## How it works
- **Channel.** Vendor HID report 75 (the same one the flasher uses for "enter bootloader"), 64 bytes.
  Request `[75, 0xCA, seq, len, expression…]`, reply `[75, 0xCA, seq, status, errpos, 0,0,0, double @8, cycles u32 @16, counter u32 @20]`.
- **Reception in the firmware.** The USB interrupt (`0x748c`, or `SET_REPORT` over control) puts the report into the buffer `msg = 0x106554 {state, buf}`
  and sets `state = 2`. The main loop calls the handler `0x8d84(ctx 0x121ec0, msg)`.
- **Hook.** `app_init()` redirects `0x8d84` to `calc_hid()` with `spv_ram_redirect()`. Reports with byte `0xCA` are evaluated there;
  everything else (including `[75,1,255]`, enter bootloader) goes to the stock code at `0x8d94`. Reflashing works as before.
- **Reply.** Into the same buffer, sent with the stock `0x8418(ctx, buf, 64)`, the same way the firmware itself replies.
  The send copies into the endpoint FIFO synchronously (`0x7f68`).
- **Cycles** are `mcycle` before/after parsing at 360 MHz. In the emulator the cycle count is meaningless (time is accelerated there).

Status: ✅ works on hardware. Verified in the emulator with `test_emu.py`.
