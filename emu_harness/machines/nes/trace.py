"""What a CPU trace did to the PPU, from the hardware registers alone (row Q).

Feed it executed instructions (any objects with pc, a, x, y, frame, code = the instruction bytes,
and addr = an emulator-resolved effective address or None; backends.mesen.tracelog.Step fits). It
follows the CPU side of the PPU interface (NESdev Wiki, "PPU registers", "PPU scrolling"):

  $2000 write   VRAM increment: 32 if bit 2 else 1
  $2002 read    clears the shared $2005/$2006 write toggle
  $2005 write   flips the toggle
  $2006 write   first: t high 6 bits; second: t low byte, v = t (an upload run starts there)
  $2007 write   VRAM[v] = value, v += increment;  $2007 read: v += increment
  $4014 write   OAM DMA from page value * $100 (recorded; the source bytes are not in a trace)

registers mirror every 8 bytes through $3FFF. So any game's VRAM uploads come back without knowing
its upload routines. `vram` is the 16 KiB PPU address space as written (no nametable mirroring,
palette mirrors not folded). Read-modify-write instructions on PPU registers are counted in
`unhandled`, not modelled.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ...cpu.m6502.disasm import ABS, ABX, ABY, IZX, IZY, OPS, ZP, ZPX, ZPY

STORES = {"sta": "a", "stx": "x", "sty": "y"}
READS = {"lda", "ldx", "ldy", "bit", "and", "ora", "eor", "adc", "sbc", "cmp", "cpx", "cpy"}


def effective_address(step) -> int | None:
    """The data address an instruction touches, from its bytes and registers; the emulator's own
    resolved address when the mode needs memory (indirect)."""
    if step.addr is not None:
        return step.addr
    if not step.code or step.code[0] not in OPS:
        return None
    mode = OPS[step.code[0]][1]
    lo = step.code[1] if len(step.code) > 1 else 0
    w = lo | (step.code[2] << 8 if len(step.code) > 2 else 0)
    return {ZP: lo, ZPX: (lo + step.x) & 0xFF, ZPY: (lo + step.y) & 0xFF, ABS: w,
            ABX: (w + step.x) & 0xFFFF, ABY: (w + step.y) & 0xFFFF}.get(mode)


@dataclass
class Run:
    """One stretch of consecutive $2007 writes: data[i] went to start + i * increment. A $2006
    address set, an increment change or a $2007 read starts a new one. A run can be empty (an
    address set with no writes: the palette-reset idiom, an attribute read-modify-write)."""
    frame: int | None
    pc: int                 # the instruction that set the address (or wrote first)
    start: int
    increment: int
    data: bytearray = field(default_factory=bytearray)


class PPUWrites:
    def __init__(self):
        self.vram = bytearray(0x4000)
        self.written = bytearray(0x4000)     # 1 where the trace wrote
        self.runs: list[Run] = []
        self.oam_dma: list[tuple[int | None, int]] = []    # (frame, source page)
        self.unhandled = 0
        self.t = self.v = 0
        self.toggle = False
        self.increment = 1
        self._run: Run | None = None

    def feed(self, step) -> None:
        op = step.code[0] if step.code else None
        if op not in OPS:
            return
        mn, mode = OPS[op]
        if mode not in (ABS, ABX, ABY, IZX, IZY):
            return                               # zero page never reaches $2000
        addr = effective_address(step)
        if addr is None:
            return
        if addr == 0x4014 and mn in STORES:
            self.oam_dma.append((step.frame, getattr(step, STORES[mn])))
            return
        if not 0x2000 <= addr < 0x4000:
            return
        reg = addr & 7
        if mn in STORES:
            self._write(reg, getattr(step, STORES[mn]), step)
        elif mn in READS:
            if reg == 2:
                self.toggle = False
            elif reg == 7:
                self.v = (self.v + self.increment) & 0x3FFF
                self._run = None                 # keeps start + i * increment = each byte's address
        else:
            self.unhandled += 1

    def _write(self, reg: int, val: int, step) -> None:
        if reg == 0:
            self.increment = 32 if val & 4 else 1
            self.t = (self.t & ~0x0C00) | ((val & 3) << 10)
        elif reg == 5:
            self.toggle = not self.toggle
        elif reg == 6:
            if not self.toggle:
                self.t = (self.t & 0x00FF) | ((val & 0x3F) << 8)
            else:
                self.t = (self.t & 0xFF00) | val
                self.v = self.t
                self._run = Run(step.frame, step.pc, self.v, self.increment)
                self.runs.append(self._run)
            self.toggle = not self.toggle
        elif reg == 7:
            if self._run is None or self._run.increment != self.increment:
                self._run = Run(step.frame, step.pc, self.v, self.increment)
                self.runs.append(self._run)
            a = self.v & 0x3FFF
            self.vram[a] = val
            self.written[a] = 1
            self._run.data.append(val)
            self.v = (self.v + self.increment) & 0x3FFF

    def feed_all(self, steps) -> "PPUWrites":
        for st in steps:
            self.feed(st)
        return self
