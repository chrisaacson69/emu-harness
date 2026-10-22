"""repair_registry.py - make a bank-sectioned label registry (mesen-labels.toml) valid TOML,
changing no label.

    py -3.14 tools/repair_registry.py <mesen-labels.toml> [--write]

Only mechanical repairs, each one provably content-preserving:
  - an entry tomllib rejects (unescaped quote or backslash) is rewritten with correct escaping,
    keeping the name and comment exactly as the shared reader reads them;
  - an exact duplicate (same section, address, name AND comment) is removed;
  - a section declared twice has its later block moved to the end of the first, the later
    header's own comment kept as a comment line.
Conflicting names, and same-name duplicates whose comments differ, need a person: they are
listed and left alone. Without --write, prints what it would do. Verify with tools/mlb_compare.py
on the .mlb generated before and after, AND with every other reader of the file: the koei-nes
decompiler resolves collisions by file order, so a section merge can change a decompiled name
(UW bank 8 $A003, MIGRATION.md row A2) -- use --keep-sections there.
"""
import argparse
import re
import sys
import tomllib
from pathlib import Path

from emu_harness.core.symbols import KEY_RE, SECTION_RE, format_entry, read_registry


def repair(path, merge_sections=True):
    text = Path(path).read_text(encoding="utf-8")
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()
    by_line = {e.line: e for e in read_registry(path).entries}
    seen, report = {}, {"rewritten": 0, "exact-duplicate": 0, "merged-section": 0, "needs-decision": []}

    # Pass 1, line by line: rewrite invalid entries, drop exact duplicates.
    out, section = [], None
    for n, raw in enumerate(lines, 1):
        s = raw.strip()
        m = SECTION_RE.match(s)
        if m:
            section = m.group(1).strip()
        k = KEY_RE.match(s) if not m else None
        if k and section is not None:
            e = by_line.get(n)
            if e is None:                                    # a later entry at a taken address
                first = seen[(section, int(k.group(1), 16))]
                try:
                    v = tomllib.loads("v = " + k.group(2))["v"]
                    same = (v["name"], v.get("comment", "")) == (first.name, first.comment)
                except (tomllib.TOMLDecodeError, KeyError, TypeError):
                    same = False
                if same:
                    report["exact-duplicate"] += 1
                    continue
                report["needs-decision"].append((n, section, first, s))
            else:
                seen[(section, e.addr)] = e
                try:
                    tomllib.loads("v = " + k.group(2))
                except tomllib.TOMLDecodeError:
                    indent = raw[: len(raw) - len(raw.lstrip())]
                    raw = indent + format_entry("0x" + k.group(1), e.name, e.comment)
                    report["rewritten"] += 1
        out.append(raw)

    if not merge_sections:
        return newline.join(out) + (newline if text.endswith(("\n", "\r\n")) else ""), report

    # Pass 2: merge repeated sections into their first occurrence.
    blocks, order = {}, []                                   # name -> list of lines; header order
    preamble, cur = [], None
    for raw in out:
        m = SECTION_RE.match(raw.strip())
        if m:
            name = m.group(1).strip()
            if name in blocks:
                report["merged-section"] += 1
                note = raw.strip()[len(f"[{name}]"):].strip()
                cur = blocks[name]
                if note:
                    cur.append(note)                         # keep the later header's comment
            else:
                blocks[name] = [raw]
                order.append(name)
                cur = blocks[name]
            continue
        (preamble if cur is None else cur).append(raw)
    merged = preamble + [line for name in order for line in blocks[name]]
    return newline.join(merged) + (newline if text.endswith(("\n", "\r\n")) else ""), report


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("registry")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--keep-sections", action="store_true",
                    help="do not merge repeated sections (moving entries changes file order, and a reader "
                         "that resolves collisions by order, e.g. koei-nes bank_scoped_labels, can flip a name)")
    a = ap.parse_args(argv)
    fixed, rep = repair(a.registry, merge_sections=not a.keep_sections)
    try:
        tomllib.loads(fixed)
        valid = "valid TOML"
    except tomllib.TOMLDecodeError as e:
        valid = f"still invalid: {str(e)[:80]}"
    print(f"{a.registry}: rewritten {rep['rewritten']}, exact duplicates removed {rep['exact-duplicate']}, "
          f"sections merged {rep['merged-section']}; {valid}")
    for n, sec, first, line in rep["needs-decision"]:
        print(f"  DECIDE line {n} [{sec}] ${first.addr:04X}: kept {first.name!r} (line {first.line}) vs {line[:110]}")
    if a.write:
        Path(a.registry).write_bytes(fixed.encode("utf-8"))
        print("  written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
