# life — Conway's Game of Life on a 10×10 board, computed inside the USB DAC

**Language:** English · [Русский](README.ru.md)

The PC sends a board and a number of steps, the **RISC-V core inside the DAC** computes the generations and returns only the final board.
The PC program draws it. Audio is untouched. The LED turns green while the chip is computing.

The drawing code of the host program, with the glider after 4 steps (the result `test_emu.py` checks against the reference):
```
$ sdk/examples/life/host/life glider -n 4
  вход (тор)                  после 4 шагов
  ┌────────────────────┐      ┌────────────────────┐
  │··██················│      │····················│
  │····██··············│      │····██··············│
  │██████··············│      │······██············│
  │····················│      │··██████············│
  │····················│  ──► │····················│
  │····················│      │····················│
  │····················│      │····················│
  │····················│      │····················│
  │····················│      │····················│
  │····················│      │····················│
  └────────────────────┘      └────────────────────┘
```
(The host program prints in Russian: «вход» = input, «после N шагов» = after N steps, «тор» = torus.)

## Build
```sh
scripts/build_app.sh life build/fw_custom2.bin build/fw_life.bin   # firmware, checked in the emulator
python3 -I sdk/examples/life/test_emu.py                           # 11 cases vs a Python reference
make -C sdk/examples/life/host                                     # PC program (Linux, no dependencies)
```

## Usage (Linux)
```sh
life glider -n 4             # presets: glider, blinker, toad, beacon, rpent, random[:seed]
life field.txt -n 100        # board from a file: 10 lines, alive = # * o O X 1, dead = . 0 space
life - -n 5 < field.txt      # board from stdin
life glider -a 40 -t 100     # animation: 40 generations, each one computed on the chip, 100 ms per frame
life glider -n 4000000000    # billions of steps: the chip finds the cycle and takes the rest modulo the period
life rpent -n 50 --no-wrap   # dead edges instead of a torus
life random:7 -n 20 --plain  # print the result as text (can be fed back in as a file)
```

## How it works
- **Protocol** (vendor HID report 75, 64 bytes): request `[75, 0xCB, seq, flags, steps u32 @4, board 13 bytes @8]`,
  reply `[75, 0xCB, seq, status, steps u32 @4, board 13 bytes @8, time µs u32 @24, population u8 @28, cycle period u32 @32, found at step u32 @36]`.
  The board is 100 bits: cell (row r, column c) = bit `r*10 + c`, LSB of the first byte = top-left corner. `flags` bit 0 = dead edges.
- **Hook** like in `calc`: the stock HID handler `0x8d84` is redirected; reports with `0xCB` are taken, everything else goes to the stock code.
- **No blocking.** The handler only copies the request; the generations are computed in `app_loop()`, `steps_per_pass` (500) steps per
  main-loop pass, so the firmware (HID commands, entering the bootloader) keeps running. The reply is sent when done.
- **Bit-parallel step.** Each row is a 10-bit mask; the neighbour count of all 10 cells of a row is computed at once with a bit-sliced adder.
- **Cycle detection (Brent).** The state is saved at steps 1, 2, 4, 8…; once the board equals the saved one, the period is known
  and the remaining steps are reduced modulo the period. A glider on the 10×10 torus has period 40, found at step 104.

Status: 🧪 verified in the emulator (`test_emu.py`: glider, blinker, block, random soups, torus and dead edges, 4 000 000 001 steps,
computation split over 74 main-loop passes). Not yet run on hardware.
