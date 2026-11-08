#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Проверка build/fw_life.bin в эмуляторе: запрос кладётся в HID-буфер прошивки, вызывается обработчик 0x8d84
(перенаправлен на life_hid), потом app_loop() крутится до ответа (отправка 0x8418 перехватывается).
Результат сверяется с эталоном на Python.
  python3 -I sdk/examples/life/test_emu.py [build/fw_life.bin]
  (сначала: scripts/build_app.sh life build/fw_custom2.bin build/fw_life.bin)
"""
import sys, os, struct, random, subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..', '..', '..'))
sys.path.insert(0, os.path.join(ROOT, 'tools') if os.path.isdir(os.path.join(ROOT, 'tools')) else ROOT)
from led_run import make                                   # noqa: E402

SENT, CTX, MSG, BUF = 0x48036ff0, 0x121ec0, 0x106554, 0x13b000
N = 10


def boot(path):
    e = make(path); e.log_mmio = False
    err = sys.stderr; sys.stderr = open(os.devnull, 'w')
    try:
        e.run(2_500_000); e.run(300_000, until={0x48002d8c})
    finally:
        sys.stderr = err
    return e


def call(e, fn, *args):
    saved = (e.pc, list(e.x))
    for i, a in enumerate(args): e.x[10 + i] = a & 0xffffffff
    e.x[1] = SENT; e.x[2] = (e.x[2] - 256) & ~15; e.pc = fn
    assert e.run(20_000_000, until={SENT}) == 'until', f'{fn:#x} не вернулся, pc={e.pc:08x}'
    e.pc, e.x[:] = saved[0], saved[1]


# ---------------------------------------------------------------- эталон
def ref_step(cells, wrap):
    nxt = set()
    for r in range(N):
        for c in range(N):
            n = 0
            for dr in (-1, 0, 1):
                for dc in (-1, 0, 1):
                    if dr == dc == 0: continue
                    rr, cc = r + dr, c + dc
                    if wrap: rr %= N; cc %= N
                    elif not (0 <= rr < N and 0 <= cc < N): continue
                    n += (rr, cc) in cells
            if n == 3 or (n == 2 and (r, c) in cells): nxt.add((r, c))
    return frozenset(nxt)


def ref_run(cells, steps, wrap):
    seen, hist, cur = {}, [], frozenset(cells)
    for t in range(steps):
        if cur in seen:                          # цикл: дальше по модулю периода
            s = seen[cur]; return hist[s + (steps - s) % (t - s)]
        seen[cur] = t; hist.append(cur)
        cur = ref_step(cur, wrap)
    return cur


def pack(cells):
    b = bytearray(13)
    for r, c in cells: k = r * N + c; b[k >> 3] |= 1 << (k & 7)
    return bytes(b)


def unpack(b):
    return frozenset((k // N, k % N) for k in range(N * N) if b[k >> 3] >> (k & 7) & 1)


def show(cells):
    return '\n'.join(''.join('#' if (r, c) in cells else '.' for c in range(N)) for r in range(N))


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, 'build', 'fw_life.bin')
    elf = os.path.join(HERE, 'build', 'app.elf')
    syms = {p[2]: int(p[0], 16) for p in (l.split() for l in subprocess.run(
        ['riscv64-unknown-elf-nm', elf], capture_output=True, text=True).stdout.splitlines()) if len(p) == 3}
    print('загрузка в эмуляторе (~20 с)…', flush=True)
    e = boot(path)
    sent = []
    e.hooks[0x8418] = lambda ee: sent.append(bytes(ee.load(ee.x[11] + i, 1) for i in range(ee.x[12])))

    glider = {(0, 1), (1, 2), (2, 0), (2, 1), (2, 2)}
    rnd = random.Random(7)
    soup = {(r, c) for r in range(N) for c in range(N) if rnd.random() < 0.35}
    cases = [('глайдер, 1 шаг', glider, 1, True), ('глайдер, 4 шага', glider, 4, True),
             ('глайдер, 40 шагов (полный круг по тору)', glider, 40, True),
             ('глайдер без тора, 30 шагов (разбивается о край)', glider, 30, False),
             ('мигалка, 7 шагов', {(4, 3), (4, 4), (4, 5)}, 7, True),
             ('блок, 1000 шагов', {(4, 4), (4, 5), (5, 4), (5, 5)}, 1000, True),
             ('случайный суп, 50 шагов', soup, 50, True),
             ('случайный суп без тора, 50 шагов', soup, 50, False),
             ('глайдер, 4 000 000 001 шаг (через период)', glider, 4_000_000_001, True),
             ('пустое поле, 5 шагов', set(), 5, True),
             ('суп с переходом 297 шагов, 1000 шагов, порция 7 шагов', 'seed2773', 1000, True)]
    ok = True
    for seq, (name, cells, steps, wrap) in enumerate(cases, 1):
        if cells == 'seed2773':                  # счёт в несколько проходов app_loop
            rr = random.Random(2773)
            cells = {(r, c) for r in range(N) for c in range(N) if rr.random() < 0.3}
            e.store(syms['steps_per_pass'], 4, 7)
        sent.clear()
        req = bytes([75, 0xCB, seq, 0 if wrap else 1]) + struct.pack('<I', steps) + pack(cells)
        req += bytes(64 - len(req))
        for i, b in enumerate(req): e.store(BUF + i, 1, b)
        e.store(MSG + 4, 4, BUF); e.store(MSG, 4, 2)
        call(e, 0x8d84, CTX, MSG, 0)
        passes = 0
        while not sent and passes < 400:
            call(e, syms['app_loop']); passes += 1
        if not sent:
            print(f'!!  {name}: нет ответа'); ok = False; continue
        r = sent[0]
        got = unpack(r[8:21])
        exp = ref_run(cells, steps, wrap)
        period, found = struct.unpack_from('<II', r, 32)
        good = r[:4] == bytes([75, 0xCB, seq, 0]) and struct.unpack_from('<I', r, 4)[0] == steps \
            and got == exp and r[28] == len(exp)
        ok &= good
        extra = f', период {period} (найден на шаге {found})' if period else ''
        print(f'{"OK " if good else "!! "} {name}: население {r[28]}, проходов app_loop {passes}{extra}')
        if not good:
            print('чип:\n' + show(got) + '\nэталон:\n' + show(exp))
    print('Итог:', 'OK' if ok else 'ЕСТЬ ПРОБЛЕМЫ')
    return 0 if ok else 2


if __name__ == '__main__':
    sys.exit(main())
