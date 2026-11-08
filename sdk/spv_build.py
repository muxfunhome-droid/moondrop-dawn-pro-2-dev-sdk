#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
spv_build.py — сборка своей программы на C и вставка в прошивку SPV4040.

  python3 sdk/spv_build.py sdk/examples/rainbow --base fw_custom2.bin -o fw_rainbow.bin --emu

Шаги:
  1. riscv64-unknown-elf-gcc (rv32imafc/ilp32f, -mno-relax) компилирует *.c программы + sdk/rt/spv_rt.c;
  2. линковка по sdk/rt/link.ld: код с 0x48020c00 во flash, данные в RAM 0x130000;
  3. образ программы вставляется за секцией CPU базовой прошивки, размер CPU расширяется;
  4. два крючка в main() прошивки (0x48002d88 init, 0x48002d8c главный цикл) -> jal в рантайм;
  5. пересчёт checksum, проверка; --emu: загрузка в эмуляторе, печать журнала spv_log и записей в LED.
Базой может быть заводской образ, образ с патчами spv_led_colors.py или уже собранный этим скриптом
(старая программа тогда заменяется).
"""
import sys, os, struct, argparse, subprocess, glob

SDK = os.path.dirname(os.path.abspath(__file__))
FIRM = os.path.join(os.path.dirname(SDK), 'tools')   # Python-инструменты лежат в tools/
sys.path.insert(0, FIRM)
from spv_fw_tool import Firmware                     # noqa: E402
import spv_led_colors as L                           # noqa: E402
from spv_emu import decode32                         # noqa: E402

CC = 'riscv64-unknown-elf-gcc'
OBJCOPY = 'riscv64-unknown-elf-objcopy'
NM = 'riscv64-unknown-elf-nm'
SIZE = 'riscv64-unknown-elf-size'
ARCH = ['-march=rv32imafc_zicsr', '-mabi=ilp32f', '-mcmodel=medlow', '-mno-relax']
CFLAGS = ARCH + ['-Os', '-g', '-std=gnu11', '-ffreestanding', '-nostdlib', '-ffunction-sections',
                 '-fdata-sections', '-fno-common', '-fno-tree-loop-distribute-patterns',
                 '-Wall', '-Wextra', '-I', os.path.join(SDK, 'include')]

APP_ADDR = 0x48020c00
APP_FOFF = APP_ADDR - L.FLASH
APP_LIMIT = 0x37000                                  # секция EXT
MAGIC = 0x41565053
HOOKS = {  # адрес -> (исходное слово, символ рантайма)
    0x48002d88: (0x0f4000e7, '__spv_init_hook'),     # jalr ra,0xf4(zero)
    0x48002d8c: (0x360000e7, '__spv_loop_hook'),     # jalr ra,0x360(zero)
}


def run(cmd, echo=False):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if echo and r.stdout.strip(): print(r.stdout.rstrip())
    if r.returncode:
        sys.stderr.write(r.stderr)
        raise SystemExit(f'ошибка: {" ".join(cmd[:2])} …')
    if r.stderr.strip(): sys.stderr.write(r.stderr)
    return r.stdout


def build(app_dir, out_dir):
    srcs = sorted(glob.glob(os.path.join(app_dir, '*.c')))
    if not srcs: raise SystemExit(f'в {app_dir} нет *.c')
    os.makedirs(out_dir, exist_ok=True)
    objs = []
    for s in srcs + [os.path.join(SDK, 'rt', 'spv_rt.c')]:
        o = os.path.join(out_dir, os.path.basename(s)[:-2] + '.o')
        run([CC] + CFLAGS + ['-c', s, '-o', o]); objs.append(o)
    elf = os.path.join(out_dir, 'app.elf'); binf = os.path.join(out_dir, 'app.bin')
    run([CC] + ARCH + ['-nostdlib', '-T', os.path.join(SDK, 'rt', 'link.ld'), '-Wl,--gc-sections',
                       '-Wl,--no-relax', '-Wl,-Map=' + os.path.join(out_dir, 'app.map'), '-o', elf] + objs + ['-lgcc'])
    run([OBJCOPY, '-O', 'binary', '-j', '.text', '-j', '.data', elf, binf])
    syms = {}
    for line in run([NM, elf]).splitlines():
        p = line.split()
        if len(p) == 3: syms[p[2]] = int(p[0], 16)
    print(run([SIZE, elf]).splitlines()[-1])
    return elf, open(binf, 'rb').read(), syms


def strip_old_app(fw):
    """Если в базе уже есть программа — вернуть базу к состоянию до неё."""
    d = fw.d; cpu = fw.sections[0]
    if cpu.size <= APP_FOFF or L.u32(d, APP_FOFF) != MAGIC:
        return False
    base = L.u32(d, APP_FOFF + 4)
    for a, (orig, _) in HOOKS.items(): L.put32(d, a - L.FLASH, orig)
    d[APP_FOFF:cpu.size] = bytes(cpu.size - APP_FOFF)
    if base > APP_FOFF: d[base:APP_FOFF] = bytes(APP_FOFF - base)
    cpu.size = base
    L.put32(d, cpu.entry_off + 8, cpu.size)
    return True


def install(base_path, app, syms, out_path):
    fw = Firmware(open(base_path, 'rb').read())
    cpu = L.check_build(fw); d = fw.d
    if strip_old_app(fw): print(f'в базе была программа — удалена (CPU = 0x{cpu.size:x})')
    if cpu.size > APP_FOFF: raise SystemExit(f'секция CPU базы (0x{cpu.size:x}) залезает на 0x{APP_FOFF:x}')
    for a, (orig, _) in HOOKS.items():
        if L.u32(d, a - L.FLASH) != orig: raise SystemExit(f'{a:08x}: не заводская инструкция, крючок не ставлю')
    if any(d[max(cpu.size, 0x20ae0):APP_FOFF]): raise SystemExit('между CPU и программой не нули')
    end = APP_FOFF + len(app)
    if end > APP_LIMIT: raise SystemExit(f'программа слишком большая: {len(app)} Б, максимум {APP_LIMIT - APP_FOFF}')
    if L.u32(app, 0) != MAGIC: raise SystemExit('нет заголовка SPVA в начале образа программы')
    if cpu.size == 0x20ad4:                       # см. CLAUDE.md: на устройстве там 0xFF
        d[0x20ad4:0x20ae0] = b'\xff' * 12
    base_size = cpu.size
    d[APP_FOFF:end] = app
    L.put32(d, APP_FOFF + 4, base_size)
    cpu.size = (end + 3) & ~3
    L.put32(d, cpu.entry_off + 8, cpu.size)
    for a, (_, sym) in HOOKS.items():
        L.put32(d, a - L.FLASH, L.enc_jal(1, syms[sym] - a))
        m = decode32(L.u32(d, a - L.FLASH)); assert m[0] == 'jal' and (a + m[4]) & 0xffffffff == syms[sym]
    fw.recompute_all()
    if not fw.verify(): raise SystemExit('checksum не сошёлся')
    open(out_path, 'wb').write(d)
    print(f'записано: {out_path}  (программа {len(app)} Б @0x{APP_ADDR:08x}, CPU 0x{base_size:x} -> 0x{cpu.size:x})')


def emulate(path, syms, steps):
    import copy
    from led_run import make
    e = make(path); e.log_mmio = False
    cnt = {'init': 0, 'loop': 0}
    e.pre_hooks[syms['__spv_init_hook']] = lambda ee: cnt.__setitem__('init', cnt['init'] + 1)
    e.pre_hooks[syms['__spv_loop_hook']] = lambda ee: cnt.__setitem__('loop', cnt['loop'] + 1)
    leds, cur = [], {}
    def hook(ee, a, size, v):
        if a in (0x2010000c, 0x20100004, 0x20100014) and v & 1:
            cur[a] = v >> 16
            if a == 0x20100004:
                t = tuple(255 - cur.get(x, 255) for x in (0x2010000c, 0x20100004, 0x20100014))
                if not leds or leds[-1] != t: leds.append(t)
    e.mmio_write_hooks.append(hook)
    err = None
    try:
        e.run(2500000 + steps)
    except Exception as ex:                       # noqa: BLE001
        err = ex
    print(f'\nЭмулятор: init-крючок {cnt["init"]}×, проходов главного цикла {cnt["loop"]}')
    if err:
        print(f'ОСТАНОВ: {type(err).__name__}: {err}  pc={e.pc:08x}')
        print('  последние PC:', ' '.join(f'{p:08x}' for p in list(e.hist)[-12:]))
    pos = e.load(syms['spv_dbg_pos'], 4); size = 2048; buf = syms['spv_dbg_buf']
    start = max(0, pos - size)
    text = bytes(e.load(buf + (i % size), 1) for i in range(start, pos)).decode('utf-8', 'replace')
    print('--- spv_log ---' + ('' if start == 0 else ' (начало затёрто)'))
    print(text.rstrip() or '(пусто)')
    print('--- LED (настоящий цвет R,G,B с учётом общего анода), первые изменения ---')
    print(' '.join(f'({r},{g},{b})' for r, g, b in leds[:24]) or '(записей в ШИМ не было)',
          f'… всего {len(leds)}' if len(leds) > 24 else '')
    return err is None and cnt['init'] == 1 and cnt['loop'] > 0


def main():
    ap = argparse.ArgumentParser(description='Сборка программы на C для SPV4040',
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument('app', help='папка с *.c программы')
    ap.add_argument('--base', required=True, help='базовая прошивка (заводской или уже патченый образ)')
    ap.add_argument('-o', '--out', required=True)
    ap.add_argument('--emu', nargs='?', const=1500000, type=int, metavar='ШАГОВ',
                    help='прогнать в эмуляторе (по умолчанию 1.5 млн инструкций после загрузки)')
    a = ap.parse_args()
    elf, app, syms = build(a.app, os.path.join(a.app, 'build'))
    install(a.base, app, syms, a.out)
    if a.emu:
        ok = emulate(a.out, syms, a.emu)
        print('Итог:', 'OK' if ok else 'ЕСТЬ ПРОБЛЕМЫ')
        sys.exit(0 if ok else 2)


if __name__ == '__main__':
    main()
