"""The documentation gate's first check: does every name-address pair a page quotes match the registry?

A hand-written page goes stale when the walk renames what it quotes. The claim that can be checked
mechanically is the pair: a page that writes `name` ($ABCD) (or `$ABCD name`, `name` `$ABCD`) says that
label sits at that address, and the project's committed Mesen label file (.mlb, which every project
generates from its registry) says whether it does. Each pair is one of:

    ok        the name labels that address
    inside    the address is inside the named routine (from its label to the next one in its bank):
              a call-site or instruction citation, also ok
    moved     the name exists, at other addresses (the page or the registry is wrong)
    renamed   the address is labelled, by other names (the page quotes an old name)
    unknown   neither the name nor the address is labelled (an unlabelled address, or a non-label)

The address side is loose by design: a switchable-bank label's CPU address is its offset in the
16K window ($8000 or $C000 based), and RAM / SRAM labels are their own addresses, so a pair in the
wrong bank still reads ok. Generated pages (the site's copies) are left out by the caller's globs.

    py -3.14 -m emu_harness.build.doccheck <project> [--mlb FILE] [--docs GLOB ...] [--show moved,renamed]
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

NAME = r"[a-z_][a-z0-9_]*[a-z0-9]"
ADDR = r"\$([0-9A-Fa-f]{4})"
# `name` ($ABCD) | `name ($ABCD)` | `name` `$ABCD` | `$ABCD name` | `$ABCD` (`name`) ; name has an underscore
PAIRS = [
    re.compile(rf"`({NAME})`\s*\(\s*`?{ADDR}"),
    re.compile(rf"`({NAME})\s*\(\s*{ADDR}"),
    re.compile(rf"`({NAME})`\s*`{ADDR}`"),
    re.compile(rf"`{ADDR}\s+({NAME})`", ),
    re.compile(rf"`{ADDR}`\s*\(\s*`({NAME})`"),
]
RAM_BASE = {"NesInternalRam": 0x0000, "NesSaveRam": 0x6000, "NesWorkRam": 0x6000}


def load_mlb(path):
    """-> (name -> {cpu addresses}, cpu address -> {names}, name -> [(lo, hi) cpu extents])."""
    by_name, by_addr = defaultdict(set), defaultdict(set)
    prg = []
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split(":", 3)
        if len(parts) < 3 or not parts[2]:
            continue
        kind, off, name = parts[0], int(parts[1].split("-")[0], 16), parts[2]
        if kind == "NesPrgRom":
            addrs = {0x8000 + (off & 0x3FFF), 0xC000 + (off & 0x3FFF)}
            prg.append((off, name))
        elif kind in RAM_BASE:
            addrs = {RAM_BASE[kind] + off}
        else:
            continue
        by_name[name] |= addrs
        for a in addrs:
            by_addr[a].add(name)
    extents = defaultdict(list)
    prg.sort()
    for i, (off, name) in enumerate(prg):
        end = prg[i + 1][0] if i + 1 < len(prg) and prg[i + 1][0] >> 14 == off >> 14 else (off | 0x3FFF) + 1
        for base in (0x8000, 0xC000):
            extents[name].append((base + (off & 0x3FFF), base + ((end - 1) & 0x3FFF) + 1))
    return by_name, by_addr, extents


def pairs_in(text):
    """-> [(line number, name, address)] for every name-address pair the text quotes."""
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        for i, rx in enumerate(PAIRS):
            for m in rx.finditer(line):
                name, addr = (m.group(1), m.group(2)) if i in (0, 1, 2) else (m.group(2), m.group(1))
                if "_" in name:
                    out.append((n, name, int(addr, 16)))
    return out


def classify(name, addr, by_name, by_addr, extents):
    if addr in by_name.get(name, ()):
        return "ok"
    if any(lo <= addr < hi for lo, hi in extents.get(name, ())):
        return "inside"
    if name in by_name:
        return "moved"
    if by_addr.get(addr):
        return "renamed"
    return "unknown"


def check(project, mlb, globs):
    by_name, by_addr, extents = load_mlb(mlb)
    rows = []
    for g in globs:
        for page in sorted(project.glob(g)):
            for n, name, addr in pairs_in(page.read_text(encoding="utf-8", errors="replace")):
                verdict = classify(name, addr, by_name, by_addr, extents)
                rows.append((page.relative_to(project), n, name, addr, verdict,
                             sorted(by_addr.get(addr, ())) if verdict == "renamed" else
                             [f"${x:04X}" for x in sorted(by_name.get(name, ()))] if verdict == "moved" else []))
    return rows


def main(argv=None):
    from emu_harness.build.status import resolve_project
    ap = argparse.ArgumentParser(prog="emu_harness.build.doccheck", description=__doc__.split("\n")[0])
    ap.add_argument("project")
    ap.add_argument("--mlb", type=Path, help="the label file (default: the project's one committed .mlb)")
    ap.add_argument("--docs", nargs="+", default=["**/*.md"], help="page globs, project-relative")
    ap.add_argument("--show", default="moved,renamed", help="verdicts to list (comma-separated, or 'all')")
    a = ap.parse_args(argv)
    project = resolve_project(a.project)
    mlb = a.mlb
    if mlb is None:
        found = [p for p in project.rglob("*.mlb") if "docs" not in p.parts]
        if len(found) != 1:
            sys.exit(f"pass --mlb: found {len(found)} .mlb files in {project}")
        mlb = found[0]
    rows = check(project, mlb, a.docs)
    show = None if a.show == "all" else set(a.show.split(","))
    for page, n, name, addr, verdict, other in rows:
        if show is None or verdict in show:
            print(f"{verdict:8s} {page}:{n}  `{name}` ${addr:04X}" + (f"  -> {', '.join(other[:4])}" if other else ""))
    c = Counter(r[4] for r in rows)
    pages = len({r[0] for r in rows})
    print(f"{len(rows)} name-address pairs on {pages} pages: " + ", ".join(f"{k} {c[k]}" for k in
                                                                        ("ok", "inside", "moved", "renamed", "unknown")))
    return 1 if c["moved"] or c["renamed"] else 0


if __name__ == "__main__":
    sys.exit(main())
