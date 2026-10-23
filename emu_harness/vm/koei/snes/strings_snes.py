#!/usr/bin/env python3
"""
Located-string extractor for the ROTK2 SNES ROM (the SNES analog of the NES
`pure_strings.py`). Every ASCII run is anchored to BOTH its file offset and its
65C816 CPU address, so a hit becomes a coordinate we can disassemble around and
cross-reference against the fully-decompiled NES `rot3k2-decompiler` oracle.

Reuses the shared `snes-decompiler` substrate (mapping detection + offset math)
rather than re-deriving it -- resolved by the sibling convention `../snes-decompiler`.

    py -3 tools/strings_snes.py "<rom>.sfc"                 # all runs >=4
    py -3 tools/strings_snes.py "<rom>.sfc" --grep develop  # filter (case-insensitive)
    py -3 tools/strings_snes.py "<rom>.sfc" --min 6 --limit 80
"""
import argparse
import os
import re
import sys

# Reuse the shared SNES substrate (sibling repo).
_HERE = os.path.dirname(os.path.abspath(__file__))
from emu_harness.machines.snes.mapper import file_to_cpu, strip_copier_header  # noqa: E402
from emu_harness.machines.snes.header import detect                        # noqa: E402

RUN = re.compile(rb"[ -~]{%d,}")


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("rom")
    ap.add_argument("--min", type=int, default=4, help="min run length (default 4)")
    ap.add_argument("--grep", default=None, help="case-insensitive substring filter")
    ap.add_argument("--limit", type=int, default=0, help="max results (0 = all)")
    args = ap.parse_args(argv[1:])

    data = open(args.rom, "rb").read()
    rom, copier = strip_copier_header(data)
    score, f = detect(rom)
    if f is None or score < 4:
        sys.exit("no confident SNES header (score %d)" % score)
    mapping = f["mapping_guess"]
    sys.stderr.write("mapping=%s title=%r%s\n" %
                     (mapping.upper(), f["title"], "  (+512 copier stripped)" if copier else ""))

    pat = re.compile(rb"[ -~]{%d,}" % args.min)
    grep = args.grep.lower() if args.grep else None
    n = 0
    for m in pat.finditer(rom):
        s = m.group().decode()
        if grep and grep not in s.lower():
            continue
        bank, addr = file_to_cpu(mapping, m.start())
        print("%06X  $%02X:%04X  %r" % (m.start(), bank, addr, s))
        n += 1
        if args.limit and n >= args.limit:
            break
    sys.stderr.write("%d run(s) printed\n" % n)


if __name__ == "__main__":
    main(sys.argv)
