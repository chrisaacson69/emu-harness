"""A whole cartridge's CPU address space, for running it headless on cpu.m6502 (row P2).

Unlike vm/koei/nes's `Memory` (one handler at a time, banks set by hand), the cartridge drives this
bus itself: its mapper-register writes switch the banks, as on hardware. For tracing real control
flow (UW's boot tracer) without an emulator.

  2 KiB internal RAM, mirrored to $1FFF
  $2000-$3FFF  PPU stub: $2002 toggles bit 7 on each read, so vblank waits finish; else 0.
               With `ppu=ppuport.PPUPort()`, register reads and writes also reach it, so a game's
               own draw routines paint into `ppu.vram` (the $2002 answer stays the stub's).
  $4016        controller 1, serial: `buttons` bit 0..7 = A B Select Start Up Down Left Right
  $4020-$FFFF  the mapper (NROM, MMC5)

MMC5 (NESdev Wiki, "MMC5"), PRG mode 3 only, the mode KOEI's boards use: four 8 KiB windows at
$8000/$A000/$C000/$E000 from $5114-$5117. Bit 7 of $5114-$5116 picks ROM (1) or PRG-RAM (0); $5117
is always ROM. $5113 banks PRG-RAM at $6000 (one 8 KiB chip here, so every RAM bank is that chip).
$5205/$5206 is the 8 x 8 multiplier. Power-on windows: $E000 = last bank (NESdev: $5117 = $FF);
$C000 = the bank before it and $8000/$A000 = bank 0, as UW's trace_boot assumed (unspecified on
hardware; KOEI's reset sets them before use). Other MMC5 features (PRG modes 0-2, ExRAM, the
scanline IRQ, CHR) are not modelled; a mode change raises.
"""
from __future__ import annotations

from .memory import Cart
from .ppuport import PPUPort


class NROM:
    def __init__(self, prg: bytes):
        self.prg = prg

    def read(self, addr: int) -> int:
        return self.prg[(addr - 0x8000) % len(self.prg)] if addr >= 0x8000 else 0

    def write(self, addr: int, val: int) -> None:
        pass

    def prg_bank(self, addr: int) -> int:
        return 0


class MMC5:
    REGS = {0x5114: 0x8000, 0x5115: 0xA000, 0x5116: 0xC000, 0x5117: 0xE000}

    def __init__(self, prg: bytes):
        self.prg = prg
        self.banks = len(prg) // 0x2000
        self.sram = bytearray(0x2000)
        last = self.banks - 1
        # window -> (is_rom, bank)
        self.window = {0x8000: (True, 0), 0xA000: (True, 0), 0xC000: (True, last - 1), 0xE000: (True, last)}
        self.mult = [0, 0]

    def prg_bank(self, addr: int) -> int:
        """The bank actually mapped at a $8000+ address: a ROM bank number wraps by the PRG size
        (a 512 KiB cart ignores bit 6), as the read does."""
        rom, bank = self.window[addr & 0xE000]
        return bank % self.banks if rom else bank

    def read(self, addr: int) -> int:
        if addr == 0x5205:
            return (self.mult[0] * self.mult[1]) & 0xFF
        if addr == 0x5206:
            return (self.mult[0] * self.mult[1]) >> 8
        if 0x6000 <= addr < 0x8000:
            return self.sram[addr - 0x6000]
        if addr >= 0x8000:
            rom, bank = self.window[addr & 0xE000]
            return self.prg[(bank % self.banks) * 0x2000 + (addr & 0x1FFF)] if rom else self.sram[addr & 0x1FFF]
        return 0

    def write(self, addr: int, val: int) -> None:
        if addr in self.REGS:
            rom = addr == 0x5117 or bool(val & 0x80)
            self.window[self.REGS[addr]] = (rom, (val & 0x7F) if rom else (val & 0x0F))
        elif addr == 0x5100 and val & 3 != 3:
            raise NotImplementedError(f"MMC5 PRG mode {val & 3}: only mode 3 (four 8K windows)")
        elif addr == 0x5205:
            self.mult[0] = val
        elif addr == 0x5206:
            self.mult[1] = val
        elif 0x6000 <= addr < 0x8000:
            self.sram[addr - 0x6000] = val
        elif addr >= 0x8000:
            rom, _ = self.window[addr & 0xE000]
            if not rom:
                self.sram[addr & 0x1FFF] = val


MAPPERS = {0: NROM, 5: MMC5}


class Bus:
    def __init__(self, rom: bytes, ppu: PPUPort | None = None):
        if rom[:4] != b"NES\x1a":
            raise ValueError("not an iNES image")
        mapper = (rom[6] >> 4) | (rom[7] & 0xF0)
        if mapper not in MAPPERS:
            raise NotImplementedError(f"mapper {mapper}: Bus has {sorted(MAPPERS)} so far")
        self.cart = Cart(mapper, rom[4] * 0x4000, bool(rom[6] & 0x02))
        self.mapper = MAPPERS[mapper](rom[16:16 + self.cart.prg_size])
        self.ram = bytearray(0x800)
        self.ppu = ppu
        self.buttons = 0
        self._vblank = 0
        self._strobe = False
        self._shift = 0

    def prg_bank(self, addr: int) -> int:
        return self.mapper.prg_bank(addr)

    def read(self, addr: int) -> int:
        addr &= 0xFFFF
        if addr < 0x2000:
            return self.ram[addr & 0x7FF]
        if addr < 0x4000:
            val = self.ppu.read(addr & 7) if self.ppu else 0
            if addr & 7 == 2:
                self._vblank ^= 0x80
                return self._vblank
            return val
        if addr == 0x4016:
            b = (self.buttons >> self._shift) & 1 if self._shift < 8 else 1
            if not self._strobe and self._shift < 8:
                self._shift += 1
            return b
        if addr < 0x4020:
            return 0
        return self.mapper.read(addr)

    def write(self, addr: int, val: int) -> None:
        addr &= 0xFFFF
        val &= 0xFF
        if addr < 0x2000:
            self.ram[addr & 0x7FF] = val
        elif addr < 0x4000:
            if self.ppu:
                self.ppu.write(addr & 7, val)
        elif addr == 0x4016:
            self._strobe = bool(val & 1)
            if self._strobe:
                self._shift = 0
        elif addr >= 0x4020:
            self.mapper.write(addr, val)

    def read_word(self, addr: int) -> int:
        return self.read(addr) | self.read(addr + 1) << 8

    def write_word(self, addr: int, val: int) -> None:
        self.write(addr, val & 0xFF)
        self.write(addr + 1, val >> 8)
