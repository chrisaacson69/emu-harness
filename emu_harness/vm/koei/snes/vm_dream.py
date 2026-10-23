#!/usr/bin/env python3
"""
DREAM structuring for KOEI SNES routines: the NES side's pattern-independent structurer (nes/dream.py,
Yakdan et al., "No More Gotos", NDSS 2015 -- reaching conditions, loops, switches, gated), run on
the SNES lifter's blocks. MIGRATION.md row G: one structurer over a host-neutral block graph, in place
of vm_struct's follow-node view (~65-75% goto-less, loops left as gotos).

The adapter: vm_lift.RoutineLift blocks -> (goto-form lines in DREAM's dialect, block graph). It
checks itself before DREAM sees anything: lowering the printed lines (vm_cfg.lower_goto_cfg, the
same lowering DREAM's gate uses) must give back exactly the lifter's graph. DREAM then structures
through its gated entry, so a routine it cannot prove equivalent (or an unhandled shape) comes back
as the honest goto form. Statements, conditions and return values are the lifter's text, verbatim.

    py -3.14 -m emu_harness.vm.koei.snes.vm_dream <rom> --game g --module m --syms <title>/data/m --at 7698
    py -3.14 -m emu_harness.vm.koei.snes.vm_dream <rom> --game g --title <title-repo> --stats
"""
import argparse
import os
import re
import sys

from emu_harness.machines.snes.mapper import strip_copier_header
from emu_harness.vm.koei.nes import dream, vm_cfg
from emu_harness.vm.koei.snes import profile as _profile
from emu_harness.vm.koei.snes import vm_isa as ISA
from emu_harness.vm.koei.snes import vm_lift
from emu_harness.vm.koei.snes import vm_walk as VW
from emu_harness.vm.koei.snes.symbols import Symbols

EXIT = vm_cfg.EXIT
_SW = re.compile(r"^switch \((.+)\) \{ (.*) \}$")
_CASE = re.compile(r"(case (\$[0-9A-Fa-f]+|-?\d+)|default): goto L([0-9A-Fa-f]{4});")


class OutOfScope(Exception):
    """A routine this adapter does not hand to DREAM (it is rendered goto-form instead)."""


def graph_lines(R):
    """-> (lines, cfg, leaders) for one lifted routine, in DREAM's dialect (dream.tagged_cfg_graph)."""
    blocks = sorted(R.blocks, key=lambda B: B.start)
    if not blocks:
        raise OutOfScope("no blocks")
    leaders = [B.start for B in blocks]
    if leaders[0] != R.body:
        raise OutOfScope("entry $%04X is not the lowest block ($%04X)" % (R.body, leaders[0]))
    nxt = {L: (leaders[i + 1] if i + 1 < len(leaders) else EXIT) for i, L in enumerate(leaders)}
    lines, cfg = [], {}
    for B in blocks:
        a = B.start
        lines.append((a, 0, "L_%04X:" % a))
        lines += [(a, 1, st) for st in B.stmts]
        if B.term == "if":
            if B.fall != nxt[a]:
                raise OutOfScope("$%04X: if falls to $%04X, not the next block" % (a, B.fall))
            lines.append((a, 1, "if (%s) goto L_%04X;" % (B.cond, B.taken)))
            cfg[a] = frozenset({B.taken, B.fall})
        elif B.term == "goto":
            lines.append((a, 1, "goto L_%04X;" % B.taken))
            cfg[a] = frozenset({B.taken})
        elif B.term == "return":
            lines.append((a, 1, "return %s;" % B.retval))
            cfg[a] = frozenset({EXIT})
        elif B.term == "switch":
            m = _SW.match(B.switch or "")
            if not m:
                raise OutOfScope("$%04X: switch text %r" % (a, B.switch))
            lines.append((a, 1, "switch (%s) {" % m.group(1)))
            targets = set()
            for lab, val, t in _CASE.findall(m.group(2)):
                key = "default" if lab == "default" else "case %d" % (
                    int(val[1:], 16) if val.startswith("$") else int(val))
                lines.append((a, 2, "%s: goto L_%s;" % (key, t.upper())))
                targets.add(int(t, 16))
            lines.append((a, 1, "}"))
            cfg[a] = frozenset(targets)
        elif B.term == "fall":
            s = B.succs[0] if B.succs else EXIT
            if s != nxt[a]:
                lines.append((a, 1, "goto L_%04X;" % s))
            cfg[a] = frozenset({s})
        else:
            raise OutOfScope("$%04X: terminator %r" % (a, B.term))
    lowered = vm_cfg.lower_goto_cfg(lines, leaders)
    if lowered != cfg:
        bad = [L for L in leaders if lowered.get(L) != cfg.get(L)]
        raise OutOfScope("adapter self-check: lowering differs at %s" % ", ".join("$%04X" % x for x in bad[:4]))
    return lines, cfg, leaders


def structure(R):
    """-> (lines, how): DREAM's gated structured lines, or the goto form. how = 'dream' | 'goto: why'."""
    try:
        lines, cfg, leaders = graph_lines(R)
    except OutOfScope as e:
        return None, "goto: %s" % e
    why = []
    out = dream.dream_structure_gated_graph(lines, cfg, leaders, why)
    return out, ("goto: DREAM %s" % why[0] if out is lines else "dream")


def render(R, name, lines):
    """SNES-style text: the lifter's header, labels as `LXXXX`, previews hoisted as in the .c."""
    params = ", ".join("word arg%d" % k for k in range(1, R.args + 1))
    out = ["/* NOTE: %s */" % n for n in R.notes] + ["word %s(%s) {" % (name, params)]
    for _a, ind, text in lines:
        t = re.sub(r"\bL_([0-9A-Fa-f]{4})\b", r"L\1", text)
        out.append(("  " + t) if t.endswith(":") and ind == 0 else "    " * max(ind, 1) + vm_lift.hoist_previews(t))
    out.append("}")
    return "\n".join(out)


def _gotos(lines):
    return sum(1 for _a, _i, t in lines if re.search(r"\bgoto L_", t))


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("rom")
    ap.add_argument("--game", default=None)
    ap.add_argument("--module", default=None)
    ap.add_argument("--syms", default=None)
    ap.add_argument("--at", default=None, help="hex: one routine")
    ap.add_argument("--title", default=None, help="title repo: with --stats, every module with a data/<m>/routines.toml")
    ap.add_argument("--stats", action="store_true", help="per-module counts instead of the C")
    a = ap.parse_args(argv[1:])
    rom, _ = strip_copier_header(open(a.rom, "rb").read())
    P = _profile.select(a.game, rom)
    isa = ISA.bind(rom, P)
    if a.stats:
        mods = [a.module] if a.module else sorted(
            d for d in os.listdir(os.path.join(a.title, "data"))
            if os.path.isfile(os.path.join(a.title, "data", d, "routines.toml")))
        from emu_harness.vm.koei.snes import vm_struct
        tot = {"routines": 0, "dream": 0, "free_dream": 0, "free_struct": 0, "free_best": 0}
        why = {}
        for m in mods:
            ns = argparse.Namespace(module=m, start=None, base=None)
            try:
                mod = VW.module_from_args(rom, P, ns)
            except Exception as e:
                print("%-10s skipped: %s" % (m, e))
                continue
            space = VW.Space(rom, P, mod)
            S = Symbols(os.path.join(a.title, "data", m), profile=P)
            W = VW.walk_module(isa, space)
            L = vm_lift.Lifter(isa, space, S, W)
            n = d = fd = fs = fb = 0
            for e in W.routines:
                R = L.lift(e, W.walks[e])
                lines, how = structure(R)
                n += 1
                free = how == "dream" and _gotos(lines) == 0
                if how == "dream":
                    d += 1
                    fd += free
                else:
                    why[how] = why.get(how, 0) + 1
                vs = []
                vm_struct.structure(R, "r", out=vs.append)
                sfree = not any(re.search(r"\bgoto L", t) for t in vs)
                fs += sfree
                fb += free or (how != "dream" and sfree)   # the cutover: DREAM when it passes, else vm_struct
            print("%-10s %4d routines: DREAM structured %4d, goto-free: DREAM %4d / vm_struct %4d / cutover %4d"
                  % (m, n, d, fd, fs, fb))
            tot["routines"] += n; tot["dream"] += d; tot["free_dream"] += fd; tot["free_struct"] += fs
            tot["free_best"] += fb
        r = max(1, tot["routines"])
        print("TOTAL %d routines: DREAM structured %d (%.1f%%); goto-free DREAM %d (%.1f%%) vs vm_struct %d (%.1f%%);"
              " cutover (DREAM else vm_struct) %d (%.1f%%)"
              % (tot["routines"], tot["dream"], 100 * tot["dream"] / r, tot["free_dream"], 100 * tot["free_dream"] / r,
                 tot["free_struct"], 100 * tot["free_struct"] / r, tot["free_best"], 100 * tot["free_best"] / r))
        for k, v in sorted(why.items(), key=lambda kv: -kv[1])[:12]:
            print("  %5d  %s" % (v, k))
        return 0
    ns = argparse.Namespace(module=a.module, start=None, base=None)
    mod = VW.module_from_args(rom, P, ns)
    space = VW.Space(rom, P, mod)
    S = Symbols(a.syms, profile=P)
    W = VW.walk_module(isa, space)
    L = vm_lift.Lifter(isa, space, S, W)
    for e in ([int(a.at, 16)] if a.at else W.routines):
        R = L.lift(e, W.walks.get(e))
        lines, how = structure(R)
        if lines is None:
            print("/* %s */" % how)
            print(vm_lift.render(R, S.string(e) or "sub_%04X" % e))
        else:
            print("/* %s */" % how)
            print(render(R, S.string(e) or "sub_%04X" % e, lines))
        print()
    return 0


if __name__ == "__main__":
    sys.exit(_profile.run_cli(main))
