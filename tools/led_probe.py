#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Проверка логики светодиода в эмуляторе: прогоняет прошивку до главного цикла,
выставляет EQ-пресет / LED-событие / флаг LED и печатает, что пишется в ШИМ и пады LED."""
import sys
from led_run import make

PADS = {0x2011000c: 'pad3(R)', 0x20110084: 'pad33(G)', 0x20110014: 'pad5(B)'}
PWM_BASE = 0x20100000

def scenario(path, title, mode=None, event=None, ev_param=None, led_en=None, steps=400000):
    e = make(path)
    e.run(2500000)                      # загрузка до главного цикла
    if mode is not None: e.store(0x10648c, 1, mode)
    if led_en is not None: e.store(0x11cb1f, 1, led_en)
    if event is not None:
        e.store(0x1064b8, 1, event)
        p1, total, half = ev_param or (1, 500, 500)
        e.store(0x1064b9, 1, p1); e.store(0x1064bc, 2, total); e.store(0x1064ba, 2, half)
    log = []
    def w(ee, a, size, v):
        if PWM_BASE <= a < PWM_BASE + 0x20 or a in PADS:
            log.append((ee.steps, ee.pc, a, v))
    e.mmio_write_hooks.append(w)
    e.log_mmio = False
    e.run(steps)
    print(f'== {title}')
    seen = []
    for st, pc, a, v in log:
        if a in PADS:
            s = f'{PADS[a]:9} <- 0x{v:x}' + ('  (функция ШИМ)' if v == 9 else '')
        else:
            ch = (a - PWM_BASE) // 4
            s = f'PWM ch{ch}   <- duty={v >> 16:3d}/255  (raw 0x{v:08x})'
        if s not in seen:
            seen.append(s); print(f'   pc={pc:08x}  {s}')
    if not log: print('   (записей в LED нет)')
    return log

if __name__ == '__main__':
    p = sys.argv[1] if len(sys.argv) > 1 else 'firmware.bin'
    scenario(p, 'обычный режим, EQ-пресет 8 (заводской по умолчанию)', mode=8, event=0)
    scenario(p, 'обычный режим, EQ-пресет 0', mode=0, event=0)
    scenario(p, 'событие 2 (предел громкости) — мигание', mode=0, event=2, ev_param=(1, 500, 500))
    scenario(p, 'событие 9', mode=0, event=9, ev_param=(0, 100, 50))
    scenario(p, 'событие 6', mode=0, event=6, ev_param=(0, 100, 50))
    scenario(p, 'LED выключен в настройках (st[15]=0)', mode=0, event=0, led_en=0)
