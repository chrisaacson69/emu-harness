#!/usr/bin/env python3
"""
The call graph of a KOEI NES title's bytecode, across banks (row H of MIGRATION.md).

Who calls whom is what orders a bottom-up walk: name the leaves first, and every caller is read
with its callees' names already in place. Nodes are (bank, addr): bytecode subs at their stub, and
the natives they call at their entry. Edges come from every way bytecode reaches code:

  call       a decoded CALL (the ISA's cf="call" ops with an address operand). The owner of the
             target is the bank the window model maps there: the caller's own bank, a fixed or
             semi-fixed bank, or a co-resident bank -- the one bank whose window covers the address
             AND has a sub stub exactly there (GemFire's exact-entry rule), ties broken by the
             profile's coresident_<bank> list. Anything else is reported, not guessed.
  bankcall   the cross-bank trampoline (`bankcall_fn`): bank = bankval & 31, at addr
  bankrun    a syscall that runs a bank; dispatch: a mode table that does (profile [<title>.callgraph])
  data       a native that copies data out of a bank (dataload): a data edge, kept apart
  jsr / jmp  the fixed bank's native routines (native_floor's enumeration, walked by the row-B
             descent): their JSR targets and tail jumps to other routines

Roots -- entries reached from outside any routine -- are the hardware vectors, the VM handler /
ext-op / syscall tables (native_floor's seed groups), and any profile `tables` (NA1's command-driver
table at bank 1 $B9B2). A routine with no caller and no root is a true orphan.

The trampoline / syscall arguments are constants the decompiler's stack model already resolved:
each is read from the raw C line at the call's own address, and only for a call whose DECODED
target is the named native's address. The names in the C are never what decides an edge.

    py -3.14 -m emu_harness.vm.koei.nes.callgraph <title> [--banks] [--indegree] [--bank-order]
        [--order] [--unresolved] [--callers ADDR] [--calls ADDR] [--orphans]
"""
import argparse
import contextlib
import io
import re
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

from emu_harness.core.symbols import names_by_address, read_registry
from emu_harness.cpu.m6502.descent import Window, descend
from emu_harness.passes.callgraph import Graph, bank_order, layers, leaves_first
from emu_harness.vm.koei.isa import TABLE
from emu_harness.vm.koei.nes import titles

CALL_OPS = {code: o for code, o in TABLE.primary.items()
            if o.cf == "call" and any(f.type == "a16" for f in o.fields)}


def _split_args(s):
    out, depth, start = [], 0, 0
    for i, c in enumerate(s):
        depth += (c == "(") - (c == ")")
        if c == "," and depth == 0:
            out.append(s[start:i].strip()); start = i + 1
    out.append(s[start:].strip())
    return out


def call_args(text, fn):
    """The argument list of the first `fn(...)` call in a C line, or None."""
    j = text.find(fn + "(")
    if j < 0 or (j and (text[j - 1].isalnum() or text[j - 1] == "_")):
        return None
    k, depth = j + len(fn) + 1, 1
    start = k
    while k < len(text) and depth:
        depth += (text[k] == "(") - (text[k] == ")")
        k += 1
    return _split_args(text[start:k - 1]) if depth == 0 else None


def const(tok):
    try:
        return int(tok, 0)
    except (TypeError, ValueError):
        return None


def build(title, decompile=True, native=True):
    cfg = titles.game_cfg(title)
    win = titles.windows(cfg)
    size = cfg.get("bank_size", 0x4000)
    # every bank that can hold bytecode: the profile's code banks, the semi-fixed ones, and the
    # cart's fixed banks (bk's bank 31 carries bytecode subs such as rand_mod that code_banks omits)
    banks = list(dict.fromkeys(cfg.get("code_banks", []) + cfg.get("banks_at_c000", []) + cfg.get("banks_at_e000", [])
                               + sorted(win.fixed())))
    pinned = {b for b, _base in win.fixed().items()} | set(cfg.get("banks_at_c000", [])) | set(cfg.get("banks_at_e000", []))
    views, stubs = {}, {}
    for b in banks:
        vm, VD, _c, base, sz, st = titles.bank_view(title, b)
        if not st:
            continue
        views[b] = (vm, VD, base, sz, st)
        stubs[b] = set(st)
    G = Graph(title)
    names = {}
    for b in views:
        labs = titles.bank_scoped_labels(cfg, b)
        names[b] = {a: n for a, (n, _c) in labs.items()}
        for s in stubs[b]:
            G.subs[(b, s)] = names[b].get(s) or names[b].get(s + 5)

    covers = lambda c, addr: win.base(c) <= addr < win.base(c) + size
    every = sorted(set(stubs) | set(win.fixed()) | pinned)

    def owner(src_bank, addr):
        """-> ((bank, addr) | None, why). A bytecode target is (bank, stub); a native one (bank, entry)."""
        if covers(src_bank, addr):
            return (src_bank, addr), None               # the caller's own window: only it is mapped there
        for c in every:
            if c in pinned or c in win.fixed():
                if covers(c, addr):
                    return (c, addr), None              # a fixed / semi-fixed bank: always mapped
        cands = [c for c in stubs if addr in stubs[c] and covers(c, addr)]   # exact sub entry
        co = cfg.get(f"coresident_{src_bank}", [])
        if len(cands) > 1:
            pref = [c for c in cands if c in co]
            cands = pref if len(pref) == 1 else cands
        if len(cands) == 1:
            return (cands[0], addr), None
        if len(cands) > 1:
            return None, "ambiguous: banks %s" % sorted(cands)
        native = [c for c in co if covers(c, addr)]     # no stub anywhere: a native in a co-resident bank
        if len(native) == 1:
            return (native[0], addr), None
        return None, "no stub there and no single co-resident bank covers it"

    # the natives that cross banks, by ADDRESS (from the registry), with their argument conventions
    reg = defaultdict(set)
    for e in read_registry(cfg["_labels"]).entries:
        reg[e.name].add(e.addr)
    cg = cfg.get("callgraph", {})
    crossers = {}                                       # native addr -> (kind, fn, spec)
    for a in reg.get(cfg.get("bankcall_fn", "call_in_bank_dcd1"), ()):
        crossers[a] = ("bankcall", cfg.get("bankcall_fn", "call_in_bank_dcd1"), {})
    for kind in ("bankrun", "dataload", "dispatch"):
        spec = cg.get(kind)
        for a in reg.get(spec["fn"], ()) if spec else ():
            crossers[a] = (kind, spec["fn"], spec)

    for b, (vm, VD, base, sz, st) in views.items():
        bounds = st + [base + sz]
        raw_lines = _raw_c(title, b, st) if decompile and crossers else {}
        for i, s in enumerate(st):
            src = (b, s)
            instrs, _pc, _t = VD.walk_sub(vm.mem, s + 5, bounds[i + 1], names[b])
            lines = raw_lines.get(s)
            if lines is None and decompile and crossers:
                G.failed.append(src)
            calls = [(ins["addr"], ins["bytes"][1] | ins["bytes"][2] << 8) for ins in instrs if ins["op"] in CALL_OPS]
            for k, (pc, tgt) in enumerate(calls):
                dst, why = owner(b, tgt)
                if dst is None:
                    G.unresolved.append((src, tgt, why))
                else:
                    G.add(src, dst, "call")
                if tgt in crossers and lines:
                    # The C line of a call sits where the decompiler flushes its statement: at the call,
                    # or at the store of its result a few bytes on. Take the first line from the call up
                    # to the next call of the same native that contains it.
                    nxt = next((p for p, t in calls[k + 1:] if t == tgt), 1 << 17)
                    fn = crossers[tgt][1]
                    text = next((t for a, t in sorted(lines.items()) if pc <= a < nxt and fn + "(" in t), "")
                    _crossing(G, src, crossers[tgt], text, owner)
    if native:
        _native(G, title, cfg, owner, pinned, covers)
    for tb in cfg.get("callgraph", {}).get("tables", []):
        vm = views[tb["bank"]][0]
        for k in range(tb["count"]):
            a = vm.mem.read(tb["addr"] + 2 * k) | vm.mem.read(tb["addr"] + 2 * k + 1) << 8
            G.roots[(tb["bank"], a)].add("table:" + tb["kind"])
    return G


def _native(G, title, cfg, owner, pinned, covers):
    """The fixed bank's native routines as nodes, their JSR / tail-JMP edges, and the roots."""
    from emu_harness.vm.koei.nes import native_floor as NF
    t = NF.Title(title)
    subs, _census = NF.native_subs(t)
    home = lambda a: next((c for c in sorted(pinned) if covers(c, a)), None)
    names = names_by_address(read_registry(cfg["_labels"]))
    # native_floor seeds from the native side; a native that only bytecode CALLs (LEmp hand-lists
    # them as extra_seeds) is reached here from the bytecode edges already in the graph
    from_bytecode = {d[1] for (s, d), ks in G.edges.items()
                     if "call" in ks and d not in G.subs and d[0] in pinned and t.in_bank(d[1]) and not t.is_stub(d[1])}
    d = descend(Window(t.buf, NF.BASE), set(subs) | from_bytecode, follow=lambda a: not t.is_stub(a))
    entries = set(subs) | from_bytecode | set(d.entries)
    subs = sorted(a for a in entries if t.in_bank(a) and not t.is_stub(a))
    for e in subs:
        b = home(e)
        if b is None:
            continue
        G.subs.setdefault((b, e), names.get(e))
        f = d.flows.get(e)
        for tgt in (f.calls.values() if f else ()):
            dst, why = owner(b, tgt)
            if dst is None:
                G.unresolved.append(((b, e), tgt, why))
            else:
                G.add((b, e), dst, "jsr")
        for tgt in (f.jumps.values() if f else ()):
            if tgt in entries and tgt != e:
                G.add((b, e), (home(tgt), tgt), "jmp")
    for group, addrs in NF.seed_groups(t).items():
        for a in addrs:
            b = home(a)
            if b is not None:
                G.roots[(b, a)].add(group)


def _crossing(G, src, crosser, text, owner):
    kind, fn, spec = crosser
    args = call_args(text, fn)
    if args is None:
        G.dynamic[kind + " (in code the decompiler does not render)"] += 1
        return
    if kind == "bankcall":
        bank, addr = (const(args[1]) if len(args) > 1 else None), (const(args[2]) if len(args) > 2 else None)
        if bank is None:
            G.dynamic[kind] += 1
            return
        bank &= 31
        if addr is not None:
            G.add(src, (bank, addr), kind)              # a sub there, or native code (a bank with no stubs)
        else:
            G.bank_edges[(src[0], bank)].add(kind)      # the bank is known, the entry is computed
    elif kind == "bankrun":
        if const(args[0]) != spec["id"]:
            return
        bank = const(args[spec["bank_arg"]]) if len(args) > spec["bank_arg"] else None
        if bank is None:
            G.dynamic[kind] += 1
            return
        G.bank_edges[(src[0], bank & 31)].add(kind)
    elif kind == "dataload":
        bank = const(args[spec["bank_arg"]]) if len(args) > spec["bank_arg"] else None
        if bank is None:
            G.dynamic[kind] += 1
            return
        G.data[src].add(bank & 31)
    elif kind == "dispatch":
        n = const(args[0]) if args else None
        if n is None or not 0 <= n < len(spec["banks"]):
            G.dynamic[kind] += 1
            return
        G.bank_edges[(src[0], spec["banks"][n])].add(kind)


def _raw_c(title, bank, stubs):
    """{stub: {line addr: raw C text}} from the decompiler's unstructured output (its `collect` hook)."""
    VC, _cfg, labs, _sysmap = titles.decompiler_for(title, bank, raw=True)
    out = {}
    with tempfile.TemporaryDirectory() as d:
        asm = Path(d) / "bank.asm"
        with contextlib.redirect_stdout(io.StringIO()):
            titles.disasm(title, bank, str(asm))
        for s in stubs:
            col = {}
            with contextlib.redirect_stdout(io.StringIO()):
                try:
                    VC.decompile(str(asm), s, labels=labs, collect=col)
                except Exception:
                    continue
            lines = {}
            for a, _ind, t in col.get("raw", []):
                if a:
                    lines[a] = (lines[a] + " " + t) if a in lines else t
            out[s] = lines
    return out


# ---- reports --------------------------------------------------------------------------------
def walk_report(G, bank, summary=False, one_layer=None):
    """The bottom-up walk of one bank (row I of MIGRATION.md; bk walk_order, ro3k bc_layers, UW
    walk_bank): its routines leaves-first, each with its layer (0 = calls nothing in this bank),
    fan-in by call sites, in-bank callees and calls out by bank -- and first, the CO-RESIDENT GAP:
    every call out of this bank whose owner the graph could not pin down (two banks have a routine
    there, or the bank is switched at run time). Those are the calls a walk would misread."""
    from emu_harness.vm.koei.nes.match import is_named
    nodes = sorted(n for n in G.subs if n[0] == bank)
    if not nodes:
        print("bank %d: no routines" % bank)
        return
    lay = layers(G, nodes)
    order = leaves_first(G, nodes)
    fan = Counter()
    for (s, d), n in G.sites.items():
        fan[d] += n
    named = sum(1 for n in nodes if is_named(G.subs[n]))
    nl = 1 + max(lay.values())
    print("bank %d: %d routines (%d named, %d not), %d layers" % (bank, len(nodes), named, len(nodes) - named, nl))
    gap = [(s, t, why) for s, t, why in G.unresolved if s[0] == bank]
    print()
    print("co-resident gap: %d call(s) out of this bank with no single owner" % len(gap))
    for s, t, why in sorted(gap)[:40]:
        print("  $%04X -> $%04X   %s" % (s[1], t, why))
    if summary or one_layer is not None:
        print()
        for k in range(nl):
            members = [n for n in order if lay[n] == k]
            if one_layer is None:
                print("  layer %2d: %3d routines" % (k, len(members)))
            elif k == one_layer:
                print("layer %d: " % k + " ".join("$%04X" % n[1] for n in members))
        return
    print()
    print("%4s %5s %4s  %-6s %-34s %s" % ("#", "layer", "in", "addr", "name", "calls (this bank | other banks)"))
    for i, n in enumerate(order, 1):
        here, out = [], Counter()
        for d, _ks in G.callees(n):
            if d == n:
                continue
            if d[0] == bank and d in G.subs:
                here.append("$%04X" % d[1])
            elif d[0] != bank:
                out[d[0]] += G.sites[(n, d)]
        tail = " ".join(sorted(here)) or "-"
        if out:
            tail += " | " + " ".join("b%d x%d" % kv for kv in sorted(out.items()))
        print("%4d %5d %4d  $%04X %-34s %s" % (i, lay[n], fan[n], n[1], (G.subs[n] or "-")[:34], tail))


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("title")
    ap.add_argument("--banks", action="store_true", help="the bank-level graph (the default view)")
    ap.add_argument("--indegree", action="store_true", help="per callee bank: how many banks call it")
    ap.add_argument("--bank-order", action="store_true", help="code banks, leaves first")
    ap.add_argument("--order", action="store_true", help="the leaves-first sub order")
    ap.add_argument("--unresolved", action="store_true", help="calls whose target owner is unknown")
    ap.add_argument("--callers", metavar="ADDR", help="every edge into ADDR (hex; [bank:]addr)")
    ap.add_argument("--calls", metavar="ADDR", help="every edge out of ADDR")
    ap.add_argument("--orphans", action="store_true", help="routines with no caller and no root")
    ap.add_argument("--bank", type=int, default=None, help="with --orphans: only this bank")
    ap.add_argument("--walk", type=int, default=None, metavar="BANK",
                    help="one bank's walk order: routines leaves-first with layer, fan-in, callees, and the co-resident gap")
    ap.add_argument("--layers", action="store_true", help="with --walk: the per-layer summary only")
    ap.add_argument("--list", type=int, default=None, metavar="LAYER", help="with --walk: the routines of one layer")
    a = ap.parse_args(argv[1:])
    G = build(a.title)
    n_edges = sum(1 for (s, d) in G.edges)
    print("%s: %d subs, %d sub-level edges, %d cross-bank pairs, %d unresolved calls, %d subs not lifted"
          % (a.title, len(G.subs), n_edges, len(G.bank_edges), len(G.unresolved), len(G.failed)))
    if G.dynamic:
        print("  bank not a constant: " + ", ".join("%s %d" % kv for kv in sorted(G.dynamic.items())))
    if a.indegree:
        callers = defaultdict(set)
        for (s, d) in G.bank_edges:
            callers[d].add(s)
        print("\ncallee bank: called by N banks")
        for d in sorted(callers, key=lambda d: (-len(callers[d]), d)):
            print("  %3d: %2d  %s" % (d, len(callers[d]), sorted(callers[d])))
    if a.bank_order:
        print("\ncode banks, leaves first: " + " -> ".join(str(b) for b in bank_order(G)))
    def node(text):
        bank, _, addr = text.rpartition(":")
        addr = int(addr.lstrip("$").removeprefix("0x"), 16)
        hits = [n for n in G.subs if n[1] == addr and (not bank or n[0] == int(bank))]
        if hits:
            return hits
        return [(int(bank), addr)] if bank else []
    fmt = lambda n: "b%02d $%04X %s" % (n[0], n[1], G.subs.get(n) or "-") if n else "(root)"
    for n in node(a.callers) if a.callers else ():
        print("\ncallers of " + fmt(n))
        for s, ks in sorted(G.callers(n), key=lambda x: (x[0] is None, x[0] or (0, 0))):
            print("  [%-14s] %s" % (",".join(sorted(ks)), fmt(s)))
    for n in node(a.calls) if a.calls else ():
        print("\ncalls from " + fmt(n))
        for d, ks in sorted(G.callees(n)):
            print("  [%-14s] %s" % (",".join(sorted(ks)), fmt(d)))
    if a.walk is not None:
        walk_report(G, a.walk, a.layers, a.list)
        return 0
    if a.orphans:
        o = G.orphans(a.bank)
        print("\norphans (no caller, no root): %d" % len(o))
        for n in o:
            print("  " + fmt(n))
    if a.banks or not (a.order or a.unresolved or a.indegree or a.bank_order or a.callers or a.calls or a.orphans):
        print("\ncross-bank edges (caller bank -> callee bank: kinds)")
        for (s, d), ks in sorted(G.bank_edges.items()):
            print("  %3d -> %3d   %s" % (s, d, ",".join(sorted(ks))))
        data = defaultdict(set)
        for src, bs in G.data.items():
            data[src[0]] |= bs
        if data:
            print("\ndata banks read (caller bank -> banks)")
            for b in sorted(data):
                print("  %3d -> %s" % (b, sorted(data[b])))
    if a.unresolved:
        print("\nunresolved calls")
        for (b, s), t, why in G.unresolved:
            print("  b%02d $%04X -> $%04X   %s" % (b, s, t, why))
    if a.order:
        print("\nleaves-first order")
        for b, s in leaves_first(G):
            print("  b%02d $%04X %s" % (b, s, G.subs.get((b, s)) or "-"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
