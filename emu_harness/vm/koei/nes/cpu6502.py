"""
KOEI's banked NES bus for running VM handlers from ROM, plus the 6502 core.

The CPU is `emu_harness.cpu.m6502.cpu.CPU6502` (MIGRATION.md row C), re-exported here so the old
`koeivm.cpu6502.CPU6502` / `na1dream.cpu6502.CPU6502` names keep working. `Memory` (MMC1/MMC5
windows, the fixed bank, SRAM, the MMC5 multiplier) is the NES host binding of the VM.

Scope of Memory:
  - Memory model: ROM banks + SRAM + zero-page RAM
  - MMC1 bank switching (manual -- we set the bank when running a specific sub)
  - No PPU, no APU, no interrupts (we run subs to RTS or hit a stop condition)
"""
from emu_harness.cpu.m6502.cpu import CPU6502  # noqa: F401  (re-export)


class Memory:
    """NES memory map (simplified for running VM handlers from ROM).

    Address ranges:
      $0000-$1FFF — RAM (mirrored every $0800; we use full 8KB)
      $2000-$3FFF — PPU registers (stubbed)
      $4000-$401F — APU/IO (stubbed)
      $6000-$7FFF — SRAM (we load from .dmp file)
      $8000-$BFFF — switchable PRG bank (set via switch_bank)
      $C000-$FFFF — fixed PRG bank 15
    """

    def __init__(self, rom_bytes):
        self.ram = bytearray(0x800)  # 2KB RAM
        self.sram = bytearray(0x2000)  # 8KB SRAM
        self.rom = rom_bytes  # full .nes file (including $10-byte iNES header)
        self.bank_lo = 0  # switchable bank index ($8000-$BFFF)
        # bank_hi is always 15 (fixed at $C000-$FFFF)
        # Switchable-window granularity: 0x4000 = MMC1 16K bank @ $8000-$BFFF (na1/ro3k/gk);
        # 0x2000 = MMC5 8K bank @ $8000-$9FFF, with $A000-$BFFF = the next 8K bank (na2). The
        # fixed $C000-$FFFF region is the last 16K (= bank 30||31) in both modes, unchanged.
        self.bank_size = 0x4000
        self.bank_base = 0x8000   # CPU base of the switchable window (na2 bytecode banks map at $A000)
        # MMC5 hardware multiplier ($5205=multiplicand, $5206=multiplier; read back = 16-bit product).
        self._mult = [0, 0]

        # write trace for debugging
        self.trace_writes = False
        self.write_log = []

    def switch_bank(self, bank):
        """MMC1 bank switching — set which 16KB bank maps to $8000-$BFFF."""
        self.bank_lo = bank

    def _rom_offset(self, addr):
        """Map CPU address $8000-$FFFF to a file offset in the .nes ROM.
        Returns None if not a ROM address.
        """
        if 0x8000 <= addr <= 0xBFFF:
            if self.bank_size == 0x2000:                     # MMC5 8K windows
                b = self.bank_base                           # the bank_lo window ($A000 for na2 bytecode)
                if b <= addr < b + 0x2000:
                    return 0x10 + self.bank_lo * 0x2000 + (addr - b)
                # the other 8K half holds the adjacent bank (rarely read during disasm)
                other = 0x8000 if b == 0xA000 else 0xA000
                return 0x10 + self.bank_lo * 0x2000 + (addr - other)
            return 0x10 + self.bank_lo * 0x4000 + (addr - 0x8000)
        if 0xC000 <= addr <= 0xFFFF:                         # the fixed region: the LAST 16K of PRG
            return 0x10 + (self.rom[4] - 1) * 0x4000 + (addr - 0xC000)
        return None                                          # (was 16K bank 15: right only for 256K
                                                             #  carts; UW's 512K banks 62/63 read 30/31)

    def read(self, addr):
        addr &= 0xFFFF
        if addr < 0x2000:
            return self.ram[addr & 0x7FF]
        if 0x6000 <= addr < 0x8000:
            return self.sram[addr - 0x6000]
        if addr == 0x5205:                                   # MMC5 mult: product low byte
            return (self._mult[0] * self._mult[1]) & 0xFF
        if addr == 0x5206:                                   # MMC5 mult: product high byte
            return ((self._mult[0] * self._mult[1]) >> 8) & 0xFF
        rom_ofs = self._rom_offset(addr)
        if rom_ofs is not None:
            return self.rom[rom_ofs]
        # PPU/APU/etc reads: return 0 (stub)
        return 0

    def write(self, addr, val):
        addr &= 0xFFFF
        val &= 0xFF
        if self.trace_writes:
            self.write_log.append((addr, val))
        if addr < 0x2000:
            self.ram[addr & 0x7FF] = val
        elif 0x6000 <= addr < 0x8000:
            self.sram[addr - 0x6000] = val
        elif addr == 0x5205:                                 # MMC5 mult: multiplicand
            self._mult[0] = val
        elif addr == 0x5206:                                 # MMC5 mult: multiplier
            self._mult[1] = val
        # ROM writes ignored (we don't emulate MMC1 register writes here;
        # bank switching is done explicitly via switch_bank())

    def read_word(self, addr):
        return self.read(addr) | (self.read(addr + 1) << 8)

    def write_word(self, addr, val):
        self.write(addr, val & 0xFF)
        self.write(addr + 1, (val >> 8) & 0xFF)
