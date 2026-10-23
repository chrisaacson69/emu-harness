#!/usr/bin/env python3
"""
DREAM-style control-flow structuring: lift a KOEI SNES bytecode routine to goto-less
structured pseudo-C. The bytecode was compiled from structured C, so its CFG is reducible
-- we recover if / if-else / while from dominators + follow nodes instead of pattern
matching, and only fall back to `goto` for genuinely irreducible edges / switches.

The blocks and their statements come from vm_lift.Lifter (the walker's CFG, liveness-aware
call results, join variables, C-order arguments, (word) / signedness casts, syscall ids by
dataflow; decoding and per-op C semantics from the ONE shared table via vm_isa). This tool only
arranges those blocks; string previews are hoisted to a trailing comment as in the .c.

    py -3 tools/vm_struct.py <rom> [--game g] --module comroot --at 7698 --syms data/comroot
    (legacy --start/--base select the module too)

Pipeline: vm_lift blocks -> dominators -> recursive region structuring (sequence / if /
if-else); a switch is printed as its goto-form statement and ends the region.
"""
import argparse
import os
import re
import sys
HERE = os.path.dirname(os.path.abspath(__file__))
from emu_harness.machines.snes.mapper import strip_copier_header  # noqa: E402
from emu_harness.vm.koei.snes import profile as _profile  # noqa: E402
from emu_harness.vm.koei.snes import vm_isa as ISA  # noqa: E402
from emu_harness.vm.koei.snes import vm_walk as VW  # noqa: E402
from emu_harness.vm.koei.snes import vm_lift  # noqa: E402
from emu_harness.vm.koei.snes.symbols import Symbols  # noqa: E402


def structure(R, name, out=print):
    """Print one lifted routine (vm_lift.RoutineLift) as structured pseudo-C. A block that is
    re-entered by a `goto` gets its label printed where its code was first emitted."""
    lines = []                                         # (block start or None, text)
    emit_line = out
    out = lambda text, _b=None: lines.append((_cur[0], text))   # noqa: E731
    _cur = [None]
    blocks = {B.start: B for B in R.blocks}
    if not blocks:
        out("word %s() { /* nothing decoded */ }" % name)
        return
    entry = R.body

    def succs(L):
        B = blocks[L]
        if B.term in ("return", "switch", "error"):
            return []
        if B.term == "if":
            return [B.fall, B.taken]                     # fall-through first (as before)
        return [s for s in B.succs[:1]]

    printed = set()

    def follow_of(L):
        # immediate post-dominator approximation: the block both branches reconverge to
        s = succs(L)
        if len(s) == 2:
            def chain(x):
                seen = []
                for _ in range(60):
                    seen.append(x)
                    if len(succs(x)) != 1:
                        break
                    x = succs(x)[0]
                return seen
            ca, cb = chain(s[0]), set(chain(s[1]))
            for x in ca:
                if x in cb:
                    return x
        return None

    def emit(L, stop, ind):
        pad = "    " * ind
        while L is not None and L != stop:
            if L in printed:
                out(pad + "goto L%04X;" % L)
                return
            printed.add(L)
            B = blocks[L]
            _cur[0] = L
            lines.append((("label", L), None))
            for st in B.stmts:
                out(pad + st)
            if B.term == "return":
                out(pad + "return %s;" % B.retval)
                return
            if B.term == "switch":
                out(pad + B.switch)
                return
            if B.term == "error":
                return
            s = succs(L)
            if len(s) == 2:
                foll = follow_of(L)
                fall, taken = s
                out(pad + "if (%s) {" % B.cond)
                emit(taken, foll, ind + 1)
                if fall != foll:
                    out(pad + "} else {")
                    emit(fall, foll, ind + 1)
                out(pad + "}")
                L = foll
            elif len(s) == 1:
                L = s[0]
            else:
                return

    params = ", ".join("word arg%d" % k for k in range(1, R.args + 1))
    for n in R.notes:
        out("/* NOTE: %s */" % n)
    out("word %s(%s) {" % (name, params))
    emit(entry, None, 1)
    for L in sorted(blocks):                           # blocks only a goto out of a region reaches
        if L not in printed:
            emit(L, None, 1)
    out("}")
    used = set()
    for _b, text in lines:
        if text:
            used.update(int(x, 16) for x in re.findall(r"goto L([0-9A-F]{4});", text))
    for key, text in lines:
        if text is None:
            if key[1] in used:
                emit_line("  L%04X:" % key[1])
        else:
            emit_line(vm_lift.hoist_previews(text))


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("rom")
    ap.add_argument("--game", default=None, help="games.toml profile (else $KOEI_SNES_GAME, else the ROM header)")
    ap.add_argument("--module", default=None, help="archive name (instead of --start/--base)")
    ap.add_argument("--start", default=None)
    ap.add_argument("--base", default=None)
    ap.add_argument("--syms", required=True)
    ap.add_argument("--at", required=True)
    a = ap.parse_args(argv[1:])
    if not (a.module or a.start):
        ap.error("give --module NAME or --start/--base")
    rom, _ = strip_copier_header(open(a.rom, "rb").read())
    P = _profile.select(a.game, rom)
    isa = ISA.bind(rom, P)
    mod = VW.module_from_args(rom, P, a)
    space = VW.Space(rom, P, mod)
    S = Symbols(a.syms, profile=P)
    entry = int(a.at, 16)
    W = VW.walk_module(isa, space)
    R = vm_lift.Lifter(isa, space, S, W).lift(entry, W.walks.get(entry))
    structure(R, S.string(entry) or "sub_%04X" % entry)
    return 0


if __name__ == "__main__":
    from emu_harness.vm.koei.snes import profile as _cli
    sys.exit(_cli.run_cli(main))
