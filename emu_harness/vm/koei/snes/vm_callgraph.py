#!/usr/bin/env python3
"""
Build the KOEI SNES bytecode call-graph of one module and rank routines leaves-first.

The module is walked by vm_walk (shared ISA, every trampoline candidate, routine_sig +
routine_body_offset, module bounds). Call methods (classes from data/vm_opcode_names.toml):
  direct   : call.n $AC (a16), call.p $E9 (a16, then pop s8)     -> callee = the a16 trampoline
  indirect : call.ind $DD, call.ind2 $EA                           -> target = regA (unresolved)
A direct target is classified in the module's address space (itself + resident + coresident
modules): intra / resident / coresident routine, syscall stub (JSL to abi_syscall), other native
stub, or unmapped. Only intra targets are nodes of this module's graph; the rest are external.

Reports: leaves (no outgoing direct call -> name these first, bottom-up), indirect-call
sites, external targets by class, a top-down forest, naming status + frontier (a frontier
routine's every callee that is a routine of this module is already named).

    py -3 tools/vm_callgraph.py <rom> [--game g] --module COMMON [--leaves|--tree|--unnamed|...]
    py -3 tools/vm_callgraph.py <rom> --start 01:8003 --base 7E:1003 [--syms data/COMMON] ...
(--start must lie inside an archive CODE module: archive.py --code lists them; a data blob is
refused with that message. Profile: --game, else $KOEI_SNES_GAME, else the ROM header.)
"""
import argparse
import os
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
from emu_harness.machines.snes.mapper import strip_copier_header  # noqa: E402
from emu_harness.vm.koei.snes import profile as _profile  # noqa: E402
from emu_harness.vm.koei.snes import vm_isa as ISA  # noqa: E402
from emu_harness.vm.koei.snes import vm_walk as VW  # noqa: E402
from emu_harness.passes.callgraph import Graph  # noqa: E402


def module_graph(W, name):
    """One module's walk as a passes.callgraph Graph. Nodes are (module, addr): this module's
    routines, plus each external target at the module vm_walk owned it to (resident / coresident)
    or its class (syscall / native / unmapped); edge kind = vm_walk's class.
    -> (Graph, Counter of external (target, class, module), Counter of indirect calls per routine)"""
    G = Graph(name)
    for e in W.routines:
        G.subs[(name, e)] = None
    external, indirect = Counter(), Counter()
    for site, e, cls, mname, t, op in W.calls:
        if cls == "indirect":
            indirect[e] += 1
            continue
        if not (cls == "intra" and (name, t) in G.subs):
            external[(t, cls, mname)] += 1             # kept as an edge (a non-node callee)
        G.add((name, e), (mname or cls, t), cls)
    return G, external, indirect


def title_graph(rom, P, modules, strict=False):
    """Every listed module's walk in one Graph: a call into a resident / coresident module lands on
    that module's routine node, so `bank_order` orders MODULES leaves-first (the SNES overlay
    analogue of the NES bank order). Modules that are not bytecode are skipped, with the reason."""
    isa = ISA.bind(rom, P)
    G, skipped = Graph("snes"), {}
    for m in modules:
        try:
            mod = VW.module_from_args(rom, P, argparse.Namespace(module=m, start=None, base=None))
        except KeyError as e:
            skipped[m] = str(e)
            continue
        Wm = VW.walk_module(isa, VW.Space(rom, P, mod))
        if strict and (Wm.conflicts or Wm.dead_hits or Wm.decode_errors):
            raise SystemExit("walk of %s is not clean (%d conflicts, %d dead, %d errors) -- fix the tools first"
                             % (mod.name, len(Wm.conflicts), len(Wm.dead_hits), len(Wm.decode_errors)))
        Gm, _ext, _ind = module_graph(Wm, mod.name)
        G.subs.update(Gm.subs)
        for (s, d), ks in Gm.edges.items():
            for k in ks:
                G.add(s, d, k)
    return G, skipped


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("rom")
    ap.add_argument("--game", default=None, help="games.toml profile (else $KOEI_SNES_GAME, else the ROM header)")
    ap.add_argument("--module", default=None, help="archive name of the module")
    ap.add_argument("--start", default=None, help="legacy: ROM BK:AD inside the module")
    ap.add_argument("--base", default=None, help="legacy: WRAM 7E:XXXX of --start (checked)")
    ap.add_argument("--scan", type=lambda s: int(s, 16), default=None,
                    help="legacy: module end; the archive length is authoritative (checked)")
    ap.add_argument("--leaves", action="store_true"); ap.add_argument("--syms", default=None)
    ap.add_argument("--dump-leaves", dest="dump_leaves", action="store_true")
    ap.add_argument("--unnamed", action="store_true", help="report named/unnamed + naming frontier")
    ap.add_argument("--dump-frontier", dest="dump_frontier", action="store_true", help="dump frontier routine bodies")
    ap.add_argument("--tree", action="store_true", help="print top-down call forest (for the walk-down)")
    ap.add_argument("--title", default=None,
                    help="title repo: every data/<module>/ as ONE graph -> cross-module edges + modules leaves-first")
    a = ap.parse_args(argv[1:])
    if a.title:
        from emu_harness.passes.callgraph import bank_order
        rom, _ = strip_copier_header(open(a.rom, "rb").read())
        P = _profile.select(a.game, rom)
        mods = sorted(d for d in os.listdir(os.path.join(a.title, "data"))
                      if os.path.isfile(os.path.join(a.title, "data", d, "routines.toml")))
        G, skipped = title_graph(rom, P, mods)
        known = {m for m, _a in G.subs}
        print("== title call graph: %d modules, %d routines, %d edges ==" % (len(known), len(G.subs), len(G.edges)))
        for m, why in sorted(skipped.items()):
            print("  skipped %s: %s" % (m, why))
        print("\ncross-module edges (caller -> callee module: kinds)")
        for (s, d), ks in sorted(G.bank_edges.items(), key=lambda kv: (str(kv[0][0]), str(kv[0][1]))):
            if d in known:
                print("  %-10s -> %-10s %s" % (s, d, ",".join(sorted(ks))))
        print("\nmodules, leaves first: " + " -> ".join(bank_order(G)))
        return 0
    if not (a.module or a.start):
        ap.error("give --module NAME (or the legacy --start/--base)")
    rom, _ = strip_copier_header(open(a.rom, "rb").read())
    P = _profile.select(a.game, rom)
    isa = ISA.bind(rom, P)
    mod = VW.module_from_args(rom, P, a)
    if a.scan is not None and a.scan != mod.end:
        print("note: --scan $%04X ignored; module %s ends at $%04X (archive length)" % (a.scan, mod.name, mod.end),
              file=sys.stderr)
    space = VW.Space(rom, P, mod)
    W = VW.walk_module(isa, space)
    routines = {e: W.walks[e] for e in W.routines}
    rset = set(routines)

    G, external, indirect = module_graph(W, mod.name)
    calls = {e: {d[1] for d, _ks in G.callees((mod.name, e))} for e in routines}

    def is_node(t):
        return t in rset

    def compact_body(e, S):
        parts = []
        for v in sorted(routines[e].insns):
            ins = routines[e].insns[v]
            if ins.error:
                parts.append(isa.text(ins)); continue
            m = isa.text(ins).split()[0]
            if ins.call_target is not None:
                t = ins.call_target
                m = ">" + (S.string(t) or "$%04X" % t)
            elif ins.cf == "call":
                m = ">*regA"
            elif ins.cf in ISA.LOCAL_BRANCH:
                m += " " + ",".join("$%04X" % t for t in ins.targets)
            elif ins.fields:
                r, typ, val = ins.fields[0]
                if typ in ("a16", "i16"):
                    lbl = S.string(val & 0xFFFF)
                    m += "=" + (lbl if lbl else "$%04X" % (val & 0xFFFF))
                elif typ in ISA.FRAME_TYPES:
                    m += " " + ISA.frame_var(ISA.frame_offset(val))
                else:
                    m += " $%X" % (val & (0xFF if ISA.TYPE_SIZE[typ] == 1 else 0xFFFFFFFF))
            parts.append(m)
        return "  ".join(parts)

    if a.tree or a.unnamed or a.dump_frontier or a.dump_leaves:
        from emu_harness.vm.koei.snes.symbols import Symbols
        S = Symbols(a.syms, profile=P)
    if a.tree:
        tops = [n[1] for n in G.tops()]
        nm = lambda e: S.string(e) or "$%04X" % e
        seen = set()

        def pr(e, depth):
            tag = nm(e)
            if not is_node(e):
                print("  " * depth + "> " + tag + " [ext]"); return
            if e in seen:
                print("  " * depth + "> " + tag + " ..."); return
            seen.add(e)
            print("  " * depth + ("" if depth == 0 else "> ") + tag)
            for c in sorted(calls[e]):
                pr(c, depth + 1)
        print("== top-down call forest (%d top-level entries) ==" % len(tops))
        for t in tops:
            pr(t, 0)
        return 0
    if a.unnamed or a.dump_frontier:
        named = [e for e in routines if S.string(e)]
        unn = [e for e in routines if not S.string(e)]
        # naming frontier: unnamed routines whose every callee THAT IS A ROUTINE is named.
        # A callee that is not a routine of this module (native stub, resident/coresident
        # library, unmapped) must not block the frontier -- only real routines here do.
        frontier = [n[1] for n in G.frontier(lambda n: bool(S.string(n[1])))]
        if a.dump_frontier:
            print("== frontier bodies (%d; >name = call to named routine) ==" % len(frontier))
            for e in sorted(frontier):
                print("$%04X: %s" % (e, compact_body(e, S)))
            return 0
        print("== naming status (module %s @ $7E:%04X) ==" % (mod.name, mod.base))
        print("routines: %d   named: %d   unnamed: %d" % (len(routines), len(named), len(unn)))
        print("naming frontier (unnamed, all callees named): %d" % len(frontier))
        print("\nfrontier (name these next; size, #calls):")
        for e in sorted(frontier)[:40]:
            r = routines[e]
            print("  $%04X  (%d insns, %d calls%s)" % (e, len(r.insns), len(calls[e]),
                  ", indirect" if indirect[e] else ""))
        return 0
    leaves = [e for e in routines if not calls[e]]          # no direct call at all (as before)
    with_ind = [e for e in routines if indirect[e]]
    print("== bytecode call-graph: module %s ($7E:%04X-$%04X) ==" % (mod.name, mod.base, mod.end - 1))
    print("routines discovered  : %d   (trampoline candidates %d, rejected %d)" % (
        len(routines), len(W.candidates), len(W.rejected)))
    print("leaves (no direct call): %d   <- name these first (bottom-up)" % len(leaves))
    print("routines w/ indirect : %d   (call.ind targets = regA, unresolved)" % len(with_ind))
    ext_by = Counter()
    for (t, cls, mname), n in external.items():
        ext_by[cls] += 1
    print("external call targets: %d   (%s)" % (len(external), ", ".join(
        "%s %d" % (k, v) for k, v in sorted(ext_by.items())) or "none"))
    edges = sum(1 for cs in calls.values() for c in cs if is_node(c))
    print("direct call edges    : %d (intra)" % edges)
    if a.leaves:
        print("\nleaf routines (entry: size):")
        for e in sorted(leaves):
            print("  $7E:%04X  (%d insns, %s)" % (e, len(routines[e].insns),
                  "indirect" if indirect[e] else "pure leaf"))
    if a.dump_leaves:
        print("\n== leaf bodies (compact; %d leaves) ==" % len(leaves))
        for e in sorted(leaves):
            print("$%04X: %s" % (e, compact_body(e, S)))
    return 0


if __name__ == "__main__":
    from emu_harness.vm.koei.snes import profile as _cli
    sys.exit(_cli.run_cli(main))
