#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
spv_emu.py — эмулятор прошивки SPV-ЦАПа (RV32IMAC + Zicsr + AndeStar V5 custom,
частично F) для поиска логики светодиода.

Карта памяти:
  0x48000000+  flash (XIP) — содержимое .bin
  0x20000000..0x22000000 — периферия (MMIO): логируется, значения управляются хуками
  всё остальное — RAM, выделяется лениво страницами по 4 КБ

Использование как библиотеки: см. класс Emu; CLI: python3 spv_emu.py firmware.bin [опции]
"""
import sys, struct, math, argparse, collections

MASK = 0xffffffff
REG = ['zero','ra','sp','gp','tp','t0','t1','t2','s0','s1','a0','a1','a2','a3','a4','a5',
       'a6','a7','s2','s3','s4','s5','s6','s7','s8','s9','s10','s11','t3','t4','t5','t6']

def sx(v, bits):
    v &= (1 << bits) - 1
    return v - (1 << bits) if v & (1 << (bits - 1)) else v

def s32(v):
    v &= MASK
    return v - 0x100000000 if v & 0x80000000 else v

class Stop(Exception):
    pass

class Unimpl(Exception):
    pass

# ----------------------------------------------------------------------------- decode
def b(w, hi, lo):
    return (w >> lo) & ((1 << (hi - lo + 1)) - 1)

def decode32(w):
    """-> (mnemonic, rd, rs1, rs2, imm, extra)"""
    op = w & 0x7f; rd = b(w,11,7); f3 = b(w,14,12); rs1 = b(w,19,15); rs2 = b(w,24,20); f7 = b(w,31,25)
    I = sx(w >> 20, 12)
    S = sx((b(w,31,25) << 5) | b(w,11,7), 12)
    B = sx((b(w,31,31) << 12) | (b(w,7,7) << 11) | (b(w,30,25) << 5) | (b(w,11,8) << 1), 13)
    U = w & 0xfffff000
    J = sx((b(w,31,31) << 20) | (b(w,19,12) << 12) | (b(w,20,20) << 11) | (b(w,30,21) << 1), 21)
    if op == 0x37: return ('lui', rd, 0, 0, U, None)
    if op == 0x17: return ('auipc', rd, 0, 0, U, None)
    if op == 0x6f: return ('jal', rd, 0, 0, J, None)
    if op == 0x67: return ('jalr', rd, rs1, 0, I, None)
    if op == 0x63:
        m = {0:'beq',1:'bne',4:'blt',5:'bge',6:'bltu',7:'bgeu'}.get(f3)
        if m: return (m, 0, rs1, rs2, B, None)
    if op == 0x03:
        m = {0:'lb',1:'lh',2:'lw',4:'lbu',5:'lhu'}.get(f3)
        if m: return (m, rd, rs1, 0, I, None)
    if op == 0x23:
        m = {0:'sb',1:'sh',2:'sw'}.get(f3)
        if m: return (m, 0, rs1, rs2, S, None)
    if op == 0x13:
        if f3 == 1: return ('slli', rd, rs1, 0, rs2, None)
        if f3 == 5: return ('srai' if f7 & 0x20 else 'srli', rd, rs1, 0, rs2, None)
        return ({0:'addi',2:'slti',3:'sltiu',4:'xori',6:'ori',7:'andi'}[f3], rd, rs1, 0, I, None)
    if op == 0x33:
        if f7 == 1:
            return (['mul','mulh','mulhsu','mulhu','div','divu','rem','remu'][f3], rd, rs1, rs2, 0, None)
        if f7 == 0x20:
            m = {0:'sub',5:'sra'}.get(f3)
        else:
            m = ['add','sll','slt','sltu','xor','srl','or','and'][f3] if f7 == 0 else None
        if m: return (m, rd, rs1, rs2, 0, None)
    if op == 0x0f: return ('fence', 0, 0, 0, 0, None)
    if op == 0x73:
        if f3 == 0:
            if w == 0x00000073: return ('ecall', 0,0,0,0,None)
            if w == 0x00100073: return ('ebreak', 0,0,0,0,None)
            if w == 0x30200073: return ('mret', 0,0,0,0,None)
            if w == 0x10500073: return ('wfi', 0,0,0,0,None)
            return ('sys?', 0,0,0,w,None)
        m = {1:'csrrw',2:'csrrs',3:'csrrc',5:'csrrwi',6:'csrrsi',7:'csrrci'}.get(f3)
        if m: return (m, rd, rs1, 0, w >> 20, None)
    if op == 0x2f and f3 == 2:  # A
        f5 = w >> 27
        m = {2:'lr.w',3:'sc.w',1:'amoswap.w',0:'amoadd.w',4:'amoxor.w',0xc:'amoand.w',8:'amoor.w',
             0x10:'amomin.w',0x14:'amomax.w',0x18:'amominu.w',0x1c:'amomaxu.w'}.get(f5)
        if m: return (m, rd, rs1, rs2, 0, None)
    # --- F extension (single)
    if op == 0x07 and f3 == 2: return ('flw', rd, rs1, 0, I, None)
    if op == 0x27 and f3 == 2: return ('fsw', 0, rs1, rs2, S, None)
    if op in (0x43,0x47,0x4b,0x4f) and b(w,26,25) == 0:
        return ({0x43:'fmadd.s',0x47:'fmsub.s',0x4b:'fnmsub.s',0x4f:'fnmadd.s'}[op], rd, rs1, rs2, 0, b(w,31,27))
    if op == 0x53:
        f5 = w >> 27; fmt = b(w,26,25)
        if fmt == 0:
            if f5 == 0: return ('fadd.s', rd, rs1, rs2, 0, None)
            if f5 == 1: return ('fsub.s', rd, rs1, rs2, 0, None)
            if f5 == 2: return ('fmul.s', rd, rs1, rs2, 0, None)
            if f5 == 3: return ('fdiv.s', rd, rs1, rs2, 0, None)
            if f5 == 0xb: return ('fsqrt.s', rd, rs1, 0, 0, None)
            if f5 == 4: return (['fsgnj.s','fsgnjn.s','fsgnjx.s'][f3], rd, rs1, rs2, 0, None)
            if f5 == 5: return (['fmin.s','fmax.s'][f3], rd, rs1, rs2, 0, None)
            if f5 == 0x18: return ('fcvt.wu.s' if rs2 & 1 else 'fcvt.w.s', rd, rs1, 0, 0, None)
            if f5 == 0x1a: return ('fcvt.s.wu' if rs2 & 1 else 'fcvt.s.w', rd, rs1, 0, 0, None)
            if f5 == 0x1c: return ('fclass.s' if f3 == 1 else 'fmv.x.w', rd, rs1, 0, 0, None)
            if f5 == 0x1e: return ('fmv.w.x', rd, rs1, 0, 0, None)
            if f5 == 0x14: return ({0:'fle.s',1:'flt.s',2:'feq.s'}[f3], rd, rs1, rs2, 0, None)
    # --- AndeStar V5 custom
    if op == 0x5b:  # custom-2
        if f3 == 7:
            imm = sx((b(w,31,31) << 10) | (b(w,29,25) << 5) | (b(w,11,8) << 1), 11)
            return ('bbs' if b(w,30,30) else 'bbc', 0, rs1, 0, imm, b(w,24,20))
        if f3 in (5,6):
            imm = sx((b(w,31,31) << 10) | (b(w,29,25) << 5) | (b(w,11,8) << 1), 11)
            c = (b(w,30,30) << 6) | (b(w,7,7) << 5) | b(w,24,20)
            return ('beqc' if f3 == 5 else 'bnec', 0, rs1, 0, imm, c)
        if f3 in (2,3):
            return ('bfoz' if f3 == 2 else 'bfos', rd, rs1, 0, 0, (b(w,31,26), b(w,25,20)))
        if f3 == 0:
            m = {0x05:'lea.h',0x06:'lea.w',0x07:'lea.d',0x08:'lea.b.ze',0x09:'lea.h.ze',0x0a:'lea.w.ze',0x0b:'lea.d.ze'}.get(f7)
            if m: return (m, rd, rs1, rs2, 0, None)
            if f7 == 0x10 and rs2 == 0: return ('ffb', rd, rs1, rs2, 0, None)
            if f7 == 0x10: return ('ffb', rd, rs1, rs2, 0, None)
            if f7 == 0x11: return ('ffzmism', rd, rs1, rs2, 0, None)
            if f7 == 0x12: return ('ffmism', rd, rs1, rs2, 0, None)
            if f7 == 0x13: return ('flmism', rd, rs1, rs2, 0, None)
    if op == 0x0b:  # custom-0
        f2 = b(w,13,12)
        if f2 == 3:  # SBGP
            imm = sx((b(w,31,31) << 17) | (b(w,16,15) << 15) | (b(w,19,17) << 12) | (b(w,7,7) << 11) |
                     (b(w,30,25) << 5) | (b(w,11,8) << 1) | b(w,14,14), 18)
            return ('sbgp', 0, 3, rs2, imm, None)
        imm = sx((b(w,31,31) << 17) | (b(w,16,15) << 15) | (b(w,19,17) << 12) | (b(w,20,20) << 11) |
                 (b(w,30,21) << 1) | b(w,14,14), 18)
        return ({0:'lbgp',1:'addigp',2:'lbugp'}[f2], rd, 3, 0, imm, None)
    if op == 0x2b:  # custom-1
        if f3 in (0, 4):  # SHGP / SWGP
            if f3 == 4:
                imm = sx((b(w,31,31) << 18) | (b(w,8,8) << 17) | (b(w,16,15) << 15) | (b(w,19,17) << 12) |
                         (b(w,7,7) << 11) | (b(w,30,25) << 5) | (b(w,11,9) << 2), 19)
                return ('swgp', 0, 3, rs2, imm, None)
            imm = sx((b(w,31,31) << 17) | (b(w,16,15) << 15) | (b(w,19,17) << 12) | (b(w,7,7) << 11) |
                     (b(w,30,25) << 5) | (b(w,11,8) << 1), 18)
            return ('shgp', 0, 3, rs2, imm, None)
        if f3 in (1, 5):
            imm = sx((b(w,31,31) << 17) | (b(w,16,15) << 15) | (b(w,19,17) << 12) | (b(w,20,20) << 11) |
                     (b(w,30,21) << 1), 18)
            return ('lhgp' if f3 == 1 else 'lhugp', rd, 3, 0, imm, None)
        if f3 == 2:
            imm = sx((b(w,31,31) << 18) | (b(w,21,21) << 17) | (b(w,16,15) << 15) | (b(w,19,17) << 12) |
                     (b(w,20,20) << 11) | (b(w,30,22) << 2), 19)
            return ('lwgp', rd, 3, 0, imm, None)
    if op == 0x7f:  # Andes DSP (P-ext, OP-P = 0x7f у Andes)
        m = {(0x78,0):'minw', (0x79,0):'maxw', (0x72,0):'sclip32', (0x7a,0):'uclip32',
             (0x42,1):'smar64', (0x43,1):'smsr64', (0x52,1):'umar64', (0x53,1):'umsr64',
             (0x62,1):'maddr32', (0x63,1):'msubr32', (0x70,1):'mulsr64', (0x78,1):'mulr64', (0x60,1):'add64', (0x61,1):'sub64', (0x6f,0):'wexti', (0x67,0):'wext'}.get((f7,f3))
        if m: return (m, rd, rs1, rs2, rs2, None)
        return ('dsp?', rd, rs1, rs2, w, (f7, f3))
    return ('unk', 0, 0, 0, w, None)

def decode16(h):
    q = h & 3; f3 = h >> 13
    r = lambda x: 8 + x
    if q == 0:
        rdp = r(b(h,4,2)); rs1p = r(b(h,9,7))
        if f3 == 0:
            imm = (b(h,12,11) << 4) | (b(h,10,7) << 6) | (b(h,6,6) << 2) | (b(h,5,5) << 3)
            if imm == 0: return ('unk', 0,0,0,h,None)
            return ('addi', rdp, 2, 0, imm, None)
        uw = (b(h,12,10) << 3) | (b(h,6,6) << 2) | (b(h,5,5) << 6)
        if f3 == 2: return ('lw', rdp, rs1p, 0, uw, None)
        if f3 == 6: return ('sw', 0, rs1p, rdp, uw, None)
        if f3 == 3: return ('flw', rdp, rs1p, 0, uw, None)
        if f3 == 7: return ('fsw', 0, rs1p, rdp, uw, None)
        if f3 == 4: return ('execit', 0,0,0,h,None)
        return ('unk', 0,0,0,h,None)
    if q == 1:
        rd = b(h,11,7); imm6 = sx((b(h,12,12) << 5) | b(h,6,2), 6)
        if f3 == 0: return ('addi', rd, rd, 0, imm6, None)
        if f3 in (1, 5):
            j = sx((b(h,12,12) << 11) | (b(h,11,11) << 4) | (b(h,10,9) << 8) | (b(h,8,8) << 10) |
                   (b(h,7,7) << 6) | (b(h,6,6) << 7) | (b(h,5,3) << 1) | (b(h,2,2) << 5), 12)
            return ('jal', 1 if f3 == 1 else 0, 0, 0, j, None)
        if f3 == 2: return ('addi', rd, 0, 0, imm6, None)
        if f3 == 3:
            if rd == 2:
                imm = sx((b(h,12,12) << 9) | (b(h,6,6) << 4) | (b(h,5,5) << 6) | (b(h,4,3) << 7) | (b(h,2,2) << 5), 10)
                return ('addi', 2, 2, 0, imm, None)
            return ('lui', rd, 0, 0, (imm6 << 12) & MASK, None)
        if f3 == 4:
            rdp = r(b(h,9,7)); sub = b(h,11,10)
            sh = (b(h,12,12) << 5) | b(h,6,2)
            if sub == 0: return ('srli', rdp, rdp, 0, sh, None)
            if sub == 1: return ('srai', rdp, rdp, 0, sh, None)
            if sub == 2: return ('andi', rdp, rdp, 0, imm6, None)
            rs2p = r(b(h,4,2))
            if b(h,12,12) == 0:
                return (['sub','xor','or','and'][b(h,6,5)], rdp, rdp, rs2p, 0, None)
            return ('unk', 0,0,0,h,None)
        if f3 in (6, 7):
            off = sx((b(h,12,12) << 8) | (b(h,11,10) << 3) | (b(h,6,5) << 6) | (b(h,4,3) << 1) | (b(h,2,2) << 5), 9)
            return ('beq' if f3 == 6 else 'bne', 0, r(b(h,9,7)), 0, off, None)
    if q == 2:
        rd = b(h,11,7); rs2 = b(h,6,2)
        if f3 == 0: return ('slli', rd, rd, 0, (b(h,12,12) << 5) | rs2, None)
        if f3 == 2: return ('lw', rd, 2, 0, (b(h,12,12) << 5) | (b(h,6,4) << 2) | (b(h,3,2) << 6), None)
        if f3 == 3: return ('flw', rd, 2, 0, (b(h,12,12) << 5) | (b(h,6,4) << 2) | (b(h,3,2) << 6), None)
        if f3 == 4:
            if b(h,12,12) == 0:
                if rs2 == 0: return ('jalr', 0, rd, 0, 0, None)
                return ('add', rd, 0, rs2, 0, None)
            if rd == 0 and rs2 == 0: return ('ebreak', 0,0,0,0,None)
            if rs2 == 0: return ('jalr', 1, rd, 0, 0, None)
            return ('add', rd, rd, rs2, 0, None)
        uw = (b(h,12,9) << 2) | (b(h,8,7) << 6)
        if f3 == 6: return ('sw', 0, 2, rs2, uw, None)
        if f3 == 7: return ('fsw', 0, 2, rs2, uw, None)
    return ('unk', 0,0,0,h,None)

def fmt_insn(pc, d, size):
    m, rd, rs1, rs2, imm, ex = d
    R = REG
    if m in ('beq','bne','blt','bge','bltu','bgeu'): return f'{m} {R[rs1]},{R[rs2]},0x{(pc+imm)&MASK:x}'
    if m in ('bbc','bbs','beqc','bnec'): return f'{m} {R[rs1]},{ex},0x{(pc+imm)&MASK:x}'
    if m == 'jal': return f'jal {R[rd]},0x{(pc+imm)&MASK:x}'
    if m == 'jalr': return f'jalr {R[rd]},{imm}({R[rs1]})'
    if m in ('lui','auipc'): return f'{m} {R[rd]},0x{imm>>12:x}'
    if m in ('lb','lh','lw','lbu','lhu','flw'): return f'{m} {R[rd]},{imm}({R[rs1]})'
    if m in ('sb','sh','sw','fsw'): return f'{m} {R[rs2]},{imm}({R[rs1]})'
    if m.endswith('gp'): return f'{m} {R[rs2] if m[0]=="s" else R[rd]},gp{imm:+d}'
    if m in ('bfoz','bfos'): return f'{m} {R[rd]},{R[rs1]},{ex[0]},{ex[1]}'
    if m.startswith('csr'): return f'{m} {R[rd]},0x{imm:x},{rs1 if m.endswith("i") else R[rs1]}'
    if m in ('addi','slti','sltiu','xori','ori','andi','slli','srli','srai'): return f'{m} {R[rd]},{R[rs1]},{imm}'
    if m == 'unk': return f'.word 0x{imm:x}'
    return f'{m} {R[rd]},{R[rs1]},{R[rs2]}'

# ----------------------------------------------------------------------------- CPU
class Emu:
    FLASH = 0x48000000
    MMIO_LO, MMIO_HI = 0x20000000, 0x22000000

    def __init__(self, image: bytes):
        self.flash = bytearray(image) + bytearray(0x1000)
        self.flash_end = self.FLASH + len(self.flash)
        self.pages = {}
        self.x = [0] * 32
        self.f = [0] * 32          # raw 32-bit
        self.pc = self.FLASH
        self.csr = collections.defaultdict(int)
        self.csr[0xf14] = 0        # mhartid
        self.icache = {}
        self.steps = 0
        self.mmio_regs = {}        # addr -> value (последнее записанное / заданное)
        self.mmio_read_hooks = {}  # addr -> fn(emu, addr, size) -> value
        self.mmio_write_hooks = []  # fn(emu, addr, size, value)
        self.mmio_log = []         # (step, pc, 'R'/'W', addr, size, value)
        self.log_mmio = True
        self.poll = collections.Counter()
        self.poll_limit = 2000
        self.poll_policy = 'flip'  # 'flip' — на зацикленном чтении инвертировать возвращаемое значение
        self.poll_flips = {}
        self.ram_touch = set()
        self.calls = collections.Counter()
        self.trace = False
        self.lr = None
        self.mtime = 0
        self.cyc_skew = 0
        self.cyc_per_read = 20000   # ускорение времени: каждое чтение mcycle = +20k тактов
        self.hist = collections.deque(maxlen=64)
        self.hooks = {}
        self.pre_hooks = {}        # pc -> fn(emu) перед исполнением инструкции            # pc -> fn(emu); после вызова pc = ra
        self.rom_lo, self.rom_hi = 0x00080000, 0x00090000

    # --- memory
    def _page(self, a):
        p = a >> 12
        pg = self.pages.get(p)
        if pg is None:
            pg = self.pages[p] = bytearray(4096)
            self.ram_touch.add(a >> 20)
        return pg

    def is_mmio(self, a):
        return self.MMIO_LO <= a < self.MMIO_HI

    def mmio_read(self, a, size):
        key = (self.pc, a)
        self.poll[key] += 1
        if a in self.mmio_read_hooks:
            v = self.mmio_read_hooks[a](self, a, size)
        else:
            v = self.mmio_regs.get(a & ~3, 0)
            v = (v >> ((a & 3) * 8))
            if key in self.poll_flips:
                v ^= self.poll_flips[key]
            elif self.poll[key] > self.poll_limit and self.poll_policy == 'flip':
                self.poll_flips[key] = MASK
                print(f'[poll] {self.pc:08x} опрашивает {a:08x} >{self.poll_limit} раз — инвертирую ответ', file=sys.stderr)
                v ^= MASK
        v &= (1 << (8 * size)) - 1
        if self.log_mmio:
            self.mmio_log.append((self.steps, self.pc, 'R', a, size, v))
        return v

    def mmio_write(self, a, size, v):
        # опрос = повторные чтения без записей в периферию между ними; запись сбрасывает счётчики,
        # иначе read-modify-write регистров (ШИМ и т.п.) ложно считается зависанием.
        # Уже принятые решения (poll_flips) сохраняются.
        if self.poll:
            self.poll.clear()
        v &= (1 << (8 * size)) - 1
        base = a & ~3; sh = (a & 3) * 8
        old = self.mmio_regs.get(base, 0)
        m = ((1 << (8 * size)) - 1) << sh
        self.mmio_regs[base] = (old & ~m) | ((v << sh) & m)
        if self.log_mmio:
            self.mmio_log.append((self.steps, self.pc, 'W', a, size, v))
        for h in self.mmio_write_hooks:
            h(self, a, size, v)

    def load(self, a, size):
        a &= MASK
        if self.FLASH <= a < self.flash_end:
            o = a - self.FLASH
            return int.from_bytes(self.flash[o:o+size], 'little')
        if self.is_mmio(a):
            return self.mmio_read(a, size)
        o = a & 0xfff
        if o + size <= 4096:
            return int.from_bytes(self._page(a)[o:o+size], 'little')
        return sum(self.load(a + i, 1) << (8 * i) for i in range(size))

    def store(self, a, size, v):
        a &= MASK
        if self.is_mmio(a):
            return self.mmio_write(a, size, v)
        if self.FLASH <= a < self.flash_end:
            raise Stop(f'запись во flash {a:08x} pc={self.pc:08x}')
        o = a & 0xfff
        if o + size <= 4096:
            self._page(a)[o:o+size] = (v & ((1 << (8*size)) - 1)).to_bytes(size, 'little')
        else:
            for i in range(size):
                self.store(a + i, 1, v >> (8 * i))

    def fetch(self, pc):
        h = self.load(pc, 2)
        if h & 3 == 3:
            w = h | (self.load(pc + 2, 2) << 16)
            return decode32(w), 4
        return decode16(h), 2

    # --- float helpers
    @staticmethod
    def f2b(v):
        try:
            return struct.unpack('<I', struct.pack('<f', v))[0]
        except OverflowError:
            return 0x7f800000 if v > 0 else 0xff800000
    @staticmethod
    def b2f(v):
        return struct.unpack('<f', struct.pack('<I', v & MASK))[0]

    # --- CSR
    def csr_read(self, n):
        if n in (0xb00, 0xc00, 0xc01, 0xb02, 0xc02):  # mcycle / cycle / time / instret
            self.cyc_skew += self.cyc_per_read
            return (self.steps + self.cyc_skew) & MASK
        if n in (0xb80, 0xc80, 0xc81, 0xb82, 0xc82):
            return ((self.steps + self.cyc_skew) >> 32) & MASK
        if n == 0x001: return self.csr[0x003] & 0x1f
        if n == 0x002: return (self.csr[0x003] >> 5) & 7
        return self.csr[n]
    def csr_write(self, n, v):
        v &= MASK
        if n == 0x001: self.csr[0x003] = (self.csr[0x003] & ~0x1f) | (v & 0x1f); return
        if n == 0x002: self.csr[0x003] = (self.csr[0x003] & 0x1f) | ((v & 7) << 5); return
        self.csr[n] = v

    def trap(self, cause, tval=0):
        mtvec = self.csr[0x305]
        if mtvec == 0:
            raise Stop(f'trap cause={cause:#x} tval={tval:#x} pc={self.pc:08x} без mtvec')
        self.csr[0x341] = self.pc
        self.csr[0x342] = cause
        self.csr[0x343] = tval
        ms = self.csr[0x300]
        mie = (ms >> 3) & 1
        ms = (ms & ~0x88) | (mie << 7) | (3 << 11)
        self.csr[0x300] = ms
        if cause & 0x80000000 and mtvec & 1:
            self.pc = (mtvec & ~3) + 4 * (cause & 0x7fffffff)
        else:
            self.pc = mtvec & ~3

    # --- main loop
    def step(self):
        pc = self.pc
        ph = self.pre_hooks.get(pc)
        if ph is not None:
            ph(self)
        hk = self.hooks.get(pc)
        if hk is not None:
            hk(self)
            self.steps += 1
            self.pc = self.x[1]
            return
        if self.rom_lo <= pc < self.rom_hi:
            a = self.x
            if getattr(self, 'unknown_rom', None):
                self.unknown_rom(self, pc); self.steps += 1; self.pc = a[1]; return
            raise Stop(f'вызов неизвестной ROM-функции {pc:08x} a0={a[10]:08x} a1={a[11]:08x} a2={a[12]:08x} a3={a[13]:08x} ra={a[1]:08x}')
        ent = self.icache.get(pc)
        if ent is None:
            if not (self.FLASH <= pc < self.flash_end):
                d, sz = self.fetch(pc)   # код в RAM — не кешируем
            else:
                d, sz = self.fetch(pc)
                self.icache[pc] = (d, sz)
        else:
            d, sz = ent
        if self.trace:
            print(f'{pc:08x}: {fmt_insn(pc, d, sz)}')
        self.steps += 1
        self.hist.append(pc)
        m, rd, rs1, rs2, imm, ex = d
        x = self.x
        npc = pc + sz
        a = x[rs1]; bb = x[rs2]
        r = None
        if m == 'addi': r = a + imm
        elif m == 'lw': r = self.load(a + imm, 4)
        elif m == 'sw': self.store(a + imm, 4, bb)
        elif m == 'add': r = a + bb
        elif m == 'lui': r = imm
        elif m == 'auipc': r = pc + imm
        elif m == 'jal':
            r = npc; npc = pc + imm
            if rd == 1: self.calls[npc & MASK] += 1
        elif m == 'jalr':
            r = npc; npc = (a + imm) & ~1
            if rd == 1: self.calls[npc & MASK] += 1
        elif m == 'beq': npc = pc + imm if a == bb else npc
        elif m == 'bne': npc = pc + imm if a != bb else npc
        elif m == 'blt': npc = pc + imm if s32(a) < s32(bb) else npc
        elif m == 'bge': npc = pc + imm if s32(a) >= s32(bb) else npc
        elif m == 'bltu': npc = pc + imm if a < bb else npc
        elif m == 'bgeu': npc = pc + imm if a >= bb else npc
        elif m == 'lb': r = sx(self.load(a + imm, 1), 8)
        elif m == 'lh': r = sx(self.load(a + imm, 2), 16)
        elif m == 'lbu': r = self.load(a + imm, 1)
        elif m == 'lhu': r = self.load(a + imm, 2)
        elif m == 'sb': self.store(a + imm, 1, bb)
        elif m == 'sh': self.store(a + imm, 2, bb)
        elif m == 'slli': r = a << imm
        elif m == 'srli': r = a >> imm
        elif m == 'srai': r = s32(a) >> imm
        elif m == 'andi': r = a & imm
        elif m == 'ori': r = a | (imm & MASK)
        elif m == 'xori': r = a ^ (imm & MASK)
        elif m == 'slti': r = int(s32(a) < imm)
        elif m == 'sltiu': r = int(a < (imm & MASK))
        elif m == 'sub': r = a - bb
        elif m == 'and': r = a & bb
        elif m == 'or': r = a | bb
        elif m == 'xor': r = a ^ bb
        elif m == 'sll': r = a << (bb & 31)
        elif m == 'srl': r = a >> (bb & 31)
        elif m == 'sra': r = s32(a) >> (bb & 31)
        elif m == 'slt': r = int(s32(a) < s32(bb))
        elif m == 'sltu': r = int(a < bb)
        elif m == 'mul': r = a * bb
        elif m == 'mulh': r = (s32(a) * s32(bb)) >> 32
        elif m == 'mulhu': r = (a * bb) >> 32
        elif m == 'mulhsu': r = (s32(a) * bb) >> 32
        elif m == 'div':
            sa, sb_ = s32(a), s32(bb)
            if sb_ == 0: r = MASK
            elif sa == -0x80000000 and sb_ == -1: r = sa
            else: r = int(abs(sa) // abs(sb_)) * (1 if (sa < 0) == (sb_ < 0) else -1)
        elif m == 'divu': r = MASK if bb == 0 else a // bb
        elif m == 'rem':
            sa, sb_ = s32(a), s32(bb)
            if sb_ == 0: r = sa
            elif sa == -0x80000000 and sb_ == -1: r = 0
            else: r = int(math.fmod(sa, sb_))
        elif m == 'remu': r = a if bb == 0 else a % bb
        # --- Andes
        elif m == 'bbc': npc = pc + imm if not (a >> ex) & 1 else npc
        elif m == 'bbs': npc = pc + imm if (a >> ex) & 1 else npc
        elif m == 'beqc': npc = pc + imm if a == ex else npc
        elif m == 'bnec': npc = pc + imm if a != ex else npc
        elif m in ('bfoz', 'bfos'):
            msb, lsb = ex
            if msb == 0:
                v = a & 1; r = v << lsb
                if m == 'bfos' and v: r = (MASK << lsb)
            elif msb < lsb:
                ln = lsb - msb + 1
                v = a & ((1 << ln) - 1)
                if m == 'bfos': v = sx(v, ln)
                r = (v << msb) & (MASK if m == 'bfos' else ((1 << (lsb + 1)) - 1))
            else:
                ln = msb - lsb + 1
                v = (a >> lsb) & ((1 << ln) - 1)
                r = sx(v, ln) if m == 'bfos' else v
        elif m in ('lea.h', 'lea.h.ze'): r = a + (bb << 1)
        elif m in ('lea.w', 'lea.w.ze'): r = a + (bb << 2)
        elif m in ('lea.d', 'lea.d.ze'): r = a + (bb << 3)
        elif m == 'lea.b.ze': r = a + bb
        elif m == 'addigp': r = a + imm
        elif m == 'lbgp': r = sx(self.load(a + imm, 1), 8)
        elif m == 'lbugp': r = self.load(a + imm, 1)
        elif m == 'lhgp': r = sx(self.load(a + imm, 2), 16)
        elif m == 'lhugp': r = self.load(a + imm, 2)
        elif m == 'lwgp': r = self.load(a + imm, 4)
        elif m == 'sbgp': self.store(a + imm, 1, bb)
        elif m == 'shgp': self.store(a + imm, 2, bb)
        elif m == 'swgp': self.store(a + imm, 4, bb)
        elif m == 'ffb':
            r = 0
            for i in range(4):
                if ((a >> (8*i)) & 0xff) == (bb & 0xff): r = i - 4; break
        elif m in ('ffzmism', 'ffmism', 'flmism'):
            r = 0
            rng = range(4) if m != 'flmism' else range(3, -1, -1)
            for i in rng:
                ba, bv = (a >> (8*i)) & 0xff, (bb >> (8*i)) & 0xff
                if (m == 'ffzmism' and (ba == 0 or ba != bv)) or (m != 'ffzmism' and ba != bv):
                    r = i - 4; break
        # --- Andes DSP
        elif m == 'minw': r = min(s32(a), s32(bb))
        elif m == 'maxw': r = max(s32(a), s32(bb))
        elif m == 'sclip32':
            lim = (1 << imm) - 1; r = max(-lim - 1, min(lim, s32(a)))
        elif m == 'uclip32':
            lim = (1 << imm) - 1; r = max(0, min(lim, s32(a)))
        elif m == 'maddr32': r = x[rd] + a * bb
        elif m == 'msubr32': r = x[rd] - a * bb
        elif m in ('smar64', 'smsr64', 'umar64', 'umsr64', 'mulsr64', 'mulr64'):
            sgn = m[0] in 'sm' and m != 'mulr64'
            p = (s32(a) * s32(bb)) if sgn else (a * bb)
            acc = x[rd] | (x[rd + 1] << 32) if rd else 0
            if acc >> 63: acc -= 1 << 64
            v = p if m.startswith('mul') else (acc + p if m.endswith('ar64') else acc - p)
            if rd:
                x[rd] = v & MASK; x[rd + 1] = (v >> 32) & MASK
        elif m in ('add64', 'sub64'):
            va = a | (x[rs1 + 1] << 32) if rs1 else 0
            vb = bb | (x[rs2 + 1] << 32) if rs2 else 0
            v = va + vb if m == 'add64' else va - vb
            if rd:
                x[rd] = v & MASK; x[rd + 1] = (v >> 32) & MASK
        elif m in ('wexti', 'wext'):
            v = a | ((x[rs1 + 1] if rs1 else 0) << 32)
            r = (v >> (imm if m == 'wexti' else (bb & 31))) & MASK
        elif m == 'dsp?':
            raise Unimpl(f'{pc:08x}: DSP f7={ex[0]:#x} f3={ex[1]} word={imm:08x}')
        # --- system
        elif m.startswith('csr'):
            old = self.csr_read(imm)
            src = rs1 if m.endswith('i') else a
            if m in ('csrrw', 'csrrwi'): self.csr_write(imm, src)
            elif m in ('csrrs', 'csrrsi'):
                if rs1: self.csr_write(imm, old | src)
            else:
                if rs1: self.csr_write(imm, old & ~src)
            r = old
        elif m == 'fence': pass
        elif m == 'mret':
            ms = self.csr[0x300]
            mpie = (ms >> 7) & 1
            self.csr[0x300] = (ms & ~0x8) | (mpie << 3) | 0x80
            npc = self.csr[0x341]
        elif m == 'wfi':
            self.on_wfi()
        elif m == 'ecall':
            self.pc = pc; self.trap(11); return
        elif m == 'ebreak':
            raise Stop(f'ebreak @ {pc:08x}')
        # --- atomics
        elif m == 'lr.w': r = self.load(a, 4); self.lr = a
        elif m == 'sc.w':
            if self.lr == a: self.store(a, 4, bb); r = 0
            else: r = 1
            self.lr = None
        elif m.startswith('amo'):
            r = self.load(a, 4)
            op = m[3:-2]
            nv = {'swap': bb, 'add': r + bb, 'xor': r ^ bb, 'and': r & bb, 'or': r | bb,
                  'min': min(s32(r), s32(bb)), 'max': max(s32(r), s32(bb)),
                  'minu': min(r, bb), 'maxu': max(r, bb)}[op]
            self.store(a, 4, nv)
        # --- F
        elif m == 'flw': self.f[rd] = self.load(a + imm, 4)
        elif m == 'fsw': self.store(a + imm, 4, self.f[rs2])
        elif m[0] == 'f':
            self.exec_f(d)
        else:
            raise Unimpl(f'{pc:08x}: {fmt_insn(pc, d, sz)}')
        if r is not None and rd:
            x[rd] = r & MASK
        self.pc = npc & MASK

    def exec_f(self, d):
        m, rd, rs1, rs2, imm, ex = d
        F = self.f; b2f = self.b2f; f2b = self.f2b
        fa, fb = b2f(F[rs1]), b2f(F[rs2])
        def setf(v): F[rd] = f2b(v)
        def setx(v):
            if rd: self.x[rd] = v & MASK
        if m == 'fadd.s': setf(fa + fb)
        elif m == 'fsub.s': setf(fa - fb)
        elif m == 'fmul.s': setf(fa * fb)
        elif m == 'fdiv.s': setf(fa / fb if fb else math.copysign(math.inf, fa) if fa else math.nan)
        elif m == 'fsqrt.s': setf(math.sqrt(fa) if fa >= 0 else math.nan)
        elif m in ('fmadd.s','fmsub.s','fnmsub.s','fnmadd.s'):
            fc = b2f(F[ex])
            v = {'fmadd.s': fa*fb+fc, 'fmsub.s': fa*fb-fc, 'fnmsub.s': -fa*fb+fc, 'fnmadd.s': -fa*fb-fc}[m]
            setf(v)
        elif m == 'fsgnj.s': F[rd] = (F[rs1] & 0x7fffffff) | (F[rs2] & 0x80000000)
        elif m == 'fsgnjn.s': F[rd] = (F[rs1] & 0x7fffffff) | (~F[rs2] & 0x80000000)
        elif m == 'fsgnjx.s': F[rd] = F[rs1] ^ (F[rs2] & 0x80000000)
        elif m == 'fmin.s': setf(min(fa, fb))
        elif m == 'fmax.s': setf(max(fa, fb))
        elif m == 'fcvt.w.s':
            setx(0x7fffffff if math.isnan(fa) else max(-0x80000000, min(0x7fffffff, int(round(fa)))))
        elif m == 'fcvt.wu.s':
            setx(MASK if math.isnan(fa) else max(0, min(MASK, int(round(fa)))))
        elif m == 'fcvt.s.w': setf(float(s32(self.x[rs1])))
        elif m == 'fcvt.s.wu': setf(float(self.x[rs1]))
        elif m == 'fmv.x.w': setx(F[rs1])
        elif m == 'fmv.w.x': F[rd] = self.x[rs1]
        elif m == 'feq.s': setx(int(fa == fb))
        elif m == 'flt.s': setx(int(fa < fb))
        elif m == 'fle.s': setx(int(fa <= fb))
        elif m == 'fclass.s': setx(1 << 6)
        else:
            raise Unimpl(m)

    def on_wfi(self):
        pass

    def run(self, n, until=None):
        end = self.steps + n
        while self.steps < end:
            if until is not None and self.pc in until:
                return 'until'
            self.step()
        return 'steps'

    def regs_str(self):
        return ' '.join(f'{REG[i]}={self.x[i]:08x}' for i in range(1, 32))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('bin')
    ap.add_argument('-n', type=int, default=2_000_000)
    ap.add_argument('--trace', action='store_true')
    a = ap.parse_args()
    emu = Emu(open(a.bin, 'rb').read())
    emu.trace = a.trace
    try:
        emu.run(a.n)
    except (Stop, Unimpl) as e:
        print('STOP:', e)
    print(f'steps={emu.steps} pc={emu.pc:08x}')
    print(emu.regs_str())

if __name__ == '__main__':
    main()


def dis(e, start, end, gp=None):
    """Дизассемблировать память эмулятора [start, end)."""
    pc = start; out = []
    while pc < end:
        d, sz = e.fetch(pc)
        t = fmt_insn(pc, d, sz)
        if d[0].endswith('gp') and d[0] != 'addigp' or d[0] == 'addigp':
            t += f'   ; [{(e.x[3] + d[4]) & MASK:08x}]'
        out.append(f'{pc:08x}: {t}')
        pc += sz
    return '\n'.join(out)
