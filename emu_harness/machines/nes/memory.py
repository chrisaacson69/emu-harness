"""Where an NES address lives: (registry section, CPU address) -> (region, offset).

The cartridge facts come from the iNES header, not from flags: mapper number, PRG size, and
whether $6000-$7FFF is battery-backed. Grounding: NESdev Wiki, "INES", "NROM", "MMC1", "MMC5".

PRG labels sit under a `[prg.bankN]` section, N counted in the mapper's bank size. For the
fixed region the CPU address is authoritative, not the section (a fixed-bank routine is often
filed under the bank that *uses* it):

    NROM   one image mirrored from $8000:          offset = (addr - $8000) mod PRG size
    MMC1   16 KiB banks, $C000-$FFFF = last bank:  offset = bank * $4000 + (addr & $3FFF)
    MMC5   8 KiB banks, $C000-$DFFF / $E000-$FFFF = the last two banks (the mode KOEI uses):
                                                   offset = bank * $2000 + (addr & $1FFF)

Any other section is located by address alone.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_BANK = re.compile(r"^prg\.bank(\d+)$")
BANK_SIZE = {0: None, 1: 0x4000, 5: 0x2000}   # mapper -> PRG bank size (NROM: unbanked)


@dataclass(frozen=True)
class Cart:
    mapper: int
    prg_size: int
    battery: bool

    @classmethod
    def from_ines(cls, rom: str | Path) -> "Cart":
        h = Path(rom).read_bytes()[:16]
        if h[:4] != b"NES\x1a":
            raise ValueError(f"{rom}: not an iNES file")
        mapper = (h[6] >> 4) | (h[7] & 0xF0)
        if mapper not in BANK_SIZE:
            raise NotImplementedError(f"mapper {mapper}: only NROM (0), MMC1 (1) and MMC5 (5) so far")
        return cls(mapper, h[4] * 0x4000, bool(h[6] & 0x02))


def locate(cart: Cart, section: str, addr: int) -> tuple[str, int] | None:
    """-> (region, offset), or None if a [prg.bankN] label is not a PRG address ($8000+) or
    falls outside the PRG image."""
    m = _BANK.match(section)
    if m:
        if addr < 0x8000:          # not a PRG CPU address (e.g. a bank-relative offset filed by mistake)
            return None
        bank, size = int(m.group(1)), BANK_SIZE[cart.mapper]
        if size is None:
            off = (addr - 0x8000) % cart.prg_size
        else:
            last = cart.prg_size // size - 1
            if addr >= 0xC000 and cart.mapper == 1:
                bank = last
            elif addr >= 0xC000 and cart.mapper == 5:
                bank = last - 1 if addr < 0xE000 else last
            off = bank * size + (addr & (size - 1))
        return ("nes.prg_rom", off) if 0 <= off < cart.prg_size else None
    if addr < 0x2000:
        return "nes.internal_ram", addr & 0x07FF
    if 0x6000 <= addr <= 0x7FFF:
        return ("nes.save_ram" if cart.battery else "nes.work_ram"), addr - 0x6000
    return "nes.cpu_bus", addr
