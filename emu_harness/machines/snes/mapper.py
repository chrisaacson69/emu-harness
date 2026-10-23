"""
SNES address <-> file-offset mapping (LoROM / HiROM / ExHiROM).

Grounded in the SNESdev wiki:
  - Memory map:  https://snes.nesdev.org/wiki/Memory_map
  - ROM header:  https://snes.nesdev.org/wiki/ROM_header

The 65C816 sees a 24-bit space: 256 banks ($00-$FF) x 64 KiB. Cartridge ROM is
projected into that space differently depending on the board's wiring:

  LoROM  32 KiB per bank, mapped to $8000-$FFFF of each bank (A15 skipped on the
         cart). file_offset = ((bank & 0x7F) << 15) | (addr & 0x7FFF)
         Internal header lives at file offset 0x7FC0 (CPU $00:FFC0).

  HiROM  64 KiB per bank, mapped 1:1 into banks $C0-$FF (and the top half is
         mirrored into $00-$3F:$8000-$FFFF). file_offset = ((bank & 0x3F) << 16) | addr
         Internal header lives at file offset 0xFFC0 (CPU $00:FFC0).

These functions ignore RAM/IO/mirror regions -- they answer "for a *ROM* CPU
address, where is that byte in the .sfc file", which is what disassembly needs.
"""

LOROM = "lorom"
HIROM = "hirom"
EXHIROM = "exhirom"

# File offset of the 32-byte internal header ($00:FFC0..FFDF in CPU space).
HEADER_FILE_OFFSET = {
    LOROM:   0x7FC0,
    HIROM:   0xFFC0,
    EXHIROM: 0x40FFC0,
}


def cpu_to_file(mapping, bank, addr):
    """CPU (bank, addr) -> flat ROM file offset. addr is the 16-bit in-bank word."""
    if mapping == LOROM:
        if addr < 0x8000:
            raise ValueError("LoROM ROM lives at $8000-$FFFF; $%04X is RAM/IO" % addr)
        return ((bank & 0x7F) << 15) | (addr & 0x7FFF)
    if mapping in (HIROM, EXHIROM):
        return ((bank & 0x3F) << 16) | addr
    raise ValueError("unknown mapping %r" % mapping)


def file_to_cpu(mapping, offset):
    """Flat ROM file offset -> canonical CPU (bank, addr). Inverse of cpu_to_file."""
    if mapping == LOROM:
        bank = (offset >> 15) & 0x7F
        addr = 0x8000 | (offset & 0x7FFF)
        return bank, addr
    if mapping in (HIROM, EXHIROM):
        bank = 0xC0 | ((offset >> 16) & 0x3F)
        addr = offset & 0xFFFF
        return bank, addr
    raise ValueError("unknown mapping %r" % mapping)


def strip_copier_header(data):
    """Return (rom_bytes, had_copier_header).

    Copier dumps (.smc) prepend a 512-byte header. Heuristic: a raw ROM is a
    whole number of 32 KiB banks, so a 512-byte remainder means a copier header.
    """
    if len(data) % 0x8000 == 512:
        return data[512:], True
    return data, False
