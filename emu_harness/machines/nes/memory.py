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

The reverse direction, PRG offset -> CPU address, needs one more fact: the window a switchable bank
runs in. `Windows` holds it. KOEI's MMC5 titles keep each bank in one window ("library" banks at
$8000, app banks at $A000); a title profile lists them. The PRG offset stays the identity of a byte,
and the CPU address is a view of it. `window_votes` is the evidence check: a bank's own code points
at its own entry points at the address it was assembled for (MIGRATION.md row N).
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


def fixed_bank(rom: bytes) -> bytes:
    """The PRG mapped at $C000-$FFFF on the KOEI MMC1/MMC5 boards: the last 16 KiB of PRG.
    (MMC1 fixes the last 16K bank there; KOEI's MMC5 titles map the last two 8K banks.)"""
    prg = rom[16:16 + rom[4] * 0x4000]
    return prg[-0x4000:]


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


@dataclass(frozen=True)
class Windows:
    """The CPU base each PRG bank runs at. Fixed banks come from the cart (MMC1: the last 16K bank
    at $C000; MMC5: the last two 8K banks at $C000 / $E000); a switchable bank takes its entry in
    `at`, else `default`."""
    cart: Cart
    default: int = 0x8000
    at: tuple[tuple[int, int], ...] = ()       # (bank, base) pairs; a tuple keeps the class frozen

    @classmethod
    def from_profile(cls, cart: Cart, profile: dict) -> "Windows":
        """From a title profile in the koei-nes games.toml schema: `bank_base` (the default) and
        `banks_at_8000` / `banks_at_c000` / `banks_at_e000` lists. A bank in two lists takes the
        first, in that order (as koei_vm.bank_base_for always did)."""
        at: dict[int, int] = {}
        for key, base in (("banks_at_8000", 0x8000), ("banks_at_c000", 0xC000), ("banks_at_e000", 0xE000)):
            for b in profile.get(key, []):
                at.setdefault(b, base)
        return cls(cart, profile.get("bank_base", 0x8000), tuple(sorted(at.items())))

    @property
    def bank_size(self) -> int:
        return BANK_SIZE[self.cart.mapper] or self.cart.prg_size

    def fixed(self) -> dict[int, int]:
        """bank -> base for the banks the mapper pins (empty for NROM)."""
        last = self.cart.prg_size // self.bank_size - 1
        return {1: {last: 0xC000}, 5: {last - 1: 0xC000, last: 0xE000}}.get(self.cart.mapper, {})

    def base(self, bank: int, *, profile_only: bool = False) -> int:
        """CPU base of `bank`. `profile_only` skips the cart's fixed banks: the old koei_vm answer,
        kept so a caller can be moved onto this class without changing its output."""
        if not profile_only and bank in self.fixed():
            return self.fixed()[bank]
        return dict(self.at).get(bank, self.default)

    def cpu(self, prg_offset: int) -> int:
        """The CPU address a PRG byte is seen at."""
        bank, off = divmod(prg_offset, self.bank_size)
        return self.base(bank) + off

    def prg(self, bank: int, addr: int) -> int:
        """PRG offset of `addr` in `bank` (the window-independent identity)."""
        return bank * self.bank_size + (addr & (self.bank_size - 1))

    def fits(self, bank: int, addr: int) -> bool:
        """Is `addr` inside the window `bank` runs at? A label at an address its bank is never
        seen at names the right byte through the wrong window."""
        b = self.base(bank)
        return b <= addr < b + self.bank_size


def window_votes(buf: bytes, entries: set[int], bases: tuple[int, ...] = (0x8000, 0xA000),
                 after: frozenset[int] | None = None) -> dict[int, int]:
    """Evidence for where a bank runs: for each candidate base, how many little-endian words in
    `buf` land exactly on one of `entries` (offsets into `buf` of the bank's own entry points, e.g.
    KOEI's `JSR vm_entry` stubs) if the bank sits at that base. A call into another bank lands on
    an arbitrary byte here and rarely on an entry, so it adds little noise.

    `after` counts only words right after one of these bytes (e.g. {0x20, 0x4C}: JSR / JMP abs
    operands). That is the fallback for a bank with no stubs (a native sound engine), with `entries`
    = every documented-opcode offset. It is weaker: an app bank's calls into library banks in the
    other window count too, so trust only a lopsided result."""
    idx = range(1, len(buf) - 1) if after is not None else range(len(buf) - 1)
    words = [buf[i] | buf[i + 1] << 8 for i in idx if after is None or buf[i - 1] in after]
    return {b: sum(1 for w in words if (w - b) in entries) for b in bases}


PPU_REGS = ("CTRL", "MASK", "STATUS", "OAMADDR", "OAMDATA", "SCROLL", "ADDR", "DATA")


def register_name(addr: int) -> str | None:
    """The NES CPU-visible hardware register at `addr` ($2000-$2007 as listed, $4014 OAM DMA,
    $4016/$4017 the pads, $4000-$4013/$4015 the APU), or None. The PPU mirrors at $2008-$3FFF
    are not folded (as Mappy's leaf labeler had it)."""
    if addr == 0x4014:
        return "OAMDMA"
    if addr in (0x4016, 0x4017):
        return "JOYPAD"
    if 0x2000 <= addr <= 0x2007:
        return "PPU:" + PPU_REGS[addr - 0x2000]
    if addr == 0x4015:
        return "APU:STATUS"
    if 0x4000 <= addr <= 0x4013:
        return "APU:$%04X" % addr
    return None
