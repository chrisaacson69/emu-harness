"""The CPU side of the PPU interface: what register reads and writes do to VRAM (NESdev Wiki, "PPU
registers", "PPU scrolling"). One model for both places a game's uploads are followed:
`trace.PPUWrites` (an emulator's CPU trace) and `bus.Bus(ppu=PPUPort())` (a headless run on
cpu.m6502, so a game's own draw routines paint into `vram`).

  $2000 write   VRAM increment: 32 if bit 2 else 1
  $2002 read    clears the shared $2005/$2006 write toggle
  $2005 write   flips the toggle
  $2006 write   first: t high 6 bits; second: t low byte, v = t
  $2007 write   VRAM[v] = value, v += increment
  $2007 read    the read buffer, refilled from VRAM[v]; v += increment

`reg` is the register number (address & 7: the registers mirror every 8 bytes through $3FFF).
`vram` is the 16 KiB PPU address space as written: no nametable mirroring, palette mirrors not
folded, and a palette read is buffered like any other (hardware returns it at once). Rendering,
scroll, OAM and the status flags are not modelled: the bus answers $2002's vblank bit.
"""
from __future__ import annotations


class PPUPort:
    def __init__(self):
        self.vram = bytearray(0x4000)
        self.written = bytearray(0x4000)     # 1 where a write landed
        self.t = self.v = 0
        self.toggle = False
        self.increment = 1
        self._buffer = 0

    def write(self, reg: int, val: int) -> None:
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
            self.toggle = not self.toggle
        elif reg == 7:
            a = self.v & 0x3FFF
            self.vram[a] = val
            self.written[a] = 1
            self.v = (self.v + self.increment) & 0x3FFF

    def read(self, reg: int) -> int:
        if reg == 2:
            self.toggle = False
        elif reg == 7:
            val, self._buffer = self._buffer, self.vram[self.v & 0x3FFF]
            self.v = (self.v + self.increment) & 0x3FFF
            return val
        return 0
