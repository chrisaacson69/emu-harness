#!/usr/bin/env python3
"""
SNES driver for the `w65c816` CPU decoder.

Layering (mirrors the NES side: generic CPU core + cart wiring):
  - w65c816.py   = the CPU (65C816 opcodes + decode + M/X tracking). System-agnostic.
  - THIS FILE    = the SNES wiring: load a ROM, detect LoROM/HiROM, and translate
                   between flat file offsets and 65C816 (bank,addr) via `mapper`.

    py -3 tools/disasm65816.py <rom>                       # from the RESET vector
    py -3 tools/disasm65816.py <rom> --start 01:CD51 -n 30 # from a CPU address
    py -3 tools/disasm65816.py <rom> --start 00:800C --mx 8,8

--mx sets the assumed {accumulator,index} widths at the start (8 or 16). Linear
tracking is right along a straight path; after a branch join it can be stale --
pass --mx to pin it, or read the `; m16`/`; x8` annotations on each immediate.
"""
import argparse
import os
import sys

from emu_harness.machines.snes.mapper import cpu_to_file, file_to_cpu, strip_copier_header
from emu_harness.machines.snes.header import detect
from emu_harness.cpu.w65c816 import isa as w65c816


def disassemble_rom(rom, mapping, start_off, count, m8, x8, vbank=None, vaddr=None):
    """Yield decoded instructions for `count` steps from flat offset start_off.

    Normally the displayed CPU address comes from the mapping (file_to_cpu). If
    (vbank,vaddr) is given, the bytes are read from the ROM at start_off but shown
    as if executing at that VIRTUAL base -- used for code that is copied elsewhere
    before it runs (e.g. a KOEI resource blob relocated into WRAM $7E:2000)."""
    off = start_off
    vb, va = vbank, vaddr
    for _ in range(count):
        if off >= len(rom):
            return
        if vb is None:
            bank, addr = file_to_cpu(mapping, off)
        else:
            bank, addr = vb, va
        ins, m8, x8 = w65c816.decode(rom, off, bank, addr, m8, x8)
        yield ins
        off += ins["len"]
        if vb is not None:
            va += ins["len"]
            if va > 0xFFFF:
                vb = (vb + (va >> 16)) & 0xFF
                va &= 0xFFFF


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("rom")
    ap.add_argument("--start", default=None,
                    help="CPU start BANK:ADDR hex (e.g. 00:800C). Default = RESET vector.")
    ap.add_argument("-n", "--count", type=int, default=32)
    ap.add_argument("--mx", default="8,8",
                    help="assumed accumulator,index widths at start (8 or 16). Default 8,8 (reset).")
    ap.add_argument("--base", default=None,
                    help="virtual run address BANK:ADDR for relocated code: read from --start "
                         "in ROM but display/target as if executing here (e.g. 7E:2000 for a WRAM blob).")
    args = ap.parse_args(argv[1:])

    data = open(args.rom, "rb").read()
    rom, _ = strip_copier_header(data)
    score, f = detect(rom)
    if f is None or score < 4:
        sys.exit("no confident SNES header (score %d)" % score)
    mapping = f["mapping_guess"]

    if args.start:
        b, a = args.start.split(":")
        bank, addr = int(b, 16), int(a, 16)
    else:
        bank, addr = 0x00, f["reset_vector"]
    start_off = cpu_to_file(mapping, bank, addr)

    mw, xw = (s.strip() for s in args.mx.split(","))
    m8, x8 = (mw == "8"), (xw == "8")

    vbank = vaddr = None
    if args.base:
        vb, va = args.base.split(":")
        vbank, vaddr = int(vb, 16), int(va, 16)

    sys.stderr.write("mapping=%s  start=$%02X:%04X (file 0x%06X)%s  m%s x%s\n" %
                     (mapping.upper(), bank, addr, start_off,
                      ("  base=$%02X:%04X" % (vbank, vaddr)) if args.base else "",
                      "8" if m8 else "16", "8" if x8 else "16"))
    for ins in disassemble_rom(rom, mapping, start_off, args.count, m8, x8, vbank, vaddr):
        print(w65c816.render(ins))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
