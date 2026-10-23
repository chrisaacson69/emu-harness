#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Assembly layer of the layered decompilation: a labelled 65C816 disassembly of a KOEI SNES
title's native kernel (bank $00 -- the VM interpreter, syscall dispatch, loader, handlers).
Parallels gen_source.py (bytecode layer): it wraps the existing snes-decompiler disassembler
(disasm65816.disassemble_rom + w65c816.render) and injects the named-routine labels, so the
native floor reads as source instead of raw bytes.

    py -3 tools/gen_native.py <rom> [--game g] --start 00:8000 --end BA60 \
        --syms <title>/data/native/routines.toml > native.asm

Labels come from a native routines.toml (0xADDR = "name"  # "comment"); the games.toml VM-spine
keys of the title's profile (--game, else $KOEI_SNES_GAME, else the ROM header; checked against
the ROM) are merged in so the interpreter/dispatch/loader are named even for titles without a
full native table.
"""
import argparse
import os
import re
import sys
HERE = os.path.dirname(os.path.abspath(__file__))
from emu_harness.machines.snes.mapper import strip_copier_header, cpu_to_file, LOROM  # noqa: E402
from emu_harness.machines.snes.header import detect  # noqa: E402
from emu_harness.machines.snes import disasm as DA  # noqa: E402
from emu_harness.cpu.w65c816 import isa as w65c816  # noqa: E402
from emu_harness.vm.koei.snes import profile as _profile  # noqa: E402

LBL_RE = re.compile(r'^0x([0-9A-Fa-f]+)\s*=\s*"([^"]*)"(?:\s*#\s*"?([^"\n]*)"?)?')
TGT_RE = re.compile(r'\$([0-9A-Fa-f]{4})\b')


def load_labels(path, P):
    labels = {}
    if path and os.path.isfile(path):
        for line in open(path, encoding="utf-8"):
            m = LBL_RE.match(line.strip())
            if m:
                labels[int(m.group(1), 16)] = (m.group(2), (m.group(3) or "").strip())
    # merge the VM-spine entry points from the title's games.toml profile (bank $00 addrs)
    for key in ("interpreter_loop", "run_vm_entry", "syscall_dispatch", "ext_dispatch",
                "ext_interpreter", "primary_table", "extended_table", "syscall_table",
                "invalid_handler", "loader", "reset", "abi_table"):
        a = P.get(key)
        if isinstance(a, int) and a not in labels:
            labels[a] = (key, "profile VM-spine")
    return labels


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("rom")
    ap.add_argument("--start", required=True, help="BANK:ADDR, e.g. 00:8000")
    ap.add_argument("--end", required=True, help="stop CPU addr (hex), e.g. BA60")
    ap.add_argument("--syms", default=None, help="native routines.toml (optional)")
    ap.add_argument("--mx", default="8,8")
    ap.add_argument("--game", default=None, help="games.toml profile (else $KOEI_SNES_GAME, else the ROM header)")
    a = ap.parse_args(argv[1:])

    rom, _ = strip_copier_header(open(a.rom, "rb").read())
    P = _profile.select(a.game, rom)
    score, f = detect(rom)
    mapping = f["mapping_guess"] if f else LOROM
    b, ad = a.start.split(":"); bank, addr = int(b, 16), int(ad, 16)
    end = int(a.end, 16)
    start_off = cpu_to_file(mapping, bank, addr)
    mw, xw = (s.strip() for s in a.mx.split(","))
    m8, x8 = (mw == "8"), (xw == "8")
    labels = load_labels(a.syms, P)

    print("; ==== native kernel (65C816, bank $%02X) - %d named routines, $%04X-$%04X ====" %
          (bank, len(labels), addr, end))
    # a generous instruction cap; we break on the end address
    for ins in DA.disassemble_rom(rom, mapping, start_off, 0x8000, m8, x8):
        if ins["addr"] >= end and ins["bank"] == bank:
            break
        if ins["addr"] in labels:
            nm, cm = labels[ins["addr"]]
            print("\n%s:%s" % (nm, ("   ; " + cm) if cm else ""))
        line = w65c816.render(ins)
        # annotate a call/jump whose target is a named routine
        mn = ins.get("mnem", "")
        if mn in ("JSR", "JSL", "JMP", "JML", "BRA", "BRL"):
            mt = TGT_RE.search(ins.get("text", ""))
            if mt:
                t = int(mt.group(1), 16)
                if t in labels:
                    line = "%-48s ; -> %s" % (line, labels[t][0])
        print(line)


if __name__ == "__main__":
    from emu_harness.vm.koei.snes import profile as _cli
    sys.exit(_cli.run_cli(main))
