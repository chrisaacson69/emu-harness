#!/usr/bin/env python3
"""
Data walk: scan a KOEI SNES module's blob for null-terminated ASCII strings and emit
them as `addr = "text"` symbols (the game's readable text -- prompts, event messages,
labels). KOEI stores plain ASCII with $00 terminators ($0A = newline, $20 = pad).

    py -3 tools/data_walk.py <rom> --start 01:8000 --base 7E:2000 --len 51F2 [--min 4]
Emits TOML lines to stdout (redirect into the module's data/<mod>/strings.toml).
"""
import argparse
import os
import sys
HERE = os.path.dirname(os.path.abspath(__file__))
from emu_harness.machines.snes.header import detect
from emu_harness.machines.snes.mapper import cpu_to_file, strip_copier_header, LOROM
from emu_harness.passes.strings import slug_plain as slug   # row L: the shared name stems


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("rom"); ap.add_argument("--start", required=True); ap.add_argument("--base", required=True)
    ap.add_argument("--len", required=True); ap.add_argument("--min", type=int, default=4)
    a = ap.parse_args(argv[1:])
    rom, _ = strip_copier_header(open(a.rom, "rb").read())
    _score, f = detect(rom)                                     # the cart's mapping, from its header
    sb, so = a.start.split(":"); off0 = cpu_to_file(f["mapping_guess"] if f else LOROM, int(sb, 16), int(so, 16))
    vb, vo = a.base.split(":"); vaddr0 = int(vo, 16)
    ln = int(a.len, 16)
    seen = set()
    i = 0
    print("[text]                              # game text (data walk; ASCII, $00-terminated)")
    while i < ln:
        b = rom[off0 + i]
        # a string = run of printable ASCII / newline, then a $00 terminator
        if 32 <= b < 127 or b == 0x0A:
            j = i
            while j < ln and (32 <= rom[off0 + j] < 127 or rom[off0 + j] == 0x0A):
                j += 1
            if j < ln and rom[off0 + j] == 0x00 and (j - i) >= a.min:
                text = bytes(rom[off0 + i:off0 + j]).decode("ascii").replace("\n", "\\n")
                addr = vaddr0 + i
                name = slug(text, addr)
                if name in seen:
                    name = "%s_%04X" % (name, addr)
                seen.add(name)
                # store as addr = "name"; keep the literal text in the trailing comment
                print('0x%04X = "%s"   # "%s"' % (addr, name, text[:48]))
                i = j + 1
                continue
        i += 1


if __name__ == "__main__":
    main(sys.argv)
