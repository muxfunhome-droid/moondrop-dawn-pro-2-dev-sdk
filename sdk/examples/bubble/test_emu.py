#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Проверка fw_bubble.bin в эмуляторе:
  python3 sdk/examples/bubble/test_emu.py [build/fw_bubble.bin] [--base build/fw_custom2.bin] [--wav out.wav] [--sec 1.0]
Сначала соберите образ: scripts/build_app.sh bubble build/fw_custom2.bin build/fw_bubble.bin

1. bit-exact: с fx_on = 0 и без бульков замена 0x1668 даёт ровно то же, что заводская функция;
2. кнопки: нажатия «+»/«−» через уровни GPIO (0x20110110 бит 8, 0x20110114 бит 2) и тики MultiButton
   (0x26d0, как в таймерном прерывании) -> бульки запускаются, громкость меняется как в заводской;
3. рендер: тестовая «музыка» через тракт (EQ + warm + бульки) -> WAV, чтобы послушать.
"""
import sys, os, math, struct, random, copy, argparse, subprocess, wave

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..', '..', '..'))
TOOLS = os.path.join(ROOT, 'tools') if os.path.isdir(os.path.join(ROOT, 'tools')) else ROOT
sys.path.insert(0, TOOLS)
from led_run import make                                   # noqa: E402

SENT = 0x48036ff0          # адрес-«возврат»: эмулятор останавливается, не исполняя его
BUF = 0x13b000             # свободная RAM между данными программы и стеком
GPIO_LO, GPIO_HI = 0x20110110, 0x20110114
VOL = 0x11d22c


def find(name):                                     # build/<имя> (репозиторий) или <имя> (рабочая папка)
    for p in (os.path.join(ROOT, 'build', name), os.path.join(ROOT, name)):
        if os.path.exists(p): return p
    return os.path.join(ROOT, 'build', name)


def syms_of(elf):
    out = subprocess.run(['riscv64-unknown-elf-nm', elf], capture_output=True, text=True).stdout
    return {p[2]: int(p[0], 16) for p in (l.split() for l in out.splitlines()) if len(p) == 3}


def boot(path):
    e = make(path); e.log_mmio = False
    stderr = sys.stderr; sys.stderr = open(os.devnull, 'w')
    try:
        e.run(2_500_000)
        e.run(300_000, until={0x48002d8c})                  # стоим в начале прохода главного цикла
    finally:
        sys.stderr = stderr
    return e


def call(e, fn, *args, limit=5_000_000):
    saved = (e.pc, list(e.x))
    for i, a in enumerate(args): e.x[10 + i] = a & 0xffffffff
    e.x[1] = SENT; e.x[2] = (e.x[2] - 256) & ~15; e.pc = fn
    if e.run(limit, until={SENT}) != 'until':
        raise RuntimeError(f'вызов {fn:#x} не вернулся, pc={e.pc:08x}')
    r = e.x[10]
    e.pc, e.x[:] = saved[0], saved[1]
    return r


def s32(v): v &= 0xffffffff; return v - (1 << 32) if v & 0x80000000 else v


def put_frames(e, frames):
    for i, (l, r) in enumerate(frames):
        e.store(BUF + 8 * i, 4, l & 0xffffffff); e.store(BUF + 8 * i + 4, 4, r & 0xffffffff)


def get_frames(e, n):
    return [(s32(e.load(BUF + 8 * i, 4)), s32(e.load(BUF + 8 * i + 4, 4))) for i in range(n)]


def eq_block(e, frames, fs):
    """Как USB-обработчик 0x7018: >>2, EQ 0x1668, sclip 30 бит, <<2."""
    put_frames(e, [(l >> 2, r >> 2) for l, r in frames])
    call(e, 0x1668, BUF, len(frames), 10, fs)
    clip = lambda v: max(-(1 << 29), min((1 << 29) - 1, v)) << 2
    return [(clip(l), clip(r)) for l, r in get_frames(e, len(frames))]


def music(n, fs, t0=0):
    out = []
    for k in range(n):
        t = (t0 + k) / fs
        l = 0.22 * math.sin(2 * math.pi * 55 * t) + 0.12 * math.sin(2 * math.pi * 440 * t) * (0.5 + 0.5 * math.sin(2 * math.pi * 2 * t))
        r = 0.22 * math.sin(2 * math.pi * 55 * t) + 0.12 * math.sin(2 * math.pi * 660 * t)
        out.append((int(l * 2 ** 31), int(r * 2 ** 31)))
    return out


def set_buttons(e, plus=False, minus=False):        # активный ноль, подтяжка вверх
    e.mmio_regs[GPIO_LO] = 0xffffffff & ~((1 << 8) if plus else 0)
    e.mmio_regs[GPIO_HI] = 0xffffffff & ~((1 << 2) if minus else 0)


def ticks(e, n):
    for _ in range(n): call(e, 0x26d0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('fw', nargs='?', default=find('fw_bubble.bin'))
    ap.add_argument('--base', default=find('fw_custom2.bin'))
    ap.add_argument('--wav', default=os.path.join(os.path.dirname(find('fw_bubble.bin')), 'bubble_demo.wav'))
    ap.add_argument('--sec', type=float, default=1.0)
    a = ap.parse_args()
    S = syms_of(os.path.join(HERE, 'build', 'app.elf'))
    fs, blk = 48000, 48
    ok = True

    # ---------------------------------------------------------------- 1. bit-exact
    print('загрузка базы и программы в эмуляторе (~40 с)…', flush=True)
    base, e = boot(a.base), boot(a.fw)
    e.store(S['fx_on'], 1, 0)
    rnd = random.Random(1)
    same = True
    for b in range(6):
        fr = [(rnd.randint(-2 ** 31, 2 ** 31 - 1), rnd.randint(-2 ** 31, 2 ** 31 - 1)) for _ in range(blk)] if b % 2 else music(blk, fs, b * blk)
        x, y = eq_block(base, fr, fs), eq_block(e, fr, fs)
        same &= x == y
    print(f'1. fx off: EQ бит в бит с заводским: {"OK" if same else "РАЗНИЦА"}')
    ok &= same
    e.store(S['fx_on'], 1, 1)

    # ---------------------------------------------------------------- 2. кнопки
    set_buttons(e); ticks(e, 20)
    b0 = e.load(S['stat_bubbles'], 4); v0 = s32(e.load(VOL, 2) << 16) >> 16
    set_buttons(e, minus=True); ticks(e, 10)              # нажали «−»
    eq_block(e, music(blk, fs), fs)                       # пакет аудио запускает голос
    b1 = e.load(S['stat_bubbles'], 4)
    set_buttons(e); ticks(e, 10)                          # отпустили -> шаг громкости (PRESS_UP прошивки)
    v1 = s32(e.load(VOL, 2) << 16) >> 16
    print(f'2. «−»: бульков {b1 - b0} (ждём 1), громкость {v0} -> {v1} (ждём уменьшения)')
    ok &= b1 - b0 == 1 and v1 < v0
    ticks(e, 400)                                         # тайм-аут клика
    set_buttons(e, plus=True); ticks(e, 10); eq_block(e, music(blk, fs), fs)
    b2 = e.load(S['stat_bubbles'], 4)
    set_buttons(e); ticks(e, 10)
    v2 = s32(e.load(VOL, 2) << 16) >> 16
    print(f'   «+»: бульков {b2 - b1} (ждём 1), громкость {v1} -> {v2} (ждём увеличения)')
    ok &= b2 - b1 == 1 and v2 > v1
    ticks(e, 400)
    # двойной клик «−»
    for _ in range(2):
        set_buttons(e, minus=True); ticks(e, 8); set_buttons(e); ticks(e, 8)
    ticks(e, 400)
    tog = e.load(S['fx_toggle_req'], 1)
    print(f'   двойной клик «−»: запросов переключения {tog} (ждём 1)')
    ok &= tog == 1
    # удержание «+»: автоповтор
    set_buttons(e, minus=True); ticks(e, 10); set_buttons(e); ticks(e, 400)  # чуть тише, чтобы было куда расти
    for _ in range(3):
        set_buttons(e, minus=True); ticks(e, 8); set_buttons(e); ticks(e, 400)
    hv0 = s32(e.load(VOL, 2) << 16) >> 16; hr0 = e.load(S['req'] + 2, 1)
    set_buttons(e, plus=True); ticks(e, 700)
    hv1 = s32(e.load(VOL, 2) << 16) >> 16; hr1 = e.load(S['req'] + 2, 1)
    set_buttons(e); ticks(e, 400)
    print(f'   удержание «+» 700 тиков: громкость {hv0} -> {hv1}, мягких бульков {(hr1 - hr0) & 255}')
    ok &= hv1 > hv0 and (hr1 - hr0) & 255 > 0

    # ---------------------------------------------------------------- 3. рендер
    e.store(S['fx_on'], 1, 1)
    # сброс очереди бульков, чтобы демо началось с чистого листа
    for k in range(7): e.store(S['seen'] + k, 1, e.load(S['req'] + k, 1))
    total = int(a.sec * fs) // blk
    plan = {int(0.15 * fs) // blk: 'plus', int(0.45 * fs) // blk: 'minus', int(0.75 * fs) // blk: 'plus'}
    out_fx, out_dry = [], []
    print(f'3. рендер {a.sec:.1f} с через тракт (в эмуляторе это несколько минут)…', flush=True)
    for b in range(total):
        if b in plan:
            set_buttons(e, **{plan[b]: True}); ticks(e, 6); set_buttons(e); ticks(e, 6)
        fr = music(blk, fs, b * blk)
        out_fx += eq_block(e, fr, fs)
        out_dry += eq_block(base, fr, fs)
    for path, data in ((a.wav, out_fx), (a.wav.replace('.wav', '_factory.wav'), out_dry)):
        with wave.open(path, 'wb') as w:
            w.setnchannels(2); w.setsampwidth(2); w.setframerate(fs)
            w.writeframes(b''.join(struct.pack('<hh', l >> 16, r >> 16) for l, r in data))
        print('   записано', path)
    peak = max(max(abs(l), abs(r)) for l, r in out_fx) / 2 ** 31
    diff = sum(abs(x[0] - y[0]) for x, y in zip(out_fx, out_dry)) / len(out_fx) / 2 ** 31
    print(f'   пик {20 * math.log10(peak):.1f} дБFS, средняя разница с заводским {diff:.4f}')
    print(f'   вызовов EQ {e.load(S["stat_calls"], 4)}, бульков всего {e.load(S["stat_bubbles"], 4)}')
    print('Итог:', 'OK' if ok else 'ЕСТЬ ПРОБЛЕМЫ')
    return 0 if ok else 2


if __name__ == '__main__':
    sys.exit(main())
