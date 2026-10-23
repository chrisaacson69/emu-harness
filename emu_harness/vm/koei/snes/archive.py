#!/usr/bin/env python3
"""
The KOEI SNES resource archive -- the named-blob directory every title's loader searches.

The directory sits in bank $00 at the profile's `resource_archive` (rot3k2/Gemfire $F800,
Aerobiz $F000). Each 16-byte row is

    [name: 8 bytes, NUL-padded ASCII][bank: u16][addr: u16][len: u32]

and the directory ends at the first all-zero row (Aerobiz: 223 rows, terminator at $FDF0).
There is no row cap and the length is a full u32 (Aerobiz AMAP is longer than 64 KiB).
A row that is neither printable nor the terminator is an error, never a silent stop.

A CODE module is a row whose blob starts with `JMP base+3` (its WRAM load address is `base`)
followed by the per-routine trampoline signature (profile `routine_sig`) of its first routine.

    py -3 tools/archive.py <rom> [--game g] [--code]     # list the directory / the code modules
"""
import argparse
import os
import sys
from collections import namedtuple

HERE = os.path.dirname(os.path.abspath(__file__))
from emu_harness.machines.snes.mapper import cpu_to_file, strip_copier_header  # noqa: E402
from emu_harness.vm.koei.snes import profile as _prof  # noqa: E402

ROW_BYTES = 16
Row = namedtuple("Row", "index name bank addr length rom_off dir_addr")
# base = WRAM address of byte 0 (the JMP header); entry = base + 3; end = base + length
Module = namedtuple("Module", "name row base entry end rom_off length image")


class ArchiveError(_prof.ToolError):
    pass


def read_directory(rom, P):
    """Every row of the archive directory at P['resource_archive'] up to the all-zero row."""
    dir_addr = P["resource_archive"]
    rows, i = [], 0
    while True:
        a = dir_addr + ROW_BYTES * i
        if a + ROW_BYTES > 0x10000:
            raise ArchiveError("archive at $00:%04X: no all-zero terminator row before the bank end" % dir_addr)
        o = cpu_to_file(_prof.mapping(P), 0, a)
        e = bytes(rom[o:o + ROW_BYTES])
        if not any(e):
            return rows
        name = e[:8].split(b"\x00")[0]
        if not name or not all(32 <= c < 127 for c in name) or any(e[len(name):8]):
            raise ArchiveError("archive at $00:%04X row %d ($%04X) is not a directory row: %s"
                               % (dir_addr, i, a, e.hex(" ")))
        bank = e[8] | (e[9] << 8)
        addr = e[10] | (e[11] << 8)
        length = e[12] | (e[13] << 8) | (e[14] << 16) | (e[15] << 24)
        rows.append(Row(i, name.decode("ascii"), bank, addr, length, cpu_to_file(_prof.mapping(P), bank, addr), a))
        i += 1


def find(rom, P, name):
    for r in read_directory(rom, P):
        if r.name == name:
            return r
    raise KeyError("no archive row named %r" % name)


def code_module(rom, row, P):
    """The Module for a row if it is bytecode (JMP base+3 header + routine_sig at +3), else None."""
    o = row.rom_off
    sig = bytes(P.get("routine_sig", []))
    if row.length < 3 + len(sig) or rom[o] != 0x4C:
        return None
    entry = rom[o + 1] | (rom[o + 2] << 8)
    if not sig or bytes(rom[o + 3:o + 3 + len(sig)]) != sig:
        return None
    base = (entry - 3) & 0xFFFF
    return Module(row.name, row, base, entry, base + row.length, o, row.length,
                  bytes(rom[o:o + row.length]))


def code_modules(rom, P):
    """Every code module in directory order."""
    out = []
    for r in read_directory(rom, P):
        m = code_module(rom, r, P)
        if m is not None:
            out.append(m)
    return out


def module(rom, P, name):
    m = code_module(rom, find(rom, P, name), P)
    if m is None:
        raise KeyError("archive row %r is not a bytecode module" % name)
    return m


def module_at(rom, P, rom_off):
    """The code module whose blob contains ROM file offset `rom_off` (legacy --start args)."""
    for m in code_modules(rom, P):
        if m.rom_off <= rom_off < m.rom_off + m.length:
            return m
    return None


def main(argv):
    from emu_harness.vm.koei.snes import profile as _profile
    ap = argparse.ArgumentParser(description="List a KOEI SNES title's resource archive.")
    ap.add_argument("rom")
    ap.add_argument("--game", default=None, help="games.toml profile (else $KOEI_SNES_GAME, else the ROM header)")
    ap.add_argument("--code", action="store_true", help="only the bytecode modules")
    a = ap.parse_args(argv[1:])
    rom, _ = strip_copier_header(open(a.rom, "rb").read())
    P = _profile.select(a.game, rom)
    rows = read_directory(rom, P)
    mods = {m.name: m for m in code_modules(rom, P)}
    print("== archive $00:%04X: %d rows, terminator at $%04X; %d code modules ==" % (
        P["resource_archive"], len(rows), P["resource_archive"] + ROW_BYTES * len(rows), len(mods)))
    for r in rows:
        m = mods.get(r.name)
        if a.code and not m:
            continue
        print("%3d %-8s %02X:%04X len $%05X%s" % (r.index, r.name, r.bank, r.addr, r.length,
              ("  code: WRAM $%04X-$%04X entry $%04X" % (m.base, m.end - 1, m.entry)) if m else ""))
    return 0


if __name__ == "__main__":
    from emu_harness.vm.koei.snes import profile as _cli
    sys.exit(_cli.run_cli(main))
