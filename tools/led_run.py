#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Прогон прошивки в spv_emu с заглушками ROM и журналом GPIO."""
import sys, collections
from spv_emu import Emu, Stop, Unimpl, REG, MASK, fmt_insn

def rom_memcpy(e):
    d, s, n = e.x[10], e.x[11], e.x[12]
    for i in range(n):
        e.store(d + i, 1, e.load(s + i, 1))

def rom_memset(e):
    d, c, n = e.x[10], e.x[11] & 0xff, e.x[12]
    for i in range(n):
        e.store(d + i, 1, c)

FLASH_SIZE = 0x400000   # предположительно 4 МБ SPI-flash; всё вне образа = стёрто (0xFF)

def rom_flash_read(e):
    addr, buf, n = e.x[10], e.x[11], e.x[12]
    e.rom_log.append(('flash_read', addr, buf, n, e.x[1]))
    for i in range(n):
        o = addr + i
        v = e.flash[o] if o < len(e.flash) - 0x1000 else 0xff
        e.store(buf + i, 1, v)
    e.x[10] = 0

def rom_flash_write(e):
    addr, buf, n = e.x[10], e.x[11], e.x[12]
    e.rom_log.append(('flash_write', addr, buf, n, e.x[1]))
    e.x[10] = 0

def rom_flash_erase(e):
    e.rom_log.append(('flash_erase', e.x[10], e.x[11], e.x[12], e.x[1]))
    e.x[10] = 0

def rom_ret0(e):
    e.x[10] = 0

ROM = {
    0x00080318: rom_memcpy,
    0x00080410: rom_memset,
    0x0008225e: rom_flash_read,
    0x0008213c: rom_flash_erase,
    0x00082200: rom_flash_write,
    0x00082356: rom_ret0,     # flash: begin/unlock
    0x0008236e: rom_ret0,     # flash: end
}

def make(path):
    e = Emu(open(path, 'rb').read())
    e.rom_log = []
    for a, f in ROM.items():
        e.hooks[a] = f
    def unknown_rom(ee, pc):
        ee.rom_log.append(('unknown', pc, ee.x[10], ee.x[11], ee.x[12], ee.x[1]))
        print(f'[rom?] {pc:08x}(a0={ee.x[10]:08x}, a1={ee.x[11]:08x}, a2={ee.x[12]:08x}) ra={ee.x[1]:08x} -> 0', file=sys.stderr)
        ee.x[10] = 0
    e.unknown_rom = unknown_rom
    # вариант чипа (ветка 0x1350 в RAM-коде) и ID кристалла
    e.mmio_regs[0x20000000] = 0x49004080
    e.mmio_regs[0x20160028] = 0x12345678
    e.mmio_regs[0x2016002c] = 0x9abcdef0
    # заводской ключ привязки к чипу (flash 0x3e000) — в образе отсутствует; подставляем ожидаемый
    def chipkey(ee):
        sp = ee.x[2]
        for i in range(8):
            ee.store(sp + i, 1, ee.load(sp + 8 + i, 1))
    e.pre_hooks[0x000013a4] = chipkey
    # журнал записей в GPIO / IOMUX
    e.gpio_log = []
    def gpio_watch(ee, a, size, v):
        if 0x20005000 <= a < 0x20005100 or 0x21010000 <= a < 0x21010100:
            ee.gpio_log.append((ee.steps, ee.pc, ee.x[1], a, v, ee.mmio_regs.get(a & ~3, 0)))
    e.mmio_write_hooks.append(gpio_watch)
    return e

def print_gpio(e, last=None):
    rows = e.gpio_log if last is None else e.gpio_log[-last:]
    for st, pc, ra, a, v, reg in rows:
        print(f'  #{st:<9} pc={pc:08x} ra={ra:08x}  W {a:08x} <- {v:08x}   (рег={reg:08x})')

if __name__ == '__main__':
    e = make(sys.argv[1] if len(sys.argv) > 1 else 'firmware.bin')
    n = int(sys.argv[2], 0) if len(sys.argv) > 2 else 3_000_000
    try:
        e.run(n)
        print('лимит шагов')
    except (Stop, Unimpl) as ex:
        print('STOP:', ex)
    print(f'steps={e.steps} pc={e.pc:08x}')
    print(e.regs_str())
    print('GPIO/IOMUX записи:')
    print_gpio(e)
    print('MMIO по адресам:')
    acc = collections.Counter((k, a & ~3) for (_, _, k, a, _, _) in e.mmio_log)
    for (k, a), c in sorted(acc.items(), key=lambda t: t[0][1])[:200]:
        print(f'  {k} {a:08x} x{c}')
