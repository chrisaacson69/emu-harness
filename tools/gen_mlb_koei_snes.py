#!/usr/bin/env python3
"""
Emit a Mesen(2) .mlb label file from the named KOEI SNES symbol registry, so the whole
decompiled program shows up with labels in Mesen's debugger.

    py -3.14 tools/gen_mlb_koei_snes.py --data <title>/data --out labels.mlb root comroot excsub1 [...]
    (koei-snes `tools/gen_mlb.py` runs this; it lived there until MIGRATION.md row F. A label
    exporter imports the Mesen backend, so it is a tool, not part of the VM package.)

The bytecode routines/data/text live in WRAM ($7E/$7F) -> `SnesWorkRam:<offset>:<label>`.
WorkRam offset = (bank-$7E)*$10000 + addr; for $7E:xxxx that is just xxxx.

Overlays SHARE WRAM addresses ($7200 slot: comroot/settei/kisetsu; $C300 slot:
excsub1-6), so Mesen can hold only one set per slot -- list exactly one module per slot
on the command line (default root+comroot+excsub1). Resident root ($2xxx-$6xxx) and the
shared high-WRAM globals/records ($E000+/$F800+) are always emitted (first module wins).
A few native VM entry points (bank $00) are added from the title's games.toml profile
(--game, else $KOEI_SNES_GAME -- required: there is no default profile) as `SnesPrgRom`.
"""
import argparse
import os
import re
import sys
HERE = os.path.dirname(os.path.abspath(__file__))
from emu_harness.machines.snes.mapper import cpu_to_file, LOROM
from emu_harness.backends.mesen.labels import write_mlb   # pip install -e ../emu-harness (py -3.14)
from emu_harness.core.symbols import Symbol
_LINE = re.compile(r'(0x[0-9A-Fa-f]+)\s*=\s*"([^"]+)"(?:\s*#\s*"([^"]*)")?')


def load_labels(mdir):
    """addr -> (label, comment) from routines.toml + strings.toml + enums.py."""
    out = {}
    for fn in ("routines.toml", "strings.toml"):
        p = os.path.join(mdir, fn)
        if not os.path.exists(p):
            continue
        for line in open(p, encoding="utf-8"):
            body = line.split("#", 1)[0]
            m = _LINE.match(line.strip()) or re.match(r'(0x[0-9A-Fa-f]+)\s*=\s*"([^"]+)"', body.strip())
            if m:
                addr = int(m.group(1), 0)
                out[addr] = (m.group(2), (m.group(3) if m.lastindex and m.lastindex >= 3 else "") or "")
    ep = os.path.join(mdir, "enums.py")
    if os.path.exists(ep):
        ns = {}
        exec(compile(open(ep, encoding="utf-8").read(), ep, "exec"), ns)
        for name, base in ns.get("STRING_TABLE_BASE", {}).items():
            out.setdefault(base, ("tbl_" + name, ""))
    return out


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("modules", nargs="+")
    ap.add_argument("--data", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--game", default=None, help="games.toml profile (else $KOEI_SNES_GAME; required)")
    a = ap.parse_args(argv[1:])
    from emu_harness.vm.koei.snes import profile as _profile
    P = _profile.select(a.game)                  # raises when no profile is selected
    merged = {}                                  # addr -> (label, comment, module)
    for mod in a.modules:                        # first module to claim an address wins
        for addr, (label, cmt) in load_labels(os.path.join(a.data, mod)).items():
            if addr not in merged and label != "DRIFT":
                merged[addr] = (label, cmt, mod)
    symbols = []
    for addr in sorted(merged):
        label, cmt, mod = merged[addr]
        note = ("%s" % mod) + ((" | " + cmt) if cmt else "")
        symbols.append(Symbol("snes.work_ram", addr, label, note))   # $7E:xxxx -> WorkRam offset xxxx
    # native VM entry points (bank $00 ROM) from the title's profile, as SnesPrgRom offsets
    native = {"vm_interp_loop": P.get("interpreter_loop"), "vm_run_entry": P.get("run_vm_entry"),
              "syscall_dispatch": P.get("syscall_dispatch"), "resource_loader": P.get("loader")}
    for name, addr in native.items():
        if addr:
            symbols.append(Symbol("snes.prg_rom", cpu_to_file(LOROM, 0, addr), name, "native VM"))
    problems = write_mlb(symbols, a.out)      # emu-harness: Mesen's own export format
    print("wrote %s: %d labels (%d modules: %s)" % (a.out, len(symbols) - len(problems), len(a.modules),
                                                     ", ".join(a.modules)))
    for p in problems:
        print("  dropped:", p)


if __name__ == "__main__":
    from emu_harness.vm.koei.snes import profile as _cli
    sys.exit(_cli.run_cli(main))
