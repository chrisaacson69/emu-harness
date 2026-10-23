#!/usr/bin/env python3
"""
Linear KOEI SNES bytecode disassembler (full 256-opcode primary table + $B7 extended).

Opcode names, control-flow classes and operand layouts come from the ONE shared table
(data/vm_opcode_names.toml via tools/vm_isa.py); every operand size is derived from the
target ROM's own handlers and cross-checked against that table at load (vm_isa.bind raises
on a mismatch). Lines are printed by the shared lister (tools/listing.py, the same format as
the .src layer): address, raw bytes, the instruction with every operand value, annotation.
Linear = it decodes straight through data too; use vm_walk.py / gen_source.py for code-vs-data
by reachability (and for syscall ids, which need the routine's control flow: a linear listing
cannot know which id reaches a call, so a call to a syscall stub is annotated by name only).

    py -3 tools/vm_disasm.py <rom> [--game g] --start 01:8008 --base 7E:1008 -n 40 [--syms data/COMMON]
    (without --base the addresses are ROM file offsets)
"""
import argparse
import os
import sys
HERE = os.path.dirname(os.path.abspath(__file__))
from emu_harness.machines.snes.mapper import cpu_to_file, strip_copier_header, LOROM  # noqa: E402
from emu_harness.vm.koei.snes import listing  # noqa: E402
from emu_harness.vm.koei.snes import profile as _profile  # noqa: E402
from emu_harness.vm.koei.snes import vm_isa as ISA  # noqa: E402


def main(argv):
    from emu_harness.vm.koei.snes.symbols import Symbols
    ap = argparse.ArgumentParser()
    ap.add_argument("rom")
    ap.add_argument("--start", required=True)
    ap.add_argument("--game", default=None, help="games.toml profile (else $KOEI_SNES_GAME, else the ROM header)")
    ap.add_argument("--base", default=None)
    ap.add_argument("-n", "--count", type=int, default=40)
    ap.add_argument("--syms", default=None, help="module data dir (strings/routines/globals + enums.py) for name annotations")
    a = ap.parse_args(argv[1:])
    rom, _ = strip_copier_header(open(a.rom, "rb").read())
    P = _profile.select(a.game, rom)
    S = Symbols(a.syms, profile=P)
    isa = ISA.bind(rom, P)
    b, o = a.start.split(":")
    off0 = cpu_to_file(LOROM, int(b, 16), int(o, 16))
    if a.base:
        _vb, v = a.base.split(":")
        v0 = int(v, 16)
    else:
        v0 = off0                                # no base: address = file offset
    read = lambda v: rom[off0 + (v - v0)] if 0 <= off0 + (v - v0) < len(rom) else None
    L = listing.Lister(isa, S, None, read_any=read)
    v = v0
    for _ in range(a.count):
        ins = isa.decode(read, v)
        for line in L.lines(ins, read=read):
            print(line)
        v += max(1, ins.length)
    return 0


if __name__ == "__main__":
    from emu_harness.vm.koei.snes import profile as _cli
    sys.exit(_cli.run_cli(main))
