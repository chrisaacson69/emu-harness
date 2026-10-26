#!/usr/bin/env python3
"""
window_check.py - does each bank run where the title profile says? And does every registry label
sit at an address its bank is actually seen at?

  1. Profile vs evidence: for every switchable bank, count the 16-bit words in the bank that land
     exactly on one of the bank's own sub stubs (`vm_entry_stub`, plus UW's `JSR $E299` native stub)
     with the bank at $8000 vs $A000 (emu_harness window_votes). A clear majority (>2x, >=3 words)
     that disagrees with games.toml is reported. A bank with no stubs (a native sound engine) falls
     back to its JSR/JMP targets landing on documented opcodes, which an app bank's library calls
     also feed, so it must be lopsided (>5x, >=10).
  2. Registry: a `[prg.bankN]` label below $C000 whose CPU address is outside bank N's window. Either
     the profile is wrong (check 1), or the label was filed under the bank that USES it while the
     byte lives in the bank sharing the other window, so `locate` puts it on the wrong byte (the
     .mlb then shows it in the wrong place and may drop a correct name), or a tool rendered the
     bank at the wrong base (a correct byte under a wrong CPU address).

The review tool for the per-project revisits (emu-harness MIGRATION.md row N); it changes nothing.
Was koei-nes `tools/window_check.py` (moved 2026-10-26; koei-nes keeps an alias).

Usage:
  py -3.14 -m emu_harness.vm.koei.nes.window_check <title> [<title> ...] [-v]   # -v: every bank's votes
"""
import argparse
import sys
from pathlib import Path

from ....core.symbols import read_registry
from ....cpu.m6502.disasm import OPS
from ....machines.nes.memory import window_votes
from .titles import game_cfg, windows

UW_NATIVE_STUB = bytes([0x20, 0x99, 0xE2])


def check(title, verbose=False):
    cfg = game_cfg(title)
    win = windows(cfg)
    rom = Path(cfg["_rom"]).read_bytes()
    prg = rom[16:16 + rom[4] * 0x4000]
    size = win.bank_size
    stubs_pat = [bytes(int(x, 16) for x in cfg["vm_entry_stub"].split())]
    if title == "uw":
        stubs_pat.append(UW_NATIVE_STUB)
    problems = 0
    print(f"## {title}: {len(prg) // size} banks of {size // 1024}K")
    for b in range(len(prg) // size):
        if b in win.fixed():
            continue
        bk = prg[b * size:(b + 1) * size]
        stubs = {o for o in range(len(bk) - 2) if bk[o:o + 3] in stubs_pat}
        if stubs:                       # strong: words landing on the bank's own stubs
            kind, v = f"{len(stubs):3d} stubs; words onto them", window_votes(bk, stubs)
            ratio, floor = 2, 3
        else:                           # weaker: JSR / JMP targets onto documented opcodes
            ops = {o for o in range(len(bk)) if bk[o] in OPS}
            kind, v = "no stubs; JSR/JMP targets", window_votes(bk, ops, after=frozenset({0x20, 0x4C}))
            ratio, floor = 5, 10
        a, x = v[0x8000], v[0xA000]
        best = 0x8000 if a > ratio * x and a >= floor else 0xA000 if x > ratio * a and x >= floor else None
        bad = best is not None and best != win.base(b)
        problems += bad
        if bad or verbose:
            print(f"  bank {b:2d}: {kind} at $8000 {a:4d} / $A000 {x:4d}; "
                  f"profile ${win.base(b):04X}" + (f"  <-- evidence says ${best:04X}" if bad else ""))
    reg = read_registry(cfg["_labels"])
    for e in reg.entries:
        if e.section.startswith("prg.bank") and 0x8000 <= e.addr < 0xC000:
            b = int(e.section[8:])
            if b not in win.fixed() and not win.fits(b, e.addr):
                problems += 1
                print(f"  label [{e.section}] ${e.addr:04X} {e.name}: bank {b} runs at ${win.base(b):04X} "
                      f"(line {e.line})")
    print(f"  {problems} problem(s)")
    return problems


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("titles", nargs="+")
    ap.add_argument("-v", action="store_true")
    a = ap.parse_args(argv)
    return 1 if sum(check(t, a.v) for t in a.titles) else 0


if __name__ == "__main__":
    sys.exit(main())
