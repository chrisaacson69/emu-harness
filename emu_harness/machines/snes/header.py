#!/usr/bin/env python3
"""
Parse an SNES cartridge's internal ROM header and auto-detect its mapping.

    py -3 tools/header_parse.py path/to/game.sfc

Grounded in https://snes.nesdev.org/wiki/ROM_header . The 32-byte header sits at
CPU $00:FFC0, which lands at a different *file* offset per mapping (LoROM 0x7FC0,
HiROM 0xFFC0). We don't trust the filename -- we score each candidate location by
the checksum/complement identity + map-mode byte + title printability, then report
the winner. Nothing here is fabricated; every offset traces to the wiki table.
"""
import os
import sys

from emu_harness.machines.snes.mapper import LOROM, HIROM, EXHIROM, HEADER_FILE_OFFSET, strip_copier_header

# Field offsets *within* the 32-byte header (base = title start = CPU $FFC0).
F_TITLE      = (0x00, 21)
F_MAPMODE    = 0x15   # $FFD5  001smmmm : speed bit + 5-bit map mode
F_CHIPSET    = 0x16   # $FFD6  RAM / battery / coprocessor flags
F_ROMSIZE    = 0x17   # $FFD7  1<<N KiB
F_RAMSIZE    = 0x18   # $FFD8  1<<N KiB
F_COUNTRY    = 0x19   # $FFD9
F_DEVID      = 0x1A   # $FFDA
F_VERSION    = 0x1B   # $FFDB
F_CKSUM_COMP = 0x1C   # $FFDC  (2 bytes LE) checksum ^ 0xFFFF
F_CKSUM      = 0x1E   # $FFDE  (2 bytes LE) 16-bit sum of all ROM bytes
F_VECTORS    = 0x20   # $FFE0  32 bytes of native+emulation vectors
V_RESET_EMU  = 0x3C   # $FFFC  emulation-mode RESET vector = entry point

MODE_NAME = {0x0: LOROM, 0x1: HIROM, 0x5: EXHIROM}


def _u16(b, i):
    return b[i] | (b[i + 1] << 8)


def score_header(rom, mapping):
    """Return (score, fields|None). Higher score = more likely this mapping."""
    base = HEADER_FILE_OFFSET[mapping]
    if base + 0x40 > len(rom):
        return -1, None
    h = rom[base:base + 0x40]
    score = 0

    cksum = _u16(h, F_CKSUM)
    comp = _u16(h, F_CKSUM_COMP)
    if (cksum ^ comp) == 0xFFFF:          # the defining header integrity test
        score += 4

    mode = h[F_MAPMODE]
    if (mode & 0xE0) == 0x20:             # top bits must be 001 for a valid byte
        score += 1
    if MODE_NAME.get(mode & 0x0F) == mapping:
        score += 2

    title = h[F_TITLE[0]:F_TITLE[0] + F_TITLE[1]]
    printable = sum(1 for c in title if 0x20 <= c <= 0x7E)
    score += printable // 6               # up to +3 for a clean ASCII title

    fields = {
        "mapping_guess": mapping,
        "title": title.decode("ascii", "replace").rstrip(),
        "map_mode": mode,
        "chipset": h[F_CHIPSET],
        "rom_size_kib": 1 << h[F_ROMSIZE],
        "ram_size_kib": (1 << h[F_RAMSIZE]) if h[F_RAMSIZE] else 0,
        "country": h[F_COUNTRY],
        "dev_id": h[F_DEVID],
        "version": h[F_VERSION],
        "checksum": cksum,
        "checksum_comp": comp,
        "reset_vector": _u16(h, V_RESET_EMU),
    }
    return score, fields


def detect(rom):
    best = (-1, None)
    for mapping in (LOROM, HIROM, EXHIROM):
        s, f = score_header(rom, mapping)
        if s > best[0]:
            best = (s, f)
    return best


def main(argv):
    if len(argv) != 2:
        print("usage: header_parse.py <rom.sfc|.smc>", file=sys.stderr)
        return 2
    with open(argv[1], "rb") as fh:
        data = fh.read()
    rom, copier = strip_copier_header(data)

    score, f = detect(rom)
    if f is None or score < 4:
        print("Could not confidently detect an SNES header "
              "(best score %d). Corrupt, interleaved, or non-SNES image?" % score)
        return 1

    computed = sum(rom) & 0xFFFF   # exact only for power-of-two ROMs
    print("File            : %s (%d bytes%s)" %
          (argv[1], len(data), ", +512 copier header stripped" if copier else ""))
    print("Mapping         : %s  (score %d)" % (f["mapping_guess"].upper(), score))
    print("Title           : %r" % f["title"])
    print("Map/speed byte  : $%02X" % f["map_mode"])
    print("Chipset byte    : $%02X" % f["chipset"])
    print("ROM size        : %d KiB (declared)" % f["rom_size_kib"])
    print("RAM (SRAM) size : %d KiB" % f["ram_size_kib"])
    print("Country / dev   : $%02X / $%02X   version $%02X" %
          (f["country"], f["dev_id"], f["version"]))
    print("Checksum        : $%04X   complement $%04X   (xor=%s)" %
          (f["checksum"], f["checksum_comp"],
           "OK" if (f["checksum"] ^ f["checksum_comp"]) == 0xFFFF else "BAD"))
    print("Computed sum    : $%04X   (%s)" %
          (computed, "match" if computed == f["checksum"] else "differs -- non-2^N size or bad dump"))
    print("RESET vector    : $%04X  <- disassembly entry point (bank $00)" % f["reset_vector"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
