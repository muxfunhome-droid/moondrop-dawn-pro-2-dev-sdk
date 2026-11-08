#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
spv_fw_tool.py — разбор, проверка и патч прошивки формата "SPV APP Tag"
(SpacETouch / SPV-ЦАП, напр. SPV4040). Формат восстановлен из веб-прошивальщика
hub.moondroplab.tech (класс jW — парсер контейнера).

Контейнер:
  off 0x00  4B   reset-vector (j ...)           — не часть заголовка
  off 0x04  4B   tagMagic   == 0x40565053 "SPV@"
  off 0x08  4B   encryptAddress (= entry, напр. 0x48001000)
  off 0x0C  4B   userFlag   (напр. 0x4F414D41 "AMAO")
  --- если (userFlag & 0xFFFFFF00) == 0x4F414D00  (ветка "AMA"): ---
  off 0x10  4B   поле
  off 0x14  4B   buildDateAddress   (флеш-адрес строки даты)
  off 0x18  4B   buildTimeAddress   (флеш-адрес строки времени)
  off 0x1C  4B   numberOfBinFiles   (<=100)
  off 0x20  N*16 директория секций, запись = { name[4], address, size, checksum }
  (иначе — альтернативная раскладка: ver@32, date@36, time@40, count@44, dir@64)

Флеш-адрес -> смещение в файле:  offset = address & 0x00FFFFFF
checksum секции = СУММА БАЙТ данных секции (32-битная), причём собственное
                  4-байтовое поле checksum, если оно попадает внутрь диапазона
                  данных секции, считается нулём.  (Проверено на CPU и EXT.)

ВНИМАНИЕ: инструмент правит только байты КОДА/ДАННЫХ и пересчитывает checksum.
Он не трогает reset-vector, адреса и magic. Перед прошивкой всегда сверяйте
дамп оригинала с устройства.
"""
import sys, struct, argparse

MAGIC = 0x40565053           # "SPV@"
ADDR_MASK = 0x00FFFFFF
ALT_FLAG_MASK = 0xFFFFFF00
ALT_FLAG_VAL  = 0x4F414D00   # "\0MAO"

def u32(b, o): return struct.unpack_from('<I', b, o)[0]

class Section:
    __slots__ = ('name','address','size','checksum','foff','entry_off','chk_off')
    def __init__(self, name, address, size, checksum, entry_off):
        self.name = name
        self.address = address
        self.size = size
        self.checksum = checksum
        self.foff = address & ADDR_MASK
        self.entry_off = entry_off          # смещение записи директории
        self.chk_off = entry_off + 12        # смещение поля checksum в файле

class Firmware:
    def __init__(self, data: bytes):
        self.d = bytearray(data)
        if len(self.d) < 4096:
            raise ValueError("Файл слишком мал для SPV-контейнера")
        if u32(self.d, 4) != MAGIC:
            raise ValueError("Неверная magic 'SPV@' по смещению 4 — это не SPV APP Tag")
        self.entry        = u32(self.d, 8)
        self.user_flag    = u32(self.d, 12)
        if (self.user_flag & ALT_FLAG_MASK) != ALT_FLAG_VAL:
            # альтернативная раскладка
            self.ver_off, self.date_off_f, self.time_off_f = 32, 36, 40
            self.count_off, self.dir_off = 44, 64
        else:
            self.ver_off, self.date_off_f, self.time_off_f = 16, 20, 24
            self.count_off, self.dir_off = 28, 32
        self.nsec = u32(self.d, self.count_off)
        if not (0 < self.nsec <= 100):
            raise ValueError(f"Недопустимое число секций: {self.nsec}")
        self.sections = []
        for i in range(self.nsec):
            e = self.dir_off + i*16
            name = self.d[e:e+4].split(b'\x00')[0].decode('latin1')
            self.sections.append(Section(name, u32(self.d,e+4), u32(self.d,e+8),
                                         u32(self.d,e+12), e))
        self.build_date = self._cstr(u32(self.d, self.date_off_f) & ADDR_MASK)
        self.build_time = self._cstr(u32(self.d, self.time_off_f) & ADDR_MASK)

    def _cstr(self, off):
        if off >= len(self.d): return ''
        end = self.d.find(b'\x00', off)
        return self.d[off:end].decode('latin1', 'replace') if end>=0 else ''

    def calc_checksum(self, s: Section) -> int:
        """Сумма байт данных секции; собственное поле checksum = 0, если внутри."""
        total = sum(self.d[s.foff : s.foff + s.size]) & 0xFFFFFFFF
        if s.foff <= s.chk_off < s.foff + s.size:
            total = (total - sum(self.d[s.chk_off : s.chk_off+4])) & 0xFFFFFFFF
        return total

    def verify(self):
        ok = True
        for s in self.sections:
            if s.size == 0:
                continue
            calc = self.calc_checksum(s)
            good = (calc == s.checksum)
            ok = ok and good
            flag = 'OK ' if good else 'BAD'
            print(f"  [{flag}] {s.name:4s} addr=0x{s.address:08x} off=0x{s.foff:06x} "
                  f"size=0x{s.size:06x} chk=0x{s.checksum:08x} calc=0x{calc:08x}")
        return ok

    def info(self):
        print(f"magic=SPV@  entry=0x{self.entry:08x}  userFlag=0x{self.user_flag:08x}")
        print(f"build: {self.build_date} {self.build_time}  секций: {self.nsec}")
        self.verify()

    def patch(self, off: int, newbytes: bytes):
        """Пишет newbytes по смещению off и пересчитывает checksum всех секций,
        чьи данные охватывают эти байты."""
        if off < 0 or off + len(newbytes) > len(self.d):
            raise ValueError("Патч выходит за границы файла")
        # защита: не даём затирать заголовок/директорию случайно
        self.d[off:off+len(newbytes)] = newbytes
        touched = []
        for s in self.sections:
            if s.size and not (off+len(newbytes) <= s.foff or off >= s.foff+s.size):
                new = self.calc_checksum(s)
                if new != s.checksum:
                    struct.pack_into('<I', self.d, s.chk_off, new)
                    touched.append((s.name, s.checksum, new))
                    s.checksum = new
        return touched

    def recompute_all(self):
        touched = []
        for s in self.sections:
            if s.size == 0: continue
            new = self.calc_checksum(s)
            if new != s.checksum:
                struct.pack_into('<I', self.d, s.chk_off, new)
                touched.append((s.name, s.checksum, new)); s.checksum = new
        return touched

def parse_bytes(spec: str) -> bytes:
    spec = spec.strip().replace(',', ' ')
    if spec.lower().startswith('0x') and ' ' not in spec:
        spec = spec[2:]
        return bytes.fromhex(spec)
    if all(c in '0123456789abcdefABCDEF ' for c in spec):
        return bytes.fromhex(spec.replace(' ', ''))
    raise ValueError("bytes: укажите hex, напр. 2f0a03b4 или 0x2f0a03b4")

def main():
    ap = argparse.ArgumentParser(description="SPV APP Tag firmware tool")
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('info', help='показать заголовок и проверить checksum')
    p.add_argument('file')
    p = sub.add_parser('verify', help='только проверка checksum (код возврата 0/1)')
    p.add_argument('file')
    p = sub.add_parser('patch', help='записать байты по смещению и пересчитать checksum')
    p.add_argument('file'); p.add_argument('-o','--out', required=True)
    p.add_argument('--offset', required=True, help='смещение в файле, напр. 0x89d6')
    p.add_argument('--bytes', required=True, help='hex-байты, напр. 2f0a03b4')
    p = sub.add_parser('fixcrc', help='пересчитать все checksum (после ручной правки)')
    p.add_argument('file'); p.add_argument('-o','--out', required=True)
    args = ap.parse_args()

    fw = Firmware(open(args.file,'rb').read())
    if args.cmd == 'info':
        fw.info()
    elif args.cmd == 'verify':
        sys.exit(0 if fw.verify() else 1)
    elif args.cmd == 'patch':
        off = int(args.offset, 0); nb = parse_bytes(args.bytes)
        old = bytes(fw.d[off:off+len(nb)])
        t = fw.patch(off, nb)
        open(args.out,'wb').write(fw.d)
        print(f"патч @0x{off:x}: {old.hex()} -> {nb.hex()}")
        for name,o,n in t: print(f"  checksum {name}: 0x{o:08x} -> 0x{n:08x}")
        print(f"записано: {args.out}")
    elif args.cmd == 'fixcrc':
        t = fw.recompute_all(); open(args.out,'wb').write(fw.d)
        for name,o,n in t: print(f"  checksum {name}: 0x{o:08x} -> 0x{n:08x}")
        print(f"записано: {args.out}  (изменено секций: {len(t)})")

if __name__ == '__main__':
    main()
