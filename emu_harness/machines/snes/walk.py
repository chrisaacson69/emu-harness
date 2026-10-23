#!/usr/bin/env python3
"""
Recursive-descent code walker for the 65C816 (SNES driver over `w65c816`).

Design follows the CPU's real addressing model: a 24-bit space that is
bank(8) : offset(16) -- a *clean concatenation*, no overlap, no bank-swapping
(unlike the NES's MMC-windowed banks). So the address->byte source is a STATIC
one-to-one map, declared once as a list of Segments:

  - LoROM ROM banks  (fixed board wiring: cpu bank:addr -> linear ROM file offset)
  - overlays         (a resource copied elsewhere before it runs, e.g. KOEI "root"
                      relocated to WRAM $7E:2000 <- ROM $01:8000)

The walk respects the Program Bank Register: JMP/branches stay IN their bank (PC
wraps at $FFFF); only JSL/JML/RTL change the code bank. It follows JSR/JSL/JMP/
JML/branches with per-path M/X width, stops at RTS/RTL/RTI/STP and at unresolved
indirect jumps, and flags decode CONFLICTS (starting an instruction inside one
already decoded) -- the tell that separates real code from data/bytecode.

    py -3 tools/walk65816.py <rom> --root            # walk the KOEI "root" WRAM blob
    py -3 tools/walk65816.py <rom>                    # walk from RESET + $00:8000 table
    py -3 tools/walk65816.py <rom> --root --list      # + full sorted disassembly
    py -3 tools/walk65816.py <rom> --entry 00:9A4C --mx 8,16
"""
import argparse
import os
import sys

from emu_harness.machines.snes.mapper import cpu_to_file, strip_copier_header, LOROM, HIROM, EXHIROM
from emu_harness.machines.snes.header import detect
from emu_harness.cpu.w65c816 import isa as w65c816

COND ={"BPL", "BMI", "BVC", "BVS", "BCC", "BCS", "BNE", "BEQ"}
UNCOND_BR = {"BRA", "BRL"}
JUMP = {"JMP", "JML"}
CALL = {"JSR", "JSL"}
RET = {"RTS", "RTL", "RTI", "STP"}
INDIRECT_MODES = {"ind", "iax", "ial"}   # JMP (abs) / JMP (abs,X) / JML [abs]


class Segment:
    """A CPU range [bank:lo, bank:hi) backed by ROM bytes at file_base+ (addr-lo)."""
    def __init__(self, bank, lo, hi, file_base, name):
        self.bank, self.lo, self.hi, self.file_base, self.name = bank, lo, hi, file_base, name

    def resolve(self, bank, addr):
        if bank == self.bank and self.lo <= addr < self.hi:
            return self.file_base + (addr - self.lo)
        return None


class AddressSpace:
    """Static 24-bit map. Explicit overlays win; else LoROM ROM wiring."""
    def __init__(self, rom, mapping, overlays):
        self.rom, self.mapping, self.overlays = rom, mapping, overlays

    def resolve(self, bank, addr):
        for seg in self.overlays:               # overlays take precedence
            f = seg.resolve(bank, addr)
            if f is not None:
                return f if f < len(self.rom) else None
        m = self.mapping
        if m == LOROM:
            if (bank & 0x7F) <= 0x7D and addr >= 0x8000:
                f = cpu_to_file(LOROM, bank, addr)
                return f if f < len(self.rom) else None
        elif m in (HIROM, EXHIROM):
            b = bank & 0x7F                      # fold the $80 FastROM mirror
            full = (0x40 <= b <= 0x7D)           # $40-$7D and $C0-$FF: whole 64K bank is ROM
            mirror = (b <= 0x3F and addr >= 0x8000)  # $00-$3F/$80-$BF:$8000-$FFFF upper-half mirror
            if full or mirror:
                f = cpu_to_file(m, bank, addr)
                return f if f < len(self.rom) else None
        return None                              # RAM / registers / unmapped


def walk(space, entries, max_insns=300000):
    starts = {}          # akey -> instruction
    interior = set()     # akey covered by the body of some instruction
    subs = set()         # call targets (subroutine entries)
    indirects = []       # (bank,addr,mnem,mode) unresolved computed transfers
    data_stops = []      # (bank,addr) tried to execute non-ROM / unmapped
    conflicts = []       # (bank,addr) started an instruction mid another one
    seen_entry = set()
    work = list(entries)

    while work:
        bank, addr, m8, x8 = work.pop()
        while True:
            akey = (bank << 16) | addr
            if akey in starts:
                break                            # already walked from here
            if akey in interior:
                conflicts.append((bank, addr))
                break
            foff = space.resolve(bank, addr)
            if foff is None:
                data_stops.append((bank, addr))
                break
            if len(starts) >= max_insns:
                return dict(starts=starts, subs=subs, indirects=indirects,
                            data_stops=data_stops, conflicts=conflicts, capped=True)
            ins, m8, x8 = w65c816.decode(space.rom, foff, bank, addr, m8, x8)
            starts[akey] = ins
            for k in range(1, ins["len"]):
                interior.add(akey + k)
            mnem, mode, tgt = ins["mnem"], ins["mode"], ins["target"]
            nxt = addr + ins["len"]

            def enqueue(t, mm, xx):
                if t is None:
                    return
                tk = (t[0] << 16) | t[1]
                if tk not in starts and (t[0], t[1], mm, xx) not in seen_entry:
                    seen_entry.add((t[0], t[1], mm, xx))
                    work.append((t[0], t[1], mm, xx))

            if mnem in RET:
                break
            if mnem in CALL:
                if mode in ("abs", "abl"):
                    subs.add(tgt)
                    enqueue(tgt, m8, x8)
                else:                            # JSR (abs,X): indirect call
                    indirects.append((bank, addr, mnem, mode))
                # a call returns -> keep walking after it
            elif mnem in UNCOND_BR:
                enqueue(tgt, m8, x8)
                break
            elif mnem in JUMP:
                if mode in INDIRECT_MODES:
                    indirects.append((bank, addr, mnem, mode))
                else:
                    enqueue(tgt, m8, x8)
                break
            elif mnem in COND:
                enqueue(tgt, m8, x8)             # branch taken
                # fall through to next instruction

            if nxt > 0xFFFF:                     # PC would leave the bank -> stop
                break
            addr = nxt

    return dict(starts=starts, subs=subs, indirects=indirects,
                data_stops=data_stops, conflicts=conflicts, capped=False)


def parse_ba(s):
    b, a = s.split(":")
    return int(b, 16), int(a, 16)


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("rom")
    ap.add_argument("--root", action="store_true",
                    help="register the KOEI 'root' overlay ($7E:2000 <- ROM $01:8000, len from $F800) and walk it")
    ap.add_argument("--entry", action="append", default=[],
                    help="extra entry point BANK:ADDR (repeatable)")
    ap.add_argument("--mx", default="8,16", help="widths at entries (default 8,16)")
    ap.add_argument("--list", action="store_true", help="print full sorted disassembly")
    args = ap.parse_args(argv[1:])

    rom, _ = strip_copier_header(open(args.rom, "rb").read())
    score, f = detect(rom)
    if f is None or score < 4:
        sys.exit("no confident SNES header (score %d)" % score)
    mapping = f["mapping_guess"]
    mw, xw = (s.strip() for s in args.mx.split(","))
    m8, x8 = (mw == "8"), (xw == "8")

    overlays, entries = [], []
    if args.root:
        # descriptor "root" at $00:F800: +8 bank, +10 addr, +12 len
        d = cpu_to_file(mapping, 0x00, 0xF800)
        src_bank = rom[d + 8] | (rom[d + 9] << 8)
        src_addr = rom[d + 10] | (rom[d + 11] << 8)
        length = rom[d + 12] | (rom[d + 13] << 8)
        fbase = cpu_to_file(mapping, src_bank, src_addr)
        overlays.append(Segment(0x7E, 0x2000, 0x2000 + length, fbase, "root"))
        entries.append((0x7E, 0x2000, m8, x8))
        sys.stderr.write("root overlay: $7E:2000..%04X <- $%02X:%04X (file 0x%06X, len $%04X)\n"
                         % (0x2000 + length, src_bank, src_addr, fbase, length))
    if not args.root and not args.entry:
        entries.append((0x00, f["reset_vector"], True, True))     # RESET (emulation 8-bit)
        for slot in range(0x8000, 0x800C, 3):                     # the $00:8000 JSL trampoline table
            entries.append((0x00, slot, m8, x8))
    for e in args.entry:
        b, a = parse_ba(e)
        entries.append((b, a, m8, x8))

    space = AddressSpace(rom, mapping, overlays)
    r = walk(space, entries)

    starts = r["starts"]
    cov = sum(i["len"] for i in starts.values())
    print("== walk summary ==")
    print("reachable instructions : %d%s" % (len(starts), "  (CAPPED)" if r["capped"] else ""))
    print("bytes covered          : %d" % cov)
    print("subroutine entries     : %d" % len(r["subs"]))
    print("decode CONFLICTS       : %d   (overlap = data/bytecode/bad-M/X signal)" % len(r["conflicts"]))
    print("unresolved indirects   : %d" % len(r["indirects"]))
    print("data/unmapped stops    : %d" % len(r["data_stops"]))
    if r["conflicts"]:
        print("  first conflicts:", ", ".join("$%02X:%04X" % c for c in sorted(set(r["conflicts"]))[:8]))
    if r["indirects"]:
        print("  indirect transfers:")
        for b, a, mn, md in sorted(set(r["indirects"]))[:12]:
            print("    $%02X:%04X  %s (%s)" % (b, a, mn, md))
    if r["subs"]:
        ss = sorted(r["subs"])
        print("  subroutines (first 16):", ", ".join("$%02X:%04X" % s for s in ss[:16]))

    if args.list:
        print("\n== disassembly (sorted) ==")
        for akey in sorted(starts):
            print(w65c816.render(starts[akey]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
