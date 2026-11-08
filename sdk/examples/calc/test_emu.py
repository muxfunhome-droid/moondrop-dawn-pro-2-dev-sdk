#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Проверка build/fw_calc.bin в эмуляторе: HID-отчёты подаются прямо в буфер прошивки, вызывается
обработчик 0x8d84 (перенаправлен на calc_hid), отправка 0x8418 перехватывается.
  python3 -I sdk/examples/calc/test_emu.py [build/fw_calc.bin]
  (сначала: scripts/build_app.sh calc build/fw_custom2.bin build/fw_calc.bin)
"""
import sys, os, struct, math

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..', '..', '..'))
sys.path.insert(0, os.path.join(ROOT, 'tools') if os.path.isdir(os.path.join(ROOT, 'tools')) else ROOT)
from led_run import make                                   # noqa: E402

SENT, CTX, MSG, BUF = 0x48036ff0, 0x121ec0, 0x106554, 0x13b000


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
    assert e.run(3_000_000, until={SENT}) == 'until', f'{fn:#x} не вернулся, pc={e.pc:08x}'
    e.pc, e.x[:] = saved[0], saved[1]


def report(e, data):
    sent.clear()
    data = bytes(data) + bytes(64 - len(data))
    for i, b in enumerate(data): e.store(BUF + i, 1, b)
    e.store(MSG + 4, 4, BUF); e.store(MSG, 4, 2)
    call(e, 0x8d84, CTX, MSG, 0)
    return e.load(MSG, 4), list(sent)


def main():
    global sent
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, 'build', 'fw_calc.bin')
    print('загрузка в эмуляторе (~20 с)…', flush=True)
    e = boot(path)
    sent = []
    def send_hook(ee):                     # 0x8418(ctx, buf, len): запомнить отправленный отчёт
        sent.append(bytes(ee.load(ee.x[11] + i, 1) for i in range(ee.x[12])))
    e.hooks[0x8418] = send_hook

    cases = ['2+2*2', '(2+2)*2', '-3^2', '2^-2', '2^3^2', '-(2+3)*-2', '2^10', '10/4', '7 % 3', '1.5e3+0.25', 'sqrt(2)', 'pi*2',
             '1/0', '2+*3', '((1)', '2^0.5', 'sqrt(-1)', '3.14159', '1+2+3+4+5+6+7+8+9+10']
    ok = True
    for seq, ex in enumerate(cases, 1):
        st, out = report(e, [75, 0xCA, seq, len(ex)] + list(ex.encode()))
        assert st == 1 and len(out) == 1, (ex, st, out)
        r = out[0]
        status, pos = r[3], r[4]
        val = struct.unpack_from('<d', r, 8)[0]
        cyc, cnt = struct.unpack_from('<II', r, 16)
        try:
            py = eval(ex.replace('^', '**').replace('sqrt', 'math.sqrt').replace('pi', 'math.pi'))
            if isinstance(py, complex): raise ValueError
            if '^' in ex and not float(ex.split('^')[1].strip('()')).is_integer(): raise ValueError
            exp_st = 0
        except ZeroDivisionError: py, exp_st = None, 2
        except SyntaxError: py, exp_st = None, 1
        except ValueError: py, exp_st = None, 4
        good = bool(r[:3] == bytes([75, 0xCA, seq]) and status == exp_st and (status or math.isclose(val, py, rel_tol=1e-12)))
        ok &= good
        print(f'{"OK " if good else "!! "} {ex:24} -> status {status} pos {pos:2}  {val:<22.15g} ({cyc} тактов, №{cnt})')

    # заводские команды не сломаны: [75,128,0] отвечает отчётом прошивки, [75,1,0] просто принимается
    st, out = report(e, [75, 128, 0, 16])
    good = st == 1 and len(out) == 1 and out[0][:2] == bytes([75, 128])
    print(f'{"OK " if good else "!! "} заводская команда [75,128,0]: ответ прошивки {out[0][:4].hex() if out else "нет"}')
    ok &= good
    st, out = report(e, [76, 0xCA, 1, 3] + list(b'1+1'))
    good = st == 2 and not out            # другой report ID: как в оригинале, не трогаем
    print(f'{"OK " if good else "!! "} чужой report ID 76: не обработан, как в заводской')
    ok &= good
    print('Итог:', 'OK' if ok else 'ЕСТЬ ПРОБЛЕМЫ')
    return 0 if ok else 2


if __name__ == '__main__':
    sys.exit(main())
