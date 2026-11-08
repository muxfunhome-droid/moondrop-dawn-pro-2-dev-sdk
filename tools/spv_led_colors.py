#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
spv_led_colors.py — смена цветов и яркости светодиода во всех режимах прошивки SPV-ЦАПа.

Как работает. Каждый из 9 вызовов led_set_rgb(r,g,b,on,brightness) @0x48004288
перенаправляется на свою заглушку (stub) в области патчей сразу за секцией CPU:
    addi a0,zero,R ; addi a1,zero,G ; addi a2,zero,B ; addi a4,zero,BR ; j led_set_rgb
(a3 = on = 1 остаётся от места вызова). Секция CPU расширяется, checksum пересчитывается.
Запуск на уже патченом образе меняет те же заглушки (идемпотентно).

Примеры:
  python3 spv_led_colors.py firmware.bin --list
  python3 spv_led_colors.py firmware.bin -o fw_led.bin --set normal=255,0,0 --set volume=#00ff00@64
  python3 spv_led_colors.py firmware.bin -o fw_led.bin --set all=off --set normal=0,0,255@40 --check
  python3 spv_led_colors.py firmware.bin -o fw_led.bin --preset8 normal --set normal=255,64,0
  # LED с общим анодом (проверено на железе: без инверсии «зелёный» светился фиолетовым):
  python3 spv_led_colors.py firmware.bin -o fw_led.bin --invert on --set all=160,0,255 --set normal=0,255,0 --check

Цвет: R,G,B (0..255) | #RRGGBB | off ; необязательно @яркость (0..255, по умолчанию 255).
Имена режимов: см. --list. Группы: all, red, green, blue (= *_blink + *_solid), white, purple.
Поддерживается только сборка прошивки «Nov 17 2025 13:50:18»: перед патчем проверяются байты.
"""
import sys, os, struct, argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from spv_fw_tool import Firmware          # noqa: E402
from spv_emu import decode32, decode16     # noqa: E402

FLASH = 0x48000000
RAM_CODE_FOFF = 0x10a60                    # RAM-код по адресу 0 лежит в файле с этого смещения
LED_SET_RGB = 0x48004288
PATCH_FOFF = 0x20ae0                       # область патчей (после конца .data-образа 0x20ad8)
STUB_SIZE = 20
PRESET8_BEQC_FOFF = RAM_CODE_FOFF + 0x730  # beqc a5,8,0x75c — «при пресете 8 не включать ШИМ-цвет»
NOP = 0x00000013
# Полярность ШИМ. В led_set_rgb после расчёта duty стоит `bfoz a2,a2,15,0` (3 раза: B, R, G).
# Если светодиод с общим анодом (на железе: «зелёный» (0,255,0) горел фиолетовым = R+B), то duty надо
# инвертировать: заменяем bfoz на `xori a2,a2,255` (4 Б в 4 Б, duty 0..255 -> 255-duty).
INV_FOFFS = (0x42b4, 0x42cc, 0x42e4)       # flash 0x48004288 + ... (смещение в файле = адрес - 0x48000000)
BFOZ_ORIG = 0x3c06265b                     # bytes 5b26063c LE
XORI_A2_255 = (255 << 20) | (12 << 15) | (4 << 12) | (12 << 7) | 0x13

# name, адрес вызова (flash 0x48… или RAM 0x…), заводской цвет (r,g,b,br), описание
SITES = [
    ('normal',       0x00000744, (0, 220, 255, 255), 'обычное состояние: событие 0, EQ-пресет != 8 (см. --preset8)'),
    ('white_blink',  0x48003004, (255, 255, 255, 255), 'режим 0, мигание: события 1, 2 (2 = громкость в пределе)'),
    ('purple_blink', 0x48003048, (102, 0, 255, 255), 'режим 5, мигание: события 3, 4, 5'),
    ('red_blink',    0x48003084, (255, 0, 0, 255), 'режим 2, мигание: события 6, 7'),
    ('red_solid',    0x48003098, (255, 0, 0, 255), 'режим 2 без мигания (параметр blink = 0)'),
    ('green_blink',  0x480030d4, (0, 255, 0, 255), 'режим 1, мигание: событие 8 (после 7)'),
    ('green_solid',  0x480030ec, (0, 255, 0, 255), 'режим 1 без мигания'),
    ('blue_blink',   0x48003128, (0, 0, 255, 255), 'режим 3, мигание: событие 9'),
    ('blue_solid',   0x4800313c, (0, 0, 255, 255), 'режим 3 без мигания'),
    # Заводское поведение при EQ-пресете 8 (это «обычный режим» устройства по умолчанию): ШИМ не включается,
    # пад R <- 0x10 (красный горит), пады G/B <- 0x210 (погашены). Патч вместо этого вызывает led_set_rgb.
    ('normal8',      0x0000075c, (None, None, None, None), 'обычный режим при EQ-пресете 8 (заводское: просто красный, без ШИМ)'),
]
NORMAL8_FACTORY = bytes.fromhex('b70711201307002123a2e708c146d4c7d8cbf9bf')   # 20 Б @RAM 0x75c, смещение 0x10a60+0x75c
NORMAL8_RETURN = 0x74c                                                      # куда возвращаться (задержка и выход)
# Фаза «погашено» при мигании: пады всех трёх каналов <- 0x10. На железе это состояние ЗАЖИГАЕТ светодиод
# (белая вспышка). Заменяем на 0x210 (как в «погашенном» состоянии пресета 8 у G/B).
BLINKOFF_SITES = (0x48003014, 0x48003050, 0x480030a0, 0x480030f4, 0x48003144)   # блоки по 16 Б, все возвращаются в 0x48003008
BLINKOFF_HEAD = bytes.fromhex('b70711204147d8cbd8c723a2e708')                   # lui a5,0x20110; c.li a4,16; c.sw; c.sw; sw 132(a5)
BLINKOFF_BLOCK = 16
BLINKOFF_RET = 0x48003008
BLINKOFF_STUB_SIZE = 24
BLINKOFF_TAILS = tuple(bytes.fromhex(x) for x in ('ddb7', '6db7', 'a9bf', '19b7', '5dbd'))   # исходные c.j каждого блока
C_NOP2 = 0x00010001
GROUPS = {
    'all': [s[0] for s in SITES],
    'red': ['red_blink', 'red_solid'], 'green': ['green_blink', 'green_solid'],
    'blue': ['blue_blink', 'blue_solid'], 'white': ['white_blink'], 'volume': ['white_blink'],
    'purple': ['purple_blink'],
    'normal_all': ['normal', 'normal8'],
}
BLINKOFF_FOFF = PATCH_FOFF + STUB_SIZE * len(SITES)
PATCH_END = BLINKOFF_FOFF + BLINKOFF_STUB_SIZE

# ------------------------------------------------------------------ кодирование RV32
def enc_addi(rd, imm):
    assert -2048 <= imm < 2048
    return ((imm & 0xfff) << 20) | (rd << 7) | 0x13

def enc_jal(rd, off):
    assert -(1 << 20) <= off < (1 << 20) and off % 2 == 0
    o = off & 0x1fffff
    return (((o >> 20) & 1) << 31) | (((o >> 1) & 0x3ff) << 21) | (((o >> 11) & 1) << 20) | \
           (((o >> 12) & 0xff) << 12) | (rd << 7) | 0x6f

def enc_auipc(rd, imm20):
    return ((imm20 & 0xfffff) << 12) | (rd << 7) | 0x17

def enc_jalr(rd, rs1, imm):
    return ((imm & 0xfff) << 20) | (rs1 << 15) | (rd << 7) | 0x67

def hi_lo(off):
    lo = off & 0xfff
    if lo >= 0x800: lo -= 0x1000
    return ((off - lo) >> 12) & 0xfffff, lo

def foff_of(addr):
    return addr - FLASH if addr >= FLASH else RAM_CODE_FOFF + addr

def flash_addr_of_foff(foff):
    return FLASH + foff

def u32(d, o): return struct.unpack_from('<I', d, o)[0]
def put32(d, o, v): struct.pack_into('<I', d, o, v & 0xffffffff)

# ------------------------------------------------------------------ разбор мест вызова
def call_target(d, addr):
    """Куда сейчас ведёт вызов в месте addr. -> (target, kind)"""
    o = foff_of(addr)
    if addr >= FLASH:
        m = decode32(u32(d, o))
        if m[0] != 'jal' or m[1] != 1:
            raise ValueError(f'{addr:08x}: ожидался jal ra, найдено {m[0]}')
        return (addr + m[4]) & 0xffffffff, 'jal'
    a = decode32(u32(d, o)); j = decode32(u32(d, o + 4))
    if a[0] != 'auipc' or a[1] != 1 or j[0] != 'jalr' or j[1] != 1 or j[2] != 1:
        raise ValueError(f'{addr:08x}: ожидалась пара auipc ra / jalr ra')
    return (addr + a[4] + j[4]) & 0xffffffff, 'auipc'

def stub_addr(i):
    return flash_addr_of_foff(PATCH_FOFF + STUB_SIZE * i)

def read_stub(d, i):
    o = PATCH_FOFF + STUB_SIZE * i
    vals = []
    for k, rd in enumerate((10, 11, 12, 14)):
        m = decode32(u32(d, o + 4 * k))
        if m[0] != 'addi' or m[1] != rd or m[2] != 0:
            return None
        vals.append(m[4])
    return tuple(vals)

def current_colors(fw):
    d = fw.d; out = {}
    for i, (name, addr, default, _) in enumerate(SITES):
        if name == 'normal8' and bytes(d[foff_of(addr):foff_of(addr) + 20]) == NORMAL8_FACTORY:
            out[name] = (default, 'заводской'); continue
        tgt, _ = call_target(d, addr + 4 if name == 'normal8' else addr)
        if tgt == LED_SET_RGB:
            out[name] = (default, 'заводской')
        elif tgt == stub_addr(i) and read_stub(d, i):
            out[name] = (read_stub(d, i), 'патч')
        else:
            raise ValueError(f'{name}: вызов ведёт в неизвестный адрес {tgt:08x}')
    return out

# ------------------------------------------------------------------ патч
def check_build(fw):
    if fw.build_date != 'Nov 17 2025' or fw.build_time != '13:50:18':
        raise SystemExit(f'Неподдерживаемая сборка: {fw.build_date} {fw.build_time}. '
                         f'Адреса мест вызова известны только для «Nov 17 2025 13:50:18».')
    cpu = fw.sections[0]
    if cpu.name != 'CPU' or cpu.address != FLASH:
        raise SystemExit('Первая секция не CPU@0x48000000')
    return cpu

def ensure_patch_area(fw, cpu):
    d = fw.d
    if cpu.size >= PATCH_END:
        return
    if cpu.size != 0x20ad4:
        raise SystemExit(f'Неожиданный размер CPU 0x{cpu.size:x}')
    if any(d[cpu.size:PATCH_END]):
        raise SystemExit('Область патчей занята (не нули) — отказ')
    # 0x20ad4..0x20ae0: на устройстве здесь стёртая flash (0xFF) — сохраняем как было
    d[cpu.size:PATCH_FOFF] = b'\xff' * (PATCH_FOFF - cpu.size)
    cpu.size = PATCH_END
    put32(d, cpu.entry_off + 8, cpu.size)          # поле size записи директории

def enc_sw(rs2, rs1, imm):
    return (((imm >> 5) & 0x7f) << 25) | (rs2 << 20) | (rs1 << 15) | (2 << 12) | ((imm & 0x1f) << 7) | 0x23

def blinkoff_dark(d):
    st = []
    for a in BLINKOFF_SITES:
        o = a - FLASH
        if bytes(d[o:o + 14]) == BLINKOFF_HEAD: st.append(False)
        elif decode32(u32(d, o))[0] == 'jal' and decode32(u32(d, o))[1] == 0: st.append(True)
        else: raise SystemExit(f'блок мигания @{a:08x}: неизвестные байты')
    if len(set(st)) != 1: raise SystemExit('блоки «погашено» в разном состоянии')
    return st[0]

def set_blinkoff(fw, dark):
    d = fw.d; cpu = fw.sections[0]
    ensure_patch_area(fw, cpu)
    stub = FLASH + BLINKOFF_FOFF
    for k, w in enumerate((0x201107b7, enc_addi(14, 0x210), enc_sw(14, 15, 20), enc_sw(14, 15, 12),
                           enc_sw(14, 15, 132), enc_jal(0, BLINKOFF_RET - (stub + 20)))):
        put32(d, BLINKOFF_FOFF + 4 * k, w)
    for a, tail in zip(BLINKOFF_SITES, BLINKOFF_TAILS):
        o = a - FLASH
        if dark:
            put32(d, o, enc_jal(0, stub - a))
            put32(d, o + 4, C_NOP2); put32(d, o + 8, C_NOP2); put32(d, o + 12, C_NOP2)
        else:
            d[o:o + 14] = BLINKOFF_HEAD; d[o + 14:o + 16] = tail
    assert blinkoff_dark(d) == dark

def is_inverted(d):
    w = [u32(d, o) for o in INV_FOFFS]
    if all(x == XORI_A2_255 for x in w): return True
    if all(x == BFOZ_ORIG for x in w): return False
    raise SystemExit(f'led_set_rgb: неожиданные байты в точках инверсии: {[hex(x) for x in w]}')

def apply(fw, settings, preset8=None, invert=None, blink_off=None):
    cpu = check_build(fw)
    d = fw.d
    cur = current_colors(fw)
    if invert is not None:
        is_inverted(d)                                   # проверка, что байты известные
        for o in INV_FOFFS:
            put32(d, o, XORI_A2_255 if invert else BFOZ_ORIG)
    if settings:
        ensure_patch_area(fw, cpu)
    for i, (name, addr, default, _) in enumerate(SITES):
        if name not in settings:
            continue
        r, g, b, br = settings[name]
        s = stub_addr(i); so = PATCH_FOFF + STUB_SIZE * i
        for k, (rd, v) in enumerate(((10, r), (11, g), (12, b), (14, br))):
            put32(d, so + 4 * k, enc_addi(rd, v))
        put32(d, so + 16, enc_jal(0, LED_SET_RGB - (s + 16)))   # хвостовой вызов, ra сохранён
        o = foff_of(addr)
        if name == 'normal8':
            # 20 Б: addi a3,zero,1 ; auipc ra ; jalr ra -> stub ; jal zero,0x74c ; 2 × c.nop
            if bytes(d[o:o + 20]) != NORMAL8_FACTORY and u32(d, o) != enc_addi(13, 1):
                raise SystemExit('normal8: на 0x75c не заводские байты и не наш патч')
            hi, lo = hi_lo((s - (addr + 4)) & 0xffffffff)
            put32(d, o, enc_addi(13, 1)); put32(d, o + 4, enc_auipc(1, hi)); put32(d, o + 8, enc_jalr(1, 1, lo))
            put32(d, o + 12, enc_jal(0, NORMAL8_RETURN - (addr + 12))); put32(d, o + 16, C_NOP2)
            assert call_target(d, addr + 4)[0] == s
            continue
        if addr >= FLASH:
            put32(d, o, enc_jal(1, s - addr))
        else:
            hi, lo = hi_lo((s - addr) & 0xffffffff)
            put32(d, o, enc_auipc(1, hi)); put32(d, o + 4, enc_jalr(1, 1, lo))
        assert call_target(d, addr)[0] == s
    if blink_off is not None:
        set_blinkoff(fw, blink_off == 'dark')
    if preset8 is not None:
        o = PRESET8_BEQC_FOFF
        w = u32(d, o); m = decode32(w)
        if preset8 == 'normal':
            if w == NOP:
                pass
            elif m[0] == 'beqc' and m[5] == 8:
                fw.preset8_orig = w
                put32(d, o, NOP)
            else:
                raise SystemExit('preset8: не найден beqc a5,8 по ожидаемому адресу')
        elif preset8 == 'factory':
            if w == NOP:
                put32(d, o, 0x0287d65b)   # beqc a5,8,0x75c — исходная инструкция
                assert decode32(u32(d, o))[0] == 'beqc'
    fw.recompute_all()

def parse_color(spec):
    spec = spec.strip().lower()
    br = 255
    if '@' in spec:
        spec, b = spec.split('@', 1); br = int(b, 0)
    if spec == 'off':
        rgb = (0, 0, 0)
    elif spec.startswith('#') and len(spec) == 7:
        rgb = tuple(int(spec[i:i + 2], 16) for i in (1, 3, 5))
    else:
        rgb = tuple(int(x, 0) for x in spec.split(','))
    if len(rgb) != 3 or not all(0 <= v <= 255 for v in rgb + (br,)):
        raise ValueError(f'цвет «{spec}»: нужно R,G,B 0..255, #RRGGBB или off; яркость 0..255')
    return rgb + (br,)

def expand(name):
    if name in GROUPS: return GROUPS[name]
    if name in [s[0] for s in SITES]: return [name]
    raise ValueError(f'неизвестный режим «{name}». Доступны: {", ".join([s[0] for s in SITES] + list(GROUPS))}')

# ------------------------------------------------------------------ проверка в эмуляторе
def emu_check(path, settings_view, preset8_normal, inverted=False, blink_dark=False):
    import copy
    from led_run import make
    print('\nПроверка в эмуляторе (загрузка до главного цикла)...')
    e = make(path); e.log_mmio = False
    e.run(2500000)
    e.mmio_log.clear(); e.gpio_log.clear()
    cases = [  # (режим, mode, event, blink)
        ('normal', 0, 0, 0), ('white_blink', 0, 2, 1), ('purple_blink', 0, 3, 1),
        ('red_blink', 0, 6, 1), ('red_solid', 0, 6, 0), ('green_blink', 0, 8, 1),
        ('green_solid', 0, 8, 0), ('blue_blink', 0, 9, 1), ('blue_solid', 0, 9, 0),
    ]
    if preset8_normal:
        cases.append(('normal', 8, 0, 0))
    cases.append(('normal8', 8, 0, 0))
    # фаза «погашено»: (имя, mode, event, blink, счётчик) — счётчик % 500 <= 250 -> пады вместо ШИМ
    off_cases = [(n, 0, ev, 1, 100) for n, ev in (('white_blink', 2), ('purple_blink', 3), ('red_blink', 6),
                                                  ('green_blink', 8), ('blue_blink', 9))]
    ok_all = True
    for name, mode, ev, blink, cnt in [c + (499,) for c in cases] + off_cases:
        x = copy.deepcopy(e)
        x.store(0x10648c, 1, mode); x.store(0x1064b8, 1, ev)
        x.store(0x1064b9, 1, blink); x.store(0x1064bc, 2, 500); x.store(0x1064ba, 2, cnt)
        cur, seen, pads = {}, [], {}
        def padhook(ee, a, size, v, pads=pads):
            if a in (0x2011000c, 0x20110014, 0x20110084): pads[a] = v
        def hook(ee, a, size, v, cur=cur, seen=seen):
            # led_set_rgb пишет каналы в порядке B(ch5), R(ch3), G(ch1); запись G с enable = цвет готов
            if a in (0x2010000c, 0x20100004, 0x20100014) and v & 1:
                cur[a] = v >> 16
                if a == 0x20100004:
                    t = (cur.get(0x2010000c), cur.get(0x20100004), cur.get(0x20100014))
                    if t not in seen: seen.append(t)
        x.mmio_write_hooks.append(hook); x.mmio_write_hooks.append(padhook)
        x.run(300000)
        if cnt != 499:      # проверка фазы «погашено»
            want = 0x210 if blink_dark else 0x10
            ok = len(pads) == 3 and all(v == want for v in pads.values())
            ok_all &= ok
            print(f'  [{"OK " if ok else "BAD"}] {name + " (погашено)":24} пады {" ".join(hex(v) for v in pads.values()) or "—"}; ожидалось все {want:#x} ({"темно" if blink_dark else "заводское"})')
            continue
        (er, eg, eb, ebr) = settings_view[name]
        if er is None:      # normal8 заводской: R-пад 0x10, G/B 0x210, ШИМ не трогается
            ok = pads.get(0x2011000c) == 0x10 and pads.get(0x20110014) == 0x210 and pads.get(0x20110084) == 0x210 and not seen
            ok_all &= ok
            print(f'  [{"OK " if ok else "BAD"}] {name + " (EQ-пресет 8)":24} заводское: красный пад; пады {" ".join(hex(v) for v in pads.values())}')
            continue
        f = lambda c: int(round(struct.unpack('<f', struct.pack('<f', c * (ebr / 255.0)))[0]))
        exp = (f(er), f(eg), f(eb))
        if inverted: exp = tuple(255 - v for v in exp)
        ok = exp in seen
        ok_all &= ok
        tag = f'{name}' + (' (EQ-пресет 8)' if mode == 8 else '')
        shown = ' '.join(f'({r},{g},{b})' for r, g, b in seen) or '—'
        print(f'  [{"OK " if ok else "BAD"}] {tag:24} ожидалось ШИМ(duty) {exp}; увидено {shown}')
    print('Итог проверки:', 'всё совпало' if ok_all else 'ЕСТЬ РАСХОЖДЕНИЯ')
    return ok_all

# ------------------------------------------------------------------ CLI
def main():
    ap = argparse.ArgumentParser(description='Смена цветов LED в прошивке SPV-ЦАПа',
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument('file')
    ap.add_argument('-o', '--out')
    ap.add_argument('--list', action='store_true', help='показать режимы и текущие цвета')
    ap.add_argument('--set', action='append', default=[], metavar='РЕЖИМ=ЦВЕТ')
    ap.add_argument('--preset8', choices=['normal', 'factory'],
                    help='normal: при EQ-пресете 8 тоже показывать цвет normal (заводская прошивка гасит ШИМ); '
                         'factory: вернуть как было')
    ap.add_argument('--invert', choices=['on', 'off'],
                    help='on: инвертировать ШИМ (светодиод с общим анодом: duty 255 = погашен); off: как в заводской прошивке')
    ap.add_argument('--blink-off', choices=['dark', 'factory'],
                    help='dark: в фазе «погашено» мигания пады <- 0x210 (темно). factory: пады <- 0x10 (на железе = БЕЛАЯ вспышка)')
    ap.add_argument('--check', action='store_true', help='прогнать результат в эмуляторе и сверить ШИМ')
    a = ap.parse_args()

    fw = Firmware(open(a.file, 'rb').read())
    check_build(fw)
    if a.list or not (a.set or a.preset8 or a.invert or a.blink_off):
        print(f'{"режим":14} {"R":>3} {"G":>3} {"B":>3} {"ярк":>3}  источник   описание')
        for (name, _, _, desc) in SITES:
            (r, g, b, br), src = current_colors(fw)[name]
            if r is None: print(f'{name:14} {"—":>3} {"—":>3} {"—":>3} {"—":>3}  {src:9}  {desc}')
            else: print(f'{name:14} {r:3} {g:3} {b:3} {br:3}  {src:9}  {desc}')
        p8 = u32(fw.d, PRESET8_BEQC_FOFF) == NOP
        print(f'\nEQ-пресет 8: {"показывает цвет normal (патч)" if p8 else "ШИМ-цвет выключен (заводское поведение)"}')
        print('Группы:', ', '.join(f'{k}={"+".join(v)}' for k, v in GROUPS.items()))
        print('Полярность ШИМ:', 'ИНВЕРТИРОВАНА (общий анод)' if is_inverted(fw.d) else 'заводская')
        print('Фаза «погашено» мигания:', 'тёмная (пады 0x210, патч)' if blinkoff_dark(fw.d) else 'заводская (пады 0x10 = светится белым)')
        if not (a.set or a.preset8 or a.invert or a.blink_off):
            return
    if not a.out:
        raise SystemExit('нужен -o выходной файл')
    settings = {}
    for item in a.set:
        if '=' not in item: raise SystemExit(f'--set {item}: формат РЕЖИМ=ЦВЕТ')
        k, v = item.split('=', 1)
        col = parse_color(v)
        for n in expand(k.strip().lower()):
            settings[n] = col
    apply(fw, settings, a.preset8, None if a.invert is None else a.invert == 'on', a.blink_off)
    if not fw.verify():
        raise SystemExit('checksum не сошёлся — файл не записан')
    open(a.out, 'wb').write(fw.d)
    print(f'записано: {a.out}')
    view = {n: c for n, (c, _) in current_colors(fw).items()}
    for n in settings:
        print(f'  {n:14} -> R={view[n][0]} G={view[n][1]} B={view[n][2]} яркость={view[n][3]}')
    if a.check:
        ok = emu_check(a.out, view, u32(fw.d, PRESET8_BEQC_FOFF) == NOP, is_inverted(fw.d), blinkoff_dark(fw.d))
        sys.exit(0 if ok else 2)

if __name__ == '__main__':
    main()
