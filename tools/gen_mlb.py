"""gen_mlb.py - an NES label registry (mesen-labels.toml) -> a Mesen 2 .mlb beside the ROM.

    py -3.14 tools/gen_mlb.py <mesen-labels.toml> --rom <game.nes> [-o out.mlb] [--strict]

Replaces the per-repo exporters (koei-nes gen_mlb_nes, Elite gen_mlb, UW export_mlb, na1
mesen-labels --mlb); see MIGRATION.md row A. Mapper, PRG size and battery come from the ROM
header. Registry defects and labels Mesen would drop are printed; --strict makes them fatal.
"""
import argparse
import sys
from collections import Counter
from pathlib import Path

from emu_harness.backends.mesen.labels import write_mlb
from emu_harness.core.symbols import Symbol, read_registry
from emu_harness.machines.nes.memory import Cart, locate


def nes_symbols(registry, cart):
    """-> (symbols, entries that fall outside the PRG image)."""
    out, lost = [], []
    for e in registry.entries:
        where = locate(cart, e.section, e.addr)
        if where is None:
            lost.append(e)
        else:
            out.append(Symbol(where[0], where[1], e.name, e.comment))
    return out, lost


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("registry")
    ap.add_argument("--rom", required=True)
    ap.add_argument("-o", "--out", help="default: beside the ROM, ROM basename (Mesen auto-loads it)")
    ap.add_argument("--strict", action="store_true", help="exit 1 on any registry defect or dropped label")
    a = ap.parse_args(argv)

    cart = Cart.from_ines(a.rom)
    reg = read_registry(a.registry)
    symbols, lost = nes_symbols(reg, cart)
    out = Path(a.out) if a.out else Path(a.rom).with_suffix(".mlb")
    problems = write_mlb(symbols, out)

    kept = len(symbols) - len(problems)
    print(f"{out}: {kept} labels (mapper {cart.mapper}, PRG {cart.prg_size // 1024} KiB, "
          f"{'battery' if cart.battery else 'no battery'})")
    for region, n in Counter(s.region for s in symbols).most_common():
        print(f"    {region:18} {n}")
    kinds = Counter(d.kind for d in reg.defects)
    if kinds:
        print(f"  registry defects: {dict(kinds)}")
        for d in [d for d in reg.defects if d.kind == "conflict"][:10]:
            print(f"    line {d.line}: CONFLICT {d.detail}")
    for e in lost:
        print(f"  outside PRG: line {e.line} [{e.section}] ${e.addr:04X} {e.name}")
    for p in problems:
        print(f"  dropped: {p}")
    return 1 if a.strict and (reg.defects or problems or lost) else 0


if __name__ == "__main__":
    sys.exit(main())
